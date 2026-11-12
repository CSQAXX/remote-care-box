# -*- coding: utf-8 -*-
"""校验 srmodels.bin 里的 fst/commands_cn.txt 是否正常 (条数 / 内容)。

esp-sr 的 FST 编译器有一个硬性前提: 命令表【不能太短】。
只有 1~2 条命令时 build_fsts / fst_compile_from_commands 会越界写,
板子直接 Guru Meditation (StoreProhibited, EXCVADDR 0xfffffffc) 无限重启。

所以每次重新打包模型后都跑一下这个脚本, 别等刷完板子才发现又崩了。

srmodels.bin 格式 (见 esp-sr model/pack_model.py):
    uint32  model_num
    每个模型:
        char   name[32]        (固定长度, 0 填充)
        uint32 file_num
        每个文件:
            char   filename[32] (固定长度, 0 填充)
            uint32 start       (数据段内偏移)
            uint32 len
    之后是数据段, 所有文件内容按顺序拼接

用法: python check_srmodels.py <srmodels.bin> [期望的拼音 ...]
"""
import hashlib
import struct
import sys

OFFICIAL_313_SHA = "a516f97ec1a826c7cd8396156857169aa3b73df122596f7854d90b23a15f2252"


def parse(path):
    """解析出 (文件名, 内容) 列表。"""
    d = open(path, "rb").read()
    off = 0
    n_models, = struct.unpack_from("<I", d, off)
    off += 4

    entries = []
    for _ in range(n_models):
        off += 32                                # model name (定长填充)
        n_files, = struct.unpack_from("<I", d, off)
        off += 4
        for _ in range(n_files):
            raw = d[off:off + 32]
            off += 32
            fname = raw.split(b"\x00", 1)[0].decode("utf-8", "replace")
            start, ln = struct.unpack_from("<II", d, off)
            off += 8
            entries.append((fname, start, ln))

    # 注意: pack_model.py 里存的 start 是【整个文件的绝对偏移】(含头部),
    #      不是数据段内的相对偏移。
    for fname, start, ln in entries:
        yield fname, d[start:start + ln]


def main():
    path = sys.argv[1]
    expect = sys.argv[2:]
    print("检查 %s" % path)

    cmd = None
    for fname, data in parse(path):
        if fname.endswith("commands_cn.txt"):
            cmd = data
            break

    if cmd is None:
        print("  !! 没找到 fst/commands_cn.txt")
        return 2

    lines = [l for l in cmd.split(b"\n") if l.strip()]
    ids = [int(l.split(b",", 1)[0]) for l in lines]
    sha = hashlib.sha256(cmd).hexdigest()

    print("  commands_cn.txt: %d 条  (id %d..%d)  %d 字节  SHA256 %s"
          % (len(lines), min(ids), max(ids), len(cmd), sha[:16]))
    print("  官方 313 条原版: %s"
          % ("逐字节一致 (未加自定义命令)" if sha == OFFICIAL_313_SHA
             else "已被修改 (正常, 加了自定义命令)"))

    ok = True
    if len(lines) < 50:
        print("  !! 条数太少 (%d), FST 编译器会越界写 -> 板子 panic" % len(lines))
        ok = False
    else:
        print("  条数充足 (%d), FST 编译器不会越界" % len(lines))

    for e in expect:
        hit = any(l.split(b",", 1)[1].decode("utf-8").strip() == e for l in lines)
        print("  含 '%s': %s" % (e, "是" if hit else "否 !!"))
        ok = ok and hit

    print("结果: %s" % ("通过" if ok else "有问题"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
