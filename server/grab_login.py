"""一键取登录态：从本机浏览器里读 TV 后台 Cookie 与 Flow 后台 token。

前提：**先把 Chrome / Edge 完全关闭**（浏览器运行时这两处数据都被独占锁住）。
取不到时会明确告诉你下一步怎么办。

用法：
  runtime\\python.exe grab_login.py            # 两个都取
  runtime\\python.exe grab_login.py --tv       # 只取 TV
  runtime\\python.exe grab_login.py --flow     # 只取 Flow
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import browser_cookies as bc  # noqa: E402

DATA = os.path.join(BASE, "data")
TV_HOST = "dzkjm"
FLOW_BASE = "https://makerflow-agent.kvread.com"
JWT_RE = re.compile(rb"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")


def browser_running():
    try:
        out = subprocess.run(["tasklist"], capture_output=True, text=True, timeout=15,
                             encoding="utf-8", errors="replace").stdout.lower()
    except Exception:
        return []
    return [n for n in ("chrome.exe", "msedge.exe", "firefox.exe") if n in out]


def varint(b, i):
    r = 0
    s = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        if not c & 0x80:
            return r, i
        s += 7


def ldb_blocks(path):
    """解析 .ldb 里的所有数据块（含 snappy 解压）。"""
    buf = open(path, "rb").read()
    if len(buf) < 48:
        return []
    sys.path.insert(0, os.path.join(BASE, "..", "_tmp"))
    try:
        import ldb_read
    except Exception:
        return []
    try:
        return ldb_read.blocks_of(path)
    except Exception:
        return []


def leveldb_entries(path):
    out = []
    for blob in ldb_blocks(path):
        try:
            n, _ = varint(blob, len(blob) - 4)
            end = len(blob) - 4 - 4 * n
        except Exception:
            continue
        if end <= 0 or end > len(blob):
            continue
        key = b""
        i = 0
        while i < end:
            try:
                shared, i = varint(blob, i)
                non_shared, i = varint(blob, i)
                vlen, i = varint(blob, i)
            except IndexError:
                break
            if shared > len(key) or i + non_shared + vlen > end:
                break
            key = key[:shared] + blob[i:i + non_shared]
            i += non_shared
            val = blob[i:i + vlen]
            i += vlen
            out.append((key, val))
    return out


def flow_token_from_localstorage(profile):
    """从 Local Storage\leveldb 里找 maker-admin-token 的值。"""
    d = os.path.join(profile, "Default", "Local Storage", "leveldb")
    if not os.path.isdir(d):
        return None
    tmp = os.path.join(tempfile.gettempdir(), "lsdb_grab")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(d, tmp)
    for f in sorted(glob.glob(os.path.join(tmp, "*.ldb"))):
        for k, v in leveldb_entries(f):
            if k.endswith(b"maker-admin-token"):
                val = v[1:] if v[:1] in (b"\x00", b"\x01") else v
                if val:
                    return val.decode("utf-8", "replace")
    # 兜底：扫 log 与所有块里的 JWT，逐个试接口
    cands = []
    for f in sorted(glob.glob(os.path.join(tmp, "*.log"))) + sorted(glob.glob(os.path.join(tmp, "*.ldb"))):
        try:
            raw = open(f, "rb").read()
        except Exception:
            continue
        for m in JWT_RE.finditer(raw):
            cands.append(m.group(0).decode())
        for blob in ldb_blocks(f):
            for m in JWT_RE.finditer(blob):
                cands.append(m.group(0).decode())
    for tok in dict.fromkeys(cands):
        if token_ok(tok):
            return tok
    return None


def token_ok(token):
    req = urllib.request.Request(
        FLOW_BASE + "/api/admin/agent-skills?all=1&includeHidden=1",
        headers={"Authorization": "Bearer " + token, "User-Agent": "Mozilla/5.0",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tv", action="store_true")
    ap.add_argument("--flow", action="store_true")
    args = ap.parse_args()
    want_tv = args.tv or not (args.tv or args.flow)
    want_flow = args.flow or not (args.tv or args.flow)

    procs = browser_running()
    if procs:
        print("[先关浏览器] 检测到 " + "、".join(procs) + " 正在运行，"
              "浏览器的 Cookie 库与本地存储被独占锁住，读不了。")
        print("  请完全退出浏览器（关掉所有窗口，或任务管理器里结束 chrome.exe / msedge.exe）后重跑本脚本。")
        return 2

    os.makedirs(DATA, exist_ok=True)
    ok = True

    if want_tv:
        got = False
        for name in ("chrome", "edge"):
            try:
                items = bc.cookies_for(TV_HOST, bc.PROFILES[name])
            except Exception as e:
                print("[TV] %s 读取失败：%s" % (name, e))
                continue
            if not items:
                continue
            cookie = "; ".join("%s=%s" % (n, v) for _, n, v in items if v)
            with open(os.path.join(DATA, "tv_cookie.txt"), "w", encoding="utf-8") as f:
                f.write(cookie)
            print("[TV] 从 %s 取到 %d 条 Cookie，已写入 data/tv_cookie.txt" % (name, len(items)))
            got = True
            break
        ok = ok and got
        if not got:
            print("[TV] 没取到。确认这个浏览器登录过 https://dz-ailab.dzkjm.cn/admin-web/presets")

    if want_flow:
        got = False
        for name in ("chrome", "edge"):
            try:
                tok = flow_token_from_localstorage(bc.PROFILES[name])
            except Exception as e:
                print("[Flow] %s 读取失败：%s" % (name, e))
                continue
            if tok:
                with open(os.path.join(DATA, "flow_token.txt"), "w", encoding="utf-8") as f:
                    f.write(tok)
                print("[Flow] 取到 token（%d 字节，%s…），已写入 data/flow_token.txt" % (len(tok), tok[:12]))
                got = True
                break
        ok = ok and got
        if not got:
            print("[Flow] 没取到，本地存储里的 maker-admin-token 已失效或被清理。\n"
                  "       退路：Flow 后台按 F12 → Network → 任一条 /api/admin/ 请求右键 Copy as cURL 粘到「后台同步」页。")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
