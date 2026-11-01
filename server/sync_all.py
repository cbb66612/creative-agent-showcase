"""一键同步：TV 后台 + Flow 后台 → 本地库。给定时任务用。

流程
  TV   : 用专用 Chrome 配置（data/chrome-tv）headless 起一个实例 → 打开后台
         → 没登录就用 data/tv_account.json 里的账号自动登录
         → 抓 presets / skills 四个 JSON → sync_tv.py --from-dir data/tv_raw
  Flow : 读 data/flow_token.txt → sync_flow.py（token 过期会提示重新登录）

日志：data/logs/sync-YYYYMMDD.log（同时打印到控制台）
用法：
  runtime\\python.exe sync_all.py             # 同步 TV + Flow
  runtime\\python.exe sync_all.py --tv-only
  runtime\\python.exe sync_all.py --flow-only
"""

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
LOGDIR = os.path.join(DATA, "logs")

def _safe_stdout():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


_safe_stdout()


def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"))
    os.makedirs(LOGDIR, exist_ok=True)
    path = os.path.join(LOGDIR, "sync-%s.log" % datetime.now().strftime("%Y%m%d"))
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run(script, args, timeout=600):
    t0 = time.time()
    try:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        p = subprocess.run([sys.executable, os.path.join(BASE, script)] + args,
                           cwd=BASE, capture_output=True, text=True, env=env,
                           encoding="utf-8", errors="replace", timeout=timeout)
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        code = p.returncode
    except subprocess.TimeoutExpired:
        out, code = "超时（%d 秒）" % timeout, -1
    except Exception as e:  # noqa: BLE001
        out, code = "执行失败：%s" % e, -1
    secs = round(time.time() - t0, 1)
    for ln in out.splitlines():
        if ln.strip():
            log("  " + ln.strip())
    log("  ↳ %s 退出码 %s，用时 %ss" % (script, code, secs))
    return code, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tv-only", action="store_true")
    ap.add_argument("--flow-only", action="store_true")
    args = ap.parse_args()

    log("==== 开始同步 ====")
    ok = True

    if not args.flow_only:
        code, out = run("tv_dump.py", ["--headless"], timeout=420)
        if code == 0 and "抓取完成" in out:
            c2, _ = run("sync_tv.py", ["--from-dir", "data/tv_raw"], timeout=420)
            ok = ok and c2 == 0
        else:
            log("  ! TV 抓取失败（退出码 %s），本次跳过" % code)
            ok = False

    if not args.tv_only:
        token_path = os.path.join(DATA, "flow_token.txt")
        if not os.path.exists(token_path):
            log("  ! 没有 data/flow_token.txt，跳过 Flow（在后台登录后复制一次 token）")
        else:
            c3, out3 = run("sync_flow.py", ["--with-packages"], timeout=900)
            if c3 != 0 and ("401" in out3 or "登录态失效" in out3):
                log("  ! Flow token 过期：请到后台重新登录，把新 token 覆盖到 data/flow_token.txt")
            ok = ok and c3 == 0

    log("==== 同步结束：%s ====" % ("全部成功" if ok else "有失败项，见上面日志"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
