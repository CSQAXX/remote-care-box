import serial, time, re, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

s = serial.Serial('COM3', 115200, timeout=0.3)


def cmd(c, wait=3.0):
    s.reset_input_buffer()
    s.write((c + '\r\n').encode())
    s.flush()
    t0 = time.time()
    buf = b''
    while time.time() - t0 < wait:
        d = s.read(8192)
        if d:
            buf += d
    return re.sub(r'\x1b\[[0-9;]*m', '', buf.decode('utf-8', 'replace'))


print('=== 切到合成渐变图模式 (lcd2) ===', flush=True)
t = cmd('lcd2', 3)
for l in t.splitlines():
    if 'LCD' in l or '模式' in l:
        print('  ' + l.strip())

time.sleep(4)
print()
print('=== 检查设备是否还活着 (说明 LCD 自绘没把系统拖死) ===')
t = cmd('q', 2.5)
m = re.search(r'g2_bad=([\d.]+)%', t)
print('  q 指标: %s' % (m.group(0) if m else t.strip()[-100:]))

print()
print('=== 切到固定测试图样 (lcd1) ===', flush=True)
t = cmd('lcd1', 3)
for l in t.splitlines():
    if 'LCD' in l or '模式' in l:
        print('  ' + l.strip())
time.sleep(4)

t = cmd('stat', 2.5)
for l in t.splitlines():
    if 'cam_frames' in l:
        print('  ' + l.strip())

print()
print('=== 切回摄像头画面 ===', flush=True)
cmd('lcd0', 2)
time.sleep(2)

s.close()
print('完成 —— 请看屏幕: 现在应该是摄像头画面')
