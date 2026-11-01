"""用专用 Chrome 窗口的登录态抓 TV 后台数据。

TV 后台的会话 Cookie 是 Chrome 127+ 的 app-bound 加密（v20），
任何外部程序都解不开；只有在「同一个浏览器实例」里 Chrome 自己才认。
所以做法是：开一个专用 Chrome 窗口，你在里面登录一次，
我再通过调试端口在页面里 fetch。

用法：
  lan\\tv_login.bat                                              # 打开专用窗口并登录
  lan\\runtime\\python.exe tv_dump.py                            # 抓 4 个 JSON
  lan\\runtime\\python.exe sync_tv.py --from-dir data\\tv_raw     # 入库
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from cdp_fetch import WS  # noqa: E402

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PROFILE = os.path.join(BASE, "data", "chrome-tv")
RAW = os.path.join(BASE, "data", "tv_raw")
PORT = 9335
START_URL = "https://dz-ailab.dzkjm.cn/admin-web/presets"

FETCH_JS = r"""
(async () => {
  const API = '/admin/api';
  async function jget(path) {
    const r = await fetch(API + path, {headers: {'Accept': 'application/json'}});
    if (!r.ok) throw new Error(path + ' HTTP ' + r.status);
    return r.json();
  }
  function rowsOf(j) {
    const d = j && j.data;
    if (Array.isArray(d)) return d;
    if (d) return d.list || d.records || d.content || d.items || [];
    return [];
  }
  const feats = rowsOf(await jget('/features'));
  const cats = rowsOf(await jget('/preset-categories'));
  const skills = rowsOf(await jget('/skills?page=1&pageSize=500'));
  const presets = [];
  for (const f of feats) {
    const code = f.featureCode || f.code || f.id;
    const rows = rowsOf(await jget('/presets?featureCode=' + encodeURIComponent(code) + '&page=1&pageSize=500'));
    for (const r of rows) { r.__featureName = f.featureName; presets.push(r); }
  }
  return {counts: {features: feats.length, categories: cats.length, presets: presets.length, skills: skills.length},
          features: feats, categories: cats, presets: presets, skills: skills};
})()
"""


def http_json(url, timeout=4):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def debugger_up():
    try:
        http_json("http://127.0.0.1:%d/json/version" % PORT)
        return True
    except Exception:
        return False


def launch(headless=False):
    os.makedirs(PROFILE, exist_ok=True)
    argv = [CHROME, "--no-first-run", "--remote-debugging-port=%d" % PORT,
            "--user-data-dir=" + PROFILE, START_URL]
    if headless:
        argv[1:1] = ["--headless=new", "--disable-gpu", "--window-size=1400,1000"]
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(["cmd", "/c", "start", ""] + argv)
    for _ in range(30):
        time.sleep(0.5)
        if debugger_up():
            return True
    return False


def page_target(wait=30):
    end = time.time() + wait
    while time.time() < end:
        try:
            tabs = http_json("http://127.0.0.1:%d/json/list" % PORT)
        except Exception:
            time.sleep(0.5)
            continue
        for t in tabs:
            if t.get("type") == "page" and "dzkjm" in t.get("url", ""):
                return t
        for t in tabs:
            if t.get("type") == "page" and t.get("url", "").startswith("http"):
                return t
        time.sleep(0.5)
    return None


def evaluate(expr, timeout=180):
    target = page_target()
    if not target:
        raise RuntimeError("调试端口上没有可用页面（窗口没开？）")
    print("  页面：%s" % target.get("url", "")[:90])
    ws = WS(target["webSocketDebuggerUrl"])
    ws.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
    ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate", "params": {
        "expression": expr, "awaitPromise": True, "returnByValue": True,
        "timeout": timeout * 1000}}))
    end = time.time() + timeout
    while time.time() < end:
        msg = json.loads(ws.recv())
        if msg.get("id") == 2:
            res = msg.get("result", {})
            if "exceptionDetails" in res:
                raise RuntimeError("页面脚本报错：%s" % json.dumps(
                    res["exceptionDetails"], ensure_ascii=False)[:300])
            return res.get("result", {}).get("value")
    raise RuntimeError("等待页面返回超时")



LOGIN_JS = r"""
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const setVal = (el, v) => {
    const desc = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value');
    if (desc && desc.set) desc.set.call(el, v); else el.value = v;
    el.dispatchEvent(new Event('input', {bubbles: true}));
  };
  const inputs = [...document.querySelectorAll('input')].filter(i => i.offsetParent !== null);
  const user = inputs.find(i => ['text', 'tel', ''].includes((i.type || '').toLowerCase()));
  const pass = inputs.find(i => i.type === 'password');
  if (!user || !pass) return {ok: false, reason: 'NO_INPUT'};
  setVal(user, __USER__);
  setVal(pass, __PASS__);
  await sleep(300);
  const btn = [...document.querySelectorAll('button')].find(b => b.textContent.trim() === '登录');
  if (!btn) return {ok: false, reason: 'NO_BUTTON'};
  btn.click();
  await sleep(3000);
  return {ok: true, url: location.href};
})()
"""


def load_account():
    fp = os.path.join(BASE, "data", "tv_account.json")
    if not os.path.exists(fp):
        return None
    with open(fp, encoding="utf-8") as f:
        return json.load(f)


def try_login():
    acc = load_account()
    if not acc:
        print("  没有 data/tv_account.json，跳过自动登录")
        return False
    js = LOGIN_JS.replace("__USER__", json.dumps(acc["username"])).replace(
        "__PASS__", json.dumps(acc["password"]))
    print("  登录态失效，用本地保存的账号自动登录…")
    try:
        res = evaluate(js, timeout=60)
    except Exception as e:
        print("  自动登录失败：%s" % e)
        return False
    print("  表单提交结果：%s" % (res,))
    sess = evaluate("fetch('/admin/api/auth/session').then(r=>r.json()).catch(e=>({err:String(e)}))",
                    timeout=30)
    ok = isinstance(sess, dict) and bool(sess.get("data"))
    print("  %s" % ("登录成功" if ok else "登录后会话仍为空"))
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="抓完不关浏览器窗口")
    ap.add_argument("--headless", action="store_true", help="没有窗口时用无头模式启动（给定时任务用）")
    args = ap.parse_args()

    if not os.path.exists(CHROME):
        print("找不到 chrome.exe")
        return 2
    if not debugger_up():
        if args.headless:
            print("没有检测到专用窗口，正在用无头模式打开…")
        else:
            print("没有检测到专用窗口，正在打开…")
        if not launch(headless=args.headless):
            print("浏览器没起来（调试端口 %d 打不开）" % PORT)
            return 2
        if not args.headless:
            print("已打开。请在这个窗口里登录 TV 后台，然后再跑一次本脚本。")
            return 3
        time.sleep(2)
    print("调试端口已就绪（%d）" % PORT)
    data = evaluate(FETCH_JS)
    if not isinstance(data, dict):
        print("返回值异常：%r" % (data,))
        return 1
    if data.get("needLogin"):
        if try_login():
            time.sleep(1.5)
            data = evaluate(FETCH_JS)
        if not (isinstance(data, dict) and not data.get("needLogin")):
            print("自动登录没成功。请双击 lan\\tv_login.bat 手动登录一次，再点同步。")
            return 3
    os.makedirs(RAW, exist_ok=True)
    for k in ("features", "categories", "presets", "skills"):
        with open(os.path.join(RAW, k + ".json"), "w", encoding="utf-8") as f:
            json.dump(data.get(k, []), f, ensure_ascii=False)
    print("抓取完成：%s" % json.dumps(data.get("counts", {}), ensure_ascii=False))
    print("已写入 %s" % RAW)
    print("下一步：runtime\\python.exe sync_tv.py --from-dir data\\tv_raw")
    return 0


if __name__ == "__main__":
    sys.exit(main())
