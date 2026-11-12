"""
列出 esp-sr 内置的唤醒词模型 (只读网络, 不改任何东西)。

用法: python list_wakewords.py
"""
import re
import urllib.request

URL = "https://raw.githubusercontent.com/espressif/esp-sr/master/wakeword_list.md"

s = urllib.request.urlopen(URL, timeout=20).read().decode("utf-8")
rows = re.findall(r"^\| ([^|]+?) \| `([^`]+)` \|$", s, re.M)

cn_pat = re.compile("[\u4e00-\u9fff]")
cn = [(a.strip(), b.strip()) for a, b in rows if cn_pat.search(a)]

print("总模型数: %d   中文唤醒词: %d" % (len(rows), len(cn)))
print("-" * 46)
for a, b in cn:
    print("  %-14s %s" % (a, b))
