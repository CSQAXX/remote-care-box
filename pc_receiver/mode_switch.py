#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""看护固件 <-> 小智 AI 固件 一键切换（供控制台「⑤ 模式切换」按钮调用）。

纯标准库、独立模块，不改动 pc_console.py 的任何既有逻辑。
切换通过 USB 串口的 esptool 整片烧录完成，**不依赖板子 IP / 板子是否联网**：

  kanhu   : backup/original_full_16MB.bin -> 0x0
            （刷小智之前的整片备份，自带当时可用的 Wi-Fi 配置）
  xiaozhi : firmware/merged-binary.bin     -> 0x0
            nvs/nvs_xiaozhi_saved.bin      -> 0x9000
            （写回小智的 NVS：Wi-Fi 凭据 + 音量，免得每次切回来都要重新配网）

  kanhu-ai : 【2026-11-08 新增】养老盒子自研固件 + AI 对话 + 「晴老师」唤醒词
            firmware/build/kanhu_box.bin       -> 0x20000 (app)
            firmware/build/bootloader.bin      -> 0x0
            firmware/build/partition-table.bin -> 0x8000
            firmware/build/ota_data_initial.bin-> 0xe000
            firmware/build/srmodels/srmodels.bin-> 0x320000 (model 分区, MultiNet7)
            （这个固件自己就带 AI 对话客户端，不需要再切到小智固件）

可用环境变量覆盖（换电脑 / 换串口时不用改代码）：
  XIAOZHI_BOX_DIR      xiaozhi-box3 目录
  XIAOZHI_ESPTOOL_PY   带 esptool 的 Python
  XIAOZHI_COM          串口号，默认 COM3
  MODE_SWITCH_DRY_RUN  设为 1 则只构造命令不真烧（离线自测用）
