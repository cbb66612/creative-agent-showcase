"""从本机 Chrome / Edge 的 Cookie 库里取指定域名的 Cookie（Windows）。

用途：TV 后台（dz-ailab.dzkjm.cn）与 Flow 后台（makerflow-agent.kvread.com）
的登录态是 HttpOnly Cookie，只能从浏览器自己的库里读。

原理：
  Local State 里 os_crypt.encrypted_key（base64、前缀 DPAPI）
    → Windows DPAPI 解密 → 32 字节 AES-256 主密钥
  每条 Cookie 的 encrypted_value 以 v10/v11 开头
    → 去掉 3 字节前缀，前 12 字节是 nonce，其余是 AES-256-GCM 密文+tag
  v20 是 Chrome 127+ 的 app-bound 加密，需要额外提权解绑，这里会明确报错。

用法：
  runtime\\python.exe browser_cookies.py --host dzkjm --out data/tv_cookie.txt
  runtime\\python.exe browser_cookies.py --host kvread --show
浏览器必须关闭（Chrome 运行时对 Cookies 库加了独占锁，读不了）。
"""

import argparse
import base64
import ctypes
import json
import os
import sqlite3
import shutil
import sys
import tempfile

from Crypto.Cipher import AES

BASE = os.path.dirname(os.path.abspath(__file__))

PROFILES = {
    "chrome": os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data"),
    "edge": os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Edge", "User Data"),
}


class CookieError(Exception):
    pass


# ---------------------------------------------------------------- DPAPI

def dpapi_unprotect(blob):
    """调 Windows DPAPI 解出明文（当前用户上下文）。"""
    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint32),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(blob, len(blob))
    blob_in = DATA_BLOB(len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise CookieError("DPAPI 解密失败（换用户或换机器后密钥会失效）")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def master_key(profile):
    path = os.path.join(profile, "Local State")
    if not os.path.exists(path):
        raise CookieError("找不到 Local State：%s" % path)
    with open(path, encoding="utf-8") as f:
        state = json.load(f)
    enc = state.get("os_crypt", {}).get("encrypted_key", "")
    if not enc:
        raise CookieError("Local State 里没有 os_crypt.encrypted_key")
    raw = base64.b64decode(enc)
    if not raw.startswith(b"DPAPI"):
        raise CookieError("encrypted_key 不是 DPAPI 格式")
    return dpapi_unprotect(raw[5:])


# ---------------------------------------------------------------- 解密

def decrypt(value, key):
    if not value:
        return ""
    if value[:3] in (b"v10", b"v11"):
        nonce, payload = value[3:15], value[15:]
        plain = AES.new(key, AES.MODE_GCM, nonce=nonce).decrypt(payload)
        # GCM 密文末尾 16 字节是 tag，加密原文里可能带一个 32 字节的前缀域
        return plain.rstrip(b"\x00").decode("utf-8", "replace")
    if value[:3] == b"v20":
        raise CookieError("这条 Cookie 是 Chrome 127+ 的 app-bound 加密（v20），"
                          "DPAPI 解不开；可以改用「Copy as cURL」或换 Edge 试。")
    return value.decode("utf-8", "replace")


# ---------------------------------------------------------------- 读库

def open_cookies_db(profile):
    src = os.path.join(profile, "Default", "Network", "Cookies")
    if not os.path.exists(src):
        # 老版本 Chrome 在 Default 根目录
        src = os.path.join(profile, "Default", "Cookies")
    if not os.path.exists(src):
        raise CookieError("找不到 Cookies 库：%s" % src)
    tmp = os.path.join(tempfile.gettempdir(), "cookies_copy.db")
    shutil.copy2(src, tmp)  # 浏览器没关时这里会抛 PermissionError / WinError 32
    return sqlite3.connect(tmp)


def cookies_for(host_sub, profile):
    key = master_key(profile)
    conn = open_cookies_db(profile)
    rows = conn.execute(
        "select host_key, name, value, encrypted_value from cookies "
        "where host_key like ? order by host_key, name", ("%" + host_sub + "%",)).fetchall()
    out = []
    for host, name, plain, enc in rows:
        if plain:
            out.append((host, name, plain))
        else:
            out.append((host, name, decrypt(enc, key)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True, help="域名片段，如 dzkjm / kvread")
    ap.add_argument("--browser", default="chrome", choices=sorted(PROFILES))
    ap.add_argument("--profile", default="", help="自定义 User Data 目录")
    ap.add_argument("--out", default="", help="把 Cookie 串写到文件（喂给 sync_tv.py）")
    ap.add_argument("--show", action="store_true", help="打印每条 Cookie 名与长度（不打印值）")
    args = ap.parse_args()

    profile = args.profile or PROFILES[args.browser]
    try:
        items = cookies_for(args.host, profile)
    except PermissionError:
        print("[失败] Cookies 库被浏览器占用。先完全退出 Chrome/Edge 再跑：\n"
              "   任务栏右键 → 关闭所有窗口；或 lan\\stop.bat 不影响它，需要手动关。")
        return 2
    except CookieError as e:
        print("[失败] " + str(e))
        return 2
    if not items:
        print("[空] 这个浏览器里没有 %s 的 Cookie。换 --browser edge 或先登录一次。" % args.host)
        return 1
    if args.show:
        for host, name, val in items:
            print("%-34s %-28s %d" % (host, name, len(val)))
    cookie = "; ".join("%s=%s" % (n, v) for _, n, v in items if v)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(cookie)
        print("[ok] 写入 %s（%d 条，%d 字节）" % (args.out, len(items), len(cookie)))
    elif not args.show:
        print(cookie)
    return 0


if __name__ == "__main__":
    sys.exit(main())
