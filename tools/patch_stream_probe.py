"""
一次性补丁: 修复 /stream 空闲探测误发 chunked 结束符的 bug。

背景:
    firmware/main/main.c 的 stream_handler 里, 等待新帧时用
    httpd_resp_send_chunk(req, NULL, 0) 探测客户端是否断开。

    但 IDF 语义是: buf_len = 0 表示【结束这个 chunked 响应】,
    会往线上发 "0\r\n\r\n" 终止符。

    后果: 只要有超过 100ms 没等到新帧, 整个 multipart 流就被提前终止,
    浏览器/urllib 看到结束标记就停止读取并断开 -> 画面断断续续。

    实测 (修之前): 12 秒 713353 bytes / 61 帧, 但第 34273 字节处就
    插入了终止符, 后面 678869 字节成了废数据。

修复:
    改用 MSG_PEEK 在 socket 上做零副作用探测, 并把探测周期放宽到 500ms
    (正常帧间隔约 190ms, 500ms 足够宽松, 不会误伤)。

用法:
    python tools/patch_stream_probe.py
"""
import io
import os

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "firmware", "main", "main.c")

OLD = """            static int idle_ticks = 0;
            if (++idle_ticks >= 5) {   /* 每 100ms 探一次 */
                idle_ticks = 0;
                if (httpd_resp_send_chunk(req, NULL, 0) != ESP_OK) {
                    ESP_LOGI(TAG, "画面客户端已断开 (空闲探测发现)");
                    break;
                }
            }"""

NEW = """            static int idle_ticks = 0;
            if (++idle_ticks >= 25) {   /* 每 20ms 一次 -> 500ms 探一次 */
                idle_ticks = 0;
                if (!stream_client_alive(req)) {
                    ESP_LOGI(TAG, "画面客户端已断开 (socket 探测发现)");
                    break;
                }
            }"""

# 探测函数, 插到 stream_handler 之前
HELPER = '''
/*
 * 【2026-11-07 新增】探测 /stream 客户端是否已断开 (零副作用)。
 *
 * 【为什么不能用 httpd_resp_send_chunk(req, NULL, 0)】
 *   IDF 文档: "When you are finished sending all your chunks, you must
 *   call this function with buf_len as 0."
 *   也就是说 buf_len=0 的语义是【结束 chunked 响应】, 会发 "0\\r\\n\\r\\n"
 *   终止符, 不是"发 0 字节探测"。
 *
 *   用它做探测的后果 (实测): 消费者只要超过 100ms 没等到新帧,
 *   整个 multipart 流就被提前终止 -> 客户端停止读取并断开 -> 画面卡顿。
 *
 * 【怎么做到零副作用】
 *   用 MSG_PEEK 读 socket:
 *     - 返回 0    -> 对方发了 FIN (半关闭), 客户端已断开
 *     - 返回 <0   -> 出错 (含 RST), 客户端已断开
 *     - 返回 >0   -> 客户端发来了数据, peek 出来丢掉即可 (不影响)
 *   全程不往线上写任何字节, 不会破坏 chunked 协议。
 */
static bool stream_client_alive(httpd_req_t *req)
{
    int fd = httpd_req_to_sockfd(req);
    if (fd < 0) {
        return false;
    }

    fd_set rfds;
    FD_ZERO(&rfds);
    FD_SET(fd, &rfds);
    struct timeval tv = { .tv_sec = 0, .tv_usec = 0 };

    /* 只等 0 秒: 有数据/异常立即返回, 没有就当"还活着" */
    int r = select(fd + 1, &rfds, NULL, NULL, &tv);
    if (r <= 0) {
        return true;   /* 没动静 -> 还活着 */
    }

    char tmp[64];
    int n = recv(fd, tmp, sizeof(tmp), MSG_PEEK);
    if (n > 0) {
        /* 客户端发来了数据, peek 掉不算消费, 协议上无害 */
        return true;
    }
    if (n == 0) {
        return false;  /* FIN: 对方已关闭写方向 */
    }
    return false;      /* 出错 (含 RST) */
}

'''

MARKER = "static esp_err_t stream_handler(httpd_req_t *req)"


def main():
    src = io.open(PATH, encoding="utf-8").read()

    if OLD not in src:
        if NEW in src:
            print("已经是修复后的版本, 无需改动")
            return
        raise SystemExit("找不到待替换的探测代码块, 固件源码可能已变动")

    src = src.replace(OLD, NEW, 1)

    if "stream_client_alive" in src and HELPER.split("\n", 2)[2] not in src:
        # 插到 stream_handler 之前
        idx = src.index(MARKER)
        src = src[:idx] + HELPER.lstrip("\n") + src[idx:]

    io.open(PATH, "w", encoding="utf-8").write(src)
    print("补丁已应用: 空闲探测改为 MSG_PEEK 零副作用探测")


if __name__ == "__main__":
    main()
