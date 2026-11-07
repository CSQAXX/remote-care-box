"""
一次性补丁: 让 /stream 的客户端断开检测更及时。

背景:
    上一轮修复把空闲探测从 httpd_resp_send_chunk(req, NULL, 0)
    换成了 MSG_PEEK 零副作用探测, 解决了"流被提前终止"的问题。

    但新问题: 探测只在【等不到新帧】的分支里执行。正常情况帧率是 5fps,
    每 190ms 就有一帧, 于是几乎永远走不到那个分支 —— 客户端断开只能
    等下一次 send_chunk 失败, 而 TCP 发送失败要等内核重传超时
    (可能好几秒), 期间 socket 和 STREAM_MAX_CLIENTS 配额都被占着。

    现场: 断连测试连开 3 路流后, 8080 直接连不上 (连接超时),
    因为 2 个流配额被"已死但未释放"的连接占满。

修复:
    每发完一帧就探测一次 (每 190ms 一次, 开销可忽略),
    这样 RST/FIN 最迟 200ms 内就能被发现并释放配额。

用法:
    python tools/patch_stream_disconnect.py
"""
import io
import os

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "firmware", "main", "main.c")

OLD = """        slot_release(s_jpg_slots, idx);

        if (!ok) {
            break;
        }
        s_frame_count++;
    }"""

NEW = """        slot_release(s_jpg_slots, idx);

        if (!ok) {
            break;
        }
        s_frame_count++;

        /*
         * 【2026-11-07 新增】发完一帧就探测一次客户端是否还在。
         *
         * 为什么不能只在"等不到新帧"时探测:
         *   正常帧率 5fps, 每 190ms 就有新帧, 于是几乎永远走不到
         *   那个分支 —— 客户端断开只能等下一次 send_chunk 失败,
         *   而 TCP 发送失败要等内核重传超时 (可能好几秒), 期间
         *   socket 和 STREAM_MAX_CLIENTS 配额一直被占着。
         *
         * 现场: 断连测试连开 3 路流后, 8080 直接连不上 (连接超时),
         *       因为 2 个流配额被"已死但未释放"的连接占满。
         *
         * MSG_PEEK 是零副作用的, 每帧一次的开销可以忽略。
         */
        if (!stream_client_alive(req)) {
            ESP_LOGI(TAG, "画面客户端已断开 (发帧后探测发现)");
            break;
        }
    }"""

# 探测周期改回快一些 (每帧都会查, 这里只是兜底)
OLD_IDLE = "            if (++idle_ticks >= 25) {   /* 每 20ms 一次 -> 500ms 探一次 */"
NEW_IDLE = "            if (++idle_ticks >= 10) {   /* 每 20ms 一次 -> 200ms 探一次 */"


def main():
    src = io.open(PATH, encoding="utf-8").read()

    if NEW in src:
        print("已经是修复后的版本, 无需改动")
        return

    if OLD not in src:
        raise SystemExit("找不到待替换的发送后代码块, 固件源码可能已变动")

    src = src.replace(OLD, NEW, 1)
    src = src.replace(OLD_IDLE, NEW_IDLE, 1)

    io.open(PATH, "w", encoding="utf-8").write(src)
    print("补丁已应用: 发帧后立即探测客户端存活, 快速释放流配额")


if __name__ == "__main__":
    main()