"""
import json
import os
import socket
import subprocess
import threading
import time
from datetime import datetime

BOX_DIR = os.environ.get(
    "XIAOZHI_BOX_DIR", r"D:\Software\kanhu\xiaozhi-box3")
ESPTOOL_PY = os.environ.get(
    "XIAOZHI_ESPTOOL_PY",
    r"D:\Development Environment\ESP-IDF\python_env\Scripts\python.exe")
COM_PORT = os.environ.get("XIAOZHI_COM", "COM3")

# 自研固件的项目根目录（remote-care-box），和 xiaozhi-box3 是两套东西
KANHU_BOX_DIR = os.environ.get(
    "KANHU_BOX_DIR", r"D:\Software\kanhu\remote-care-box")

IMG_KANHU = os.path.join(BOX_DIR, "backup", "original_full_16MB.bin")
IMG_XIAOZHI = os.path.join(BOX_DIR, "firmware", "merged-binary.bin")
NVS_XIAOZHI = os.path.join(BOX_DIR, "nvs", "nvs_xiaozhi_saved.bin")
STATE_FILE = os.path.join(BOX_DIR, "mode_state.json")
LOG_FILE = os.path.join(BOX_DIR, "diag", "mode_switch.log")

# --- kanhu-ai (自研固件 + AI 对话) 的构建产物 ---
BUILD_DIR = os.path.join(KANHU_BOX_DIR, "firmware", "build")
KANHU_AI_PARTS = [
    (os.path.join(BUILD_DIR, "bootloader", "bootloader.bin"), 0x0,
     "自研固件 bootloader"),
    (os.path.join(BUILD_DIR, "partition_table", "partition-table.bin"), 0x8000,
     "自研固件分区表"),
    (os.path.join(BUILD_DIR, "ota_data_initial.bin"), 0xe000,
     "自研固件 OTA 数据"),
    (os.path.join(BUILD_DIR, "kanhu_box.bin"), 0x20000,
     "自研固件主程序"),
    (os.path.join(BUILD_DIR, "srmodels", "srmodels.bin"), 0x320000,
     "MultiNet7 模型 (「晴老师」唤醒词)"),
]

NAMES = {"kanhu": "看护模式", "xiaozhi": "小智 AI", "kanhu-ai": "看护+AI对话"}
EXPECT = (
    (IMG_KANHU, 16777216, "看护固件整片备份"),
    (IMG_XIAOZHI, None, "小智固件镜像"),
    (NVS_XIAOZHI, 16384, "小智 NVS 配置"),
)


class ModeSwitcher:
    def __init__(self, device):
        self.device = device
        self.lock = threading.Lock()
        self.busy = False
        self.target = ""
        self.phase = "空闲"
        self.started = 0.0
        self.log = []
        self.last_ok = None
        self.last_target = None
        self.last_result = ""
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                st = json.load(f)
            self.last_target = st.get("last_target")
            self.last_result = "上次切换：%s（%s，%s）" % (
                NAMES.get(self.last_target, self.last_target or "?"),
                "成功" if st.get("last_ok") else "失败",
                st.get("time", ""))
        except Exception:
            pass

    def _next_target(self):
        """决定下一个目标。

        优先信状态文件 —— 板子换了 IP 也不会切错方向。
        新增 kanhu-ai (自研固件 + AI 对话) 之后, 三种模式轮转:
          kanhu <-> kanhu-ai <-> xiaozhi
        """
        order = ["kanhu", "kanhu-ai", "xiaozhi"]
        if self.last_target in order:
            i = order.index(self.last_target)
            return order[(i + 1) % len(order)]
        # 没有状态记录: 按当前实际状态决定往哪走
        mode = self.detect()
        if mode == "xiaozhi":
            return "kanhu-ai"
        if mode == "kanhu-ai":
            return "xiaozhi"
        return "kanhu-ai"

    # -- 就绪检查 -----------------------------------------------------------
    def problems(self):
        ps = []
        if not os.path.isfile(ESPTOOL_PY):
            ps.append("找不到 esptool 的 Python：" + ESPTOOL_PY)
        for path, size, label in EXPECT:
            if not os.path.isfile(path):
                ps.append("缺%s：%s" % (label, path))
            elif size and os.path.getsize(path) != size:
                ps.append("%s大小不对：%d（应为 %d）" %
                          (label, os.path.getsize(path), size))
        # kanhu-ai 的构建产物 (只有切到它时才必须齐)
        #
        # 注意: 这里必须用 _next_target(), 不能用 self.next ——
        #   self.next 这个属性根本不存在 (是早期版本的遗留写法),
        #   一调 problems() 就 AttributeError, 整个模式切换页面直接 500。
        if self._next_target() == "kanhu-ai":
            for path, _off, label in KANHU_AI_PARTS:
                if not os.path.isfile(path):
                    ps.append("缺%s：%s（先跑 idf.py build）" % (label, path))
        return ps

    # -- 当前模式 -------------------------------------------------------
    # 看护固件开 8081, 小智固件不开。
    # 但 "看护" 和 "看护+AI对话" 都开 8081 —— 得再问一句 /health 里
    # 有没有 ai_state 字段才能分清:
    #   有 ai_state -> kanhu-ai (自研固件 + AI 对话 + 「晴老师」唤醒词)
    #   没有       -> kanhu   (纯看护固件)
    def detect(self):
        try:
            health = self._fetch_health()
        except Exception:
            health = None
        if health is not None:
            if health.get("ai_state") is not None:
                return "kanhu-ai"
            return "kanhu"
        # /health 拿不到 (固件刚启动/正在重启) -> 退回端口判断
        s = socket.socket()
        s.settimeout(0.5)
        try:
            s.connect((self.device, 8081))
            return "kanhu"
        except OSError:
            return "xiaozhi"
        finally:
            s.close()

    def _fetch_health(self):
        """读板子控制面 /health (短超时, 拿不到就抛异常)。"""
        import json
        import urllib.request
        url = "http://%s:8081/health" % self.device
        with urllib.request.urlopen(url, timeout=1.5) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def status(self):
        ps = self.problems()
        mode = self.detect()
        return {
            "ready": not ps,
            "problems": ps,
            "mode": mode,
            "mode_name": NAMES.get(mode, mode),
            "next": self._next_target(),
            "last_target": self.last_target,
            "busy": self.busy,
            "target": self.target,
            "phase": self.phase,
            "elapsed": int(time.time() - self.started) if self.busy else 0,
            "log": self.log[-8:],
            "last_result": self.last_result,
            "last_ok": self.last_ok,
            "com": COM_PORT,
        }

    # -- 切换 ---------------------------------------------------------------
    def switch(self):
        with self.lock:
            if self.busy:
                return {"ok": False, "msg": "正在切换中，请等这一轮完成"}
            ps = self.problems()
            if ps:
                return {"ok": False, "msg": "切换工具未就绪", "problems": ps}
            target = self._next_target()
            self.busy = True
            self.target = target
            self.phase = "准备中"
            self.started = time.time()
            self.log = []
            self.last_result = ""
            threading.Thread(target=self._run, args=(target,),
                             daemon=True).start()
            return {"ok": True, "msg": "开始切换到 " + NAMES[target],
                    "target": target}

    def _commands(self, target):
        # esptool v4 (>=4.0): 地址和文件名是【位置参数】, 一对一对地写。
        # 老写法的 "-z 0x0 file" 在 v4 里会直接报
        #   "unrecognized arguments" —— 整个切换失败, 板子纹丝不动。
        base = [ESPTOOL_PY, "-m", "esptool", "--port", COM_PORT,
                "--baud", "921600", "--chip", "esp32s3"]
        if target == "kanhu":
            # 整片备份必须 keep：镜像头里带着原固件自己的 flash 参数
            return [("烧录看护固件（16MB 完整备份）",
                     base + ["write_flash", "--flash_size", "keep",
                             "0x0", IMG_KANHU])]
        if target == "kanhu-ai":
            # 自研固件 + MultiNet7 模型 (「晴老师」唤醒词)
            args = ["write_flash", "--flash_mode", "dio",
                    "--flash_freq", "80m", "--flash_size", "16MB"]
            for path, off, _label in KANHU_AI_PARTS:
                args += [hex(off), path]
            return [("烧录自研固件 + AI 唤醒词模型", base + args)]
        return [
            ("烧录小智 AI 固件",
             base + ["write_flash", "--flash_mode", "dio",
                     "--flash_freq", "80m", "--flash_size", "16MB",
                     "0x0", IMG_XIAOZHI]),
            ("写回 Wi-Fi 与音量配置",
             base + ["write_flash", "--flash_mode", "dio",
                     "--flash_freq", "80m", "--flash_size", "16MB",
                     "0x9000", NVS_XIAOZHI]),
        ]

    def _run(self, target):
        ok = False
        try:
            for phase, cmd in self._commands(target):
                self.phase = phase
                self._note("> " + " ".join(cmd[3:7]) + " ...")
                if os.environ.get("MODE_SWITCH_DRY_RUN") == "1":
                    self._note("[dry-run] 跳过烧录：" + " ".join(cmd))
                    time.sleep(0.05)
                    continue
                r = subprocess.run(cmd, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, timeout=900)
                out = r.stdout.decode("utf-8", "replace")
                tail = [x.strip() for x in out.replace("\r", "\n").splitlines()
                        if x.strip()][-6:]
                for line in tail:
                    self._note(line)
                if r.returncode != 0:
                    raise RuntimeError("esptool 退出码 %d：%s" %
                                       (r.returncode, tail[-1] if tail else ""))
            ok = True
            self.last_result = "切换完成：现在是%s，板子已自动重启" % NAMES[target]
        except Exception as e:
            self.last_result = "切换失败：" + str(e)[:200]
            self._note("ERROR " + str(e))
        finally:
            self.busy = False
            self.phase = "完成" if ok else "失败"
            self.last_ok = ok
            self._save_state(target, ok)
            self._note(self.last_result)

    def _note(self, text):
        line = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), text)
        self.log.append(line)
        if len(self.log) > 60:
            self.log = self.log[-60:]
        try:
            os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def _save_state(self, target, ok):
        self.last_target = target
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({"last_target": target, "last_ok": ok,
                           "time": datetime.now().isoformat(timespec="seconds")},
                          f, ensure_ascii=False, indent=1)
        except Exception:
            pass
