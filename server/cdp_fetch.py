"""用 Chrome 自己的登录态抓 TV 后台数据（不需要解密 v20 Cookie）。

原理：Chrome 关闭后用同一个用户目录 + 远程调试端口启动一个无头实例，
它内部会自己解密 Cookie；我们再通过 CDP 的 Runtime.evaluate 在页面里
fetch 接口，把业务数据取回来。

用法：runtime\\python.exe cdp_fetch.py --url https://dz-ailab.dzkjm.cn/admin-web/presets \
          --expr "fetch('/api-data/api/v1/presets?page=1&pageSize=200').then(r=>r.json())" \
          --out data/tv_presets.json
"""

import argparse
import base64
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.request

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
USER_DATA = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data")
PORT = 9333


class WS:
    """够用的最小 WebSocket 客户端（文本帧 + 掩码 + 分片处理）。"""

    def __init__(self, url):
        assert url.startswith("ws://")
        host_port, path = url[5:].split("/", 1)
        host, port = host_port.split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET /%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (path, host_port, key))
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        self.head, self.buf = buf.split(b"\r\n\r\n", 1)
        if b"101" not in self.head.split(b"\r\n")[0]:
            raise RuntimeError("WebSocket 握手失败: %r" % self.head[:120])

    def _recv_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("连接被关闭")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, text):
        payload = text.encode()
        mask = os.urandom(4)
        header = bytearray([0x81])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv(self):
        while True:
            b1, b2 = self._recv_exact(2)
            opcode = b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            data = self._recv_exact(length) if length else b""
            if opcode == 0x1:
                return data.decode("utf-8", "replace")
            if opcode == 0x8:
                raise RuntimeError("服务端关闭连接")
            # ping/pong/其它帧忽略


def http_json(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="先打开的页面（同源即可）")
    ap.add_argument("--expr", required=True, help="要执行的 JS 表达式（返回 Promise 也行）")
    ap.add_argument("--out", default="", help="把返回值写到这个文件")
    ap.add_argument("--profile", default=USER_DATA)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--keep", action="store_true", help="跑完不关浏览器（调试用）")
    args = ap.parse_args()

    if not os.path.exists(CHROME):
        print("找不到 chrome.exe：%s" % CHROME)
        return 2

    proc = subprocess.Popen([
        CHROME, "--headless=new", "--disable-gpu", "--no-first-run",
        "--remote-debugging-port=%d" % args.port,
        "--user-data-dir=" + args.profile,
        "--profile-directory=Default",
        args.url,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    target = None
    for _ in range(40):
        time.sleep(0.4)
        try:
            tabs = http_json("http://127.0.0.1:%d/json/list" % args.port)
        except Exception:
            continue
        for t in tabs:
            if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                target = t
                break
        if target:
            break
    if not target:
        print("连不上 Chrome 调试端口，浏览器可能没起来")
        if not args.keep:
            proc.terminate()
        return 2

    ws = WS(target["webSocketDebuggerUrl"])
    ws.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
    msg_id = 2
    ws.send(json.dumps({"id": msg_id, "method": "Runtime.evaluate", "params": {
        "expression": args.expr, "awaitPromise": True, "returnByValue": True, "timeout": 60000}}))
    result = None
    deadline = time.time() + 90
    while time.time() < deadline:
        msg = json.loads(ws.recv())
        if msg.get("id") == msg_id:
            result = msg
            break
    if not args.keep:
        proc.terminate()

    if not result:
        print("没有拿到结果（超时）")
        return 1
    if "error" in result:
        print("CDP 报错：%s" % json.dumps(result["error"], ensure_ascii=False)[:300])
        return 1
    res = result.get("result", {}).get("result", {})
    if res.get("subtype") == "error" or "exceptionDetails" in result.get("result", {}):
        print("页面脚本抛错：%s" % json.dumps(result.get("result", {}).get("exceptionDetails", {}),
                                              ensure_ascii=False)[:400])
        return 1
    value = res.get("value")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            if isinstance(value, (dict, list)):
                json.dump(value, f, ensure_ascii=False, indent=1)
            else:
                f.write(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
        size = os.path.getsize(args.out)
        print("[ok] 写入 %s（%d 字节）" % (args.out, size))
    else:
        print(json.dumps(value, ensure_ascii=False)[:2000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
