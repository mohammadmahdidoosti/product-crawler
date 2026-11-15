"""(15 Nov) Crawler host side of the SSH bridge to the shop (see tools/shop_bridge.php).

The crawler runs in a container without the host's ssh key. This small server runs on the HOST: the
container connects to it over the docker network, sends the shared token, and gets a WordPress process
on the shop (ssh shop ... wp eval-file shop_bridge.php) wired to its connection. One connection = one
WordPress process; at most --max at a time.

    python3 bridge_proxy.py --bind 10.0.1.1 --port 8765 --token-file /srv/caravan-data/tmp/bridge.token

Only standard library. The token file is readable in the container as /data/tmp/bridge.token.
"""
from __future__ import annotations

import argparse
import hmac
import socket
import subprocess
import threading
import time

REMOTE = ("cd /home/caravan/htdocs/caravansanat.com && sudo -u caravan "
          "wp eval-file /opt/caravan-bridge/shop_bridge.php --user=caravancrawler")


def log(*a):
    print(time.strftime("%m-%d %H:%M:%S"), *a, flush=True)


def serve(conn: socket.socket, addr, token: str, slots: threading.Semaphore, ssh: str) -> None:
    if not slots.acquire(blocking=False):
        conn.sendall(b"@@E busy\n")
        conn.close()
        return
    proc = None
    try:
        f = conn.makefile("rb")
        got = f.readline().strip().decode(errors="ignore")
        if not hmac.compare_digest(got, token):
            log("refused", addr)
            conn.sendall(b"@@E token\n")
            return
        proc = subprocess.Popen(["ssh", "-o", "ServerAliveInterval=30", "-o", "BatchMode=yes", ssh, REMOTE],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
        log("open", addr, "pid", proc.pid)

        def up():                                       # container -> shop
            try:
                for line in f:
                    proc.stdin.write(line)
                    proc.stdin.flush()
            except (OSError, ValueError):
                pass
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass

        threading.Thread(target=up, daemon=True).start()
        while True:                                     # shop -> container
            chunk = proc.stdout.read1(65536) if hasattr(proc.stdout, "read1") else proc.stdout.read(65536)
            if not chunk:
                break
            conn.sendall(chunk)
    except OSError as e:
        log("error", addr, e)
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
        try:
            conn.close()
        except OSError:
            pass
        slots.release()
        log("closed", addr)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bind", required=True, help="the docker network gateway address (not 0.0.0.0)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--token-file", required=True)
    ap.add_argument("--max", type=int, default=3, help="WordPress processes on the shop at once")
    ap.add_argument("--ssh", default="shop", help="ssh host alias of the shop server")
    a = ap.parse_args()
    token = open(a.token_file).read().strip()
    if len(token) < 20:
        raise SystemExit("token too short")
    slots = threading.Semaphore(a.max)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((a.bind, a.port))
    s.listen(8)
    log("listening on", a.bind, a.port, "max", a.max)
    while True:
        conn, addr = s.accept()
        threading.Thread(target=serve, args=(conn, addr, token, slots, a.ssh), daemon=True).start()


if __name__ == "__main__":
    main()
