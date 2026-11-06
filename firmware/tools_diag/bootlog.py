import serial, time, sys, re
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

s = serial.Serial('COM3', 115200, timeout=0.3)

# 硬复位板子, 抓完整开机日志
print('=== 复位并抓开机日志 ===', flush=True)
s.reset_input_buffer()
s.setDTR(False)
s.rts = False
time.sleep(0.2)
s.setDTR(True)   # EN 拉低复位
time.sleep(0.1)
s.setDTR(False)
s.rts = True

buf = b''
t0 = time.time()
while time.time() - t0 < 25:
    d = s.read(8192)
    if d:
        buf += d

t = re.sub(r'\x1b\[[0-9;]*m', '', buf.decode('utf-8', 'replace'))
lines = t.splitlines()
print('共 %d 行' % len(lines), flush=True)
print()
print('=== 摄像头/DMA 相关 ===')
keep = False
for l in lines:
    if any(k in l for k in ('cam_hal', 'cam_', 'gc0308', 'camera', 'dma', 'DMA', 'node', 'buffer', 'PCLK', 'I2C', 'SCCB', 'PSRAM', 'LCD', 'esp_lcd')):
        print('  ' + l.strip()[:160])

print()
print('=== 警告/错误 ===')
for l in lines:
    if any(k in l for k in ('WARN', 'ERROR', 'E ', 'fail', 'Fail', 'FAIL', 'overflow', 'OVF', 'drop', 'DROP')):
        print('  ' + l.strip()[:160])

s.close()
