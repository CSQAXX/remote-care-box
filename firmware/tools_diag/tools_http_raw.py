import urllib.request, time, sys, os

IP = "192.168.2.138"

def fetch_raw(path):
    url = f"http://{IP}:8080/raw?capture=1"
    t0 = time.time()
    r = urllib.request.urlopen(url, timeout=30)
    data = r.read()
    return data, time.time() - t0

def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    prefix = sys.argv[2] if len(sys.argv) > 2 else "h"
    outs = []
    for i in range(n):
        try:
            data, dt = fetch_raw(None)
            p = f"{prefix}{i}.bin"
            open(p, "wb").write(data)
            print(f"  {p}: {len(data)} bytes ({dt:.2f}s)")
            outs.append(p)
        except Exception as e:
            print(f"  #{i} failed: {e}")
        time.sleep(0.4)
    print("saved:", outs)

if __name__ == "__main__":
    main()
