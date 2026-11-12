#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""D1: 备份 -> 烧 kanhu-ai 固件 -> 串口验证「晴老师」唤醒。

安全设计:
  1. 烧录前【强制】读整片 16MB + NVS 16KB 存到 xiaozhi-box3/backup/,
     文件名带时间戳, 绝不覆盖任何已有文件。
  2. 备份失败直接中止, 不烧。
  3. 烧录用 mode_switch.py 里那一套相同的偏移, 保证和网页「切换」按钮一致:
        0x0      bootloader.bin
        0x8000   partition-table.bin
        0xe000   ota_data_initial.bin
        0x20000  kanhu_box.bin
        0x320000 srmodels.bin   (MultiNet7 + 「晴老师」)
  4. 烧录后自动复位, 然后打开串口读日志, 抓 wake_word / ai_client 的关键行。

用法:
    python flash_kanhu_ai.py --port COM3            # 备份 + 烧录 + 验证
    python flash_kanhu_ai.py --port COM3 --backup-only
    python flash_kanhu_ai.py --port COM3 --rollback <整片备份.bin>
"""
import argparse
import datetime
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)                      # remote-care-box
XIAOZHI_DIR = r"D:\Software\kanhu\xiaozhi-box3"
BACKUP_DIR = os.path.join(XIAOZHI_DIR, "backup")
BUILD = os.path.join(REPO, "firmware", "build")

PY = r"D:\Development Environment\ESP-IDF\python_env\Scripts\python.exe"
FLASH_SIZE = "0x1000000"                            # 16MB
NVS_OFF, NVS_LEN = 0x9000, 0x4000

# (文件, 偏移) —— 必须和 mode_switch.KANHU_AI_PARTS 一致
PARTS = [
    (os.path.join(BUILD, "bootloader", "bootloader.bin"), 0x0),
    (os.path.join(BUILD, "partition_table", "partition-table.bin"), 0x8000),
    (os.path.join(BUILD, "ota_data_initial.bin"), 0xe000),
    (os.path.join(BUILD, "kanhu_box.bin"), 0x20000),
    (os.path.join(BUILD, "srmodels", "srmodels.bin"), 0x320000),
]


def run(cmd, timeout=600):
    print("> " + " ".join(cmd[2:]))
    p = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    tail = (p.stdout or "") + (p.stderr or "")
    for ln in tail.splitlines()[-12:]:
        print("  " + ln)
    return p.returncode, tail


def check_parts():
    missing = [(p, o) for p, o in PARTS if not os.path.isfile(p)]
    if missing:
        print("缺构建产物, 先跑 idf.py build:")
        for p, o in missing:
            print("  %s  (0x%x)" % (p, o))
        return False
    for p, o in PARTS:
        print("  OK  0x%-7x %8.1f KB  %s" % (o, os.path.getsize(p) / 1024.0,
                                           os.path.basename(p)))
    return True


def do_backup(port, stamp):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    full = os.path.join(BACKUP_DIR, "full_before_kanhu_ai_%s.bin" % stamp)
    nvs = os.path.join(BACKUP_DIR, "nvs_before_kanhu_ai_%s.bin" % stamp)

    print("\n=== 1/3 备份整片 %s ===" % FLASH_SIZE)
    rc, out = run([PY, "-m", "esptool", "--port", port, "--baud", "921600",
                   "--chip", "esp32s3", "read_flash", "0x0", FLASH_SIZE, full])
    if rc != 0 or not os.path.isfile(full):
        print("整片备份失败, 中止 (不烧)")
        return None
    sz = os.path.getsize(full)
    print("整片已存: %s (%d 字节)" % (full, sz))
    if sz != int(FLASH_SIZE, 16):
        print("整片大小不对 (%d != %d), 中止" % (sz, int(FLASH_SIZE, 16)))
        return None

    print("\n=== 2/3 备份 NVS 0x%x ===" % NVS_OFF)
    rc, out = run([PY, "-m", "esptool", "--port", port, "--baud", "921600",
                   "--chip", "esp32s3", "read_flash", hex(NVS_OFF),
                   hex(NVS_LEN), nvs])
    if rc != 0 or not os.path.isfile(nvs):
        print("NVS 备份失败, 中止 (不烧)")
        return None
    print("NVS 已存: %s (%d 字节)" % (nvs, os.path.getsize(nvs)))
    return full, nvs


def do_flash(port):
    print("\n=== 3/3 烧录 kanhu-ai 固件 ===")
    args = [PY, "-m", "esptool", "--port", port, "--baud", "921600",
            "--chip", "esp32s3", "write_flash",
            "--flash_mode", "dio", "--flash_freq", "80m",
            "--flash_size", "16MB"]
    # esptool v4: 地址和文件名是【位置参数】, 一对一对地写。
    # 不要再用老版本的 -z/--format, v4 已经不支持了 (会报 unrecognized arguments)。
    for p, off in PARTS:
        args += [hex(off), p]
    rc, out = run(args, timeout=900)
    if rc != 0:
        print("烧录失败!")
        return False
    print("烧录完成")
    return True


def do_rollback(port, full_bin):
    print("\n=== 回滚: 整片回灌 %s ===" % full_bin)
    rc, out = run([PY, "-m", "esptool", "--port", port, "--baud", "921600",
                   "--chip", "esp32s3", "write_flash", "--flash_size", "keep",
                   "0x0", full_bin], timeout=900)
    print("回滚%s" % ("成功" if rc == 0 else "失败"))
    return rc == 0


def monitor(port, seconds=90):
    """烧录后读串口日志, 抓 wake_word / ai_client 关键行。"""
    import serial
    print("\n=== 启动日志抓取 (%d 秒) ===" % seconds)
    try:
        s = serial.Serial(port, 115200, timeout=0.3)
    except Exception as e:
        print("串口打不开: %s" % e)
        return []
    s.reset_input_buffer()
    seen = []
    pat = re.compile(r"wake_word|ai_client|WakeWord|ai_state|晴老师|"
                     r"MultiNet|srmodels|model 分区|ERROR|W \(", re.I)
    t0 = time.time()
    buf = ""
    try:
        while time.time() - t0 < seconds:
            d = s.read(4096)
            if not d:
                continue
            buf += d.decode("utf-8", "ignore")
            while "\n" in buf:
                ln, buf = buf.split("\n", 1)
                ln = ln.strip()
                if not ln:
                    continue
                if pat.search(ln):
                    seen.append(ln)
                    print("  " + ln)
    except Exception as e:
        print("读取异常: %s" % e)
    finally:
        s.close()
    return seen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM3")
    ap.add_argument("--backup-only", action="store_true")
    ap.add_argument("--rollback", metavar="FULL_BIN")
    ap.add_argument("--no-monitor", action="store_true")
    ap.add_argument("--skip-backup", action="store_true",
                    help="已有备份时跳过备份步骤, 直接烧")
    a = ap.parse_args()

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    if a.rollback:
        return 0 if do_rollback(a.port, a.rollback) else 1

    print("=== 检查构建产物 ===")
    if not check_parts():
        return 2

    if a.skip_backup:
        print("\n=== 跳过备份 (--skip-backup) ===")
    else:
        r = do_backup(a.port, stamp)
        if not r:
            print("\n备份失败, 未烧录任何东西。")
            return 3
        full, nvs = r
        print("\n回滚办法:")
        print("  整片: python flash_kanhu_ai.py --port %s --rollback \"%s\"" % (a.port, full))
        print("  NVS  : esptool write_flash 0x9000 \"%s\"" % nvs)

        if a.backup_only:
            print("\n--backup-only, 结束。")
            return 0

    if not do_flash(a.port):
        return 4

    print("\n等板子复位...")
    time.sleep(12)
    if not a.no_monitor:
        monitor(a.port, 90)
    return 0


if __name__ == "__main__":
    sys.exit(main())
