"""后台同步页：把 TV 后台的预设 / Skill、Flow 后台的 Skill 拉进本地库。

界面只做两件事：粘贴登录态 → 点按钮 → 看结果。
登录态从哪来：
  TV   dz-ailab.dzkjm.cn  → F12 → Network → 任意 /api-data 请求 → Request Headers → Cookie
  Flow makerflow-agent.kvread.com → F12 → Console → localStorage.getItem('maker-admin-token')
"""

import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent


def _run(script, args, timeout=240):
    try:
        p = subprocess.run([sys.executable, str(BASE / script)] + list(args),
                           cwd=str(BASE), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        out = (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        out = "超时（%d 秒），可能数据太多，先在命令行跑一次看进度。" % timeout
    except Exception as e:  # noqa: BLE001
        out = "执行失败：%s" % e
    return (out or "").strip() or "（没有输出）"


CSS = """<style>
.sync-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}
@media (max-width:900px){.sync-grid{grid-template-columns:1fr}}
.sync-card{background:#fff;border:1px solid #e6e8f2;border-radius:14px;padding:20px 22px}
.sync-card h3{margin:0 0 6px;font-size:16px}
.sync-card p.hint{margin:0 0 12px;color:#787f92;font-size:12.5px;line-height:1.7}
.sync-card textarea{width:100%;min-height:92px;padding:10px 12px;border:1px solid #d8dceb;
  border-radius:10px;font:12.5px/1.6 ui-monospace,Consolas,monospace;resize:vertical}
.sync-card .row{display:flex;gap:10px;align-items:center;margin-top:12px}
.sync-out{margin-top:14px;background:#0f1222;color:#cfd6ff;border-radius:10px;padding:12px 14px;
  font:12.5px/1.65 ui-monospace,Consolas,monospace;white-space:pre-wrap;max-height:280px;overflow:auto}
.sync-badge{display:inline-block;font-size:11.5px;padding:2px 8px;border-radius:999px;
  background:#eef0ff;color:#4f46e5;margin-left:6px}
</style>"""


LABELS = {"tvwin": "抓取并同步 TV", "tv": "同步 TV", "flow": "同步 Flow"}


def _card(title, note, name, placeholder, button, out, badge=""):
    out_html = ('<div class="sync-out">%s</div>' % _esc(out)) if out else ""
    return (
        '<div class="sync-card">'
        '<h3>%s%s</h3>'
        '<p class="hint">%s</p>'
        '<textarea name="%s" placeholder="%s">%s</textarea>'
        '<div class="row"><button class="btn primary" type="submit" name="what" value="%s">%s</button>'
        '<span class="hint" style="margin:0">先干跑再正式同步更稳</span></div>'
        '%s</div>'
    ) % (title, badge, note, name, placeholder, "", button, LABELS.get(button, button), out_html)


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render(conn, user, form, shell):
    what = (form.get("what") or "").strip()
    tv_cookie = (form.get("tv_cookie") or "").strip()
    flow_token = (form.get("flow_token") or "").strip()
    dry = (form.get("dry") or "") == "1"
    tv_out = flow_out = ""

    if what == "tv":
        if not tv_cookie:
            tv_out = "还没填 Cookie。"
        else:
            (BASE / "data" / "tv_cookie.txt").write_text(tv_cookie, encoding="utf-8")
            args = ["--cookie", tv_cookie, "--only", "all"]
            if dry:
                args.append("--dry-run")
            tv_out = _run("sync_tv.py", args)
    elif what == "tvwin":
        nl = chr(10)
        out = _run("tv_dump.py", [], timeout=300)
        if "抓取完成" in out:
            extra = ["--dry-run"] if dry else []
            out = out + nl + nl + _run("sync_tv.py", ["--from-dir", "data/tv_raw"] + extra, timeout=300)
        else:
            out = out + nl + nl + "先去双击 lan" + chr(92) + "tv_login.bat 打开专用窗口，登录一次 TV 后台，再回来点这个按钮。"
        tv_out = out
    elif what == "flow":
        if not flow_token:
            flow_out = "还没填 token。"
        else:
            (BASE / "data" / "flow_token.txt").write_text(flow_token, encoding="utf-8")
            args = ["--token", flow_token]
            if dry:
                args.append("--dry-run")
            flow_out = _run("sync_flow.py", args)

    body = (
        CSS
        + '<div class="wrap page"><h1>和后台同步</h1>'
        '<p class="sub">把 TV 后台的预设/Skill、Flow 后台的 Skill 拉进本地库；'
        '同名（source_key）会整条更新，封面图会下载到 data/covers。</p>'
        '<form method="post" action="/admin/sync">'
        '<div class="sync-grid">'
        + _card("TV 后台（dz-ailab）",
                "TV 的登录态存在浏览器里、拷不出来（Chrome 的 app-bound 加密）。"
                "做法：双击 <code>lan/tv_login.bat</code> 打开专用窗口，登录一次，之后一直用它。"
                "登录好以后点右边按钮，自动抓取并入库。",
                "tv_cookie", "（不用填，留空即可）", "tvwin", tv_out,
                '<span class="sync-badge">预设 + Skill</span>')
        + _card("Flow 后台（makerflow）",
                "最省事：F12 → Network → 在任意一条 <code>/api/admin/</code> 请求上<b>右键 → Copy → "
                "Copy as cURL</b>，整段粘进来（会自动抽出 Bearer token）。"
                "也可以只粘 token 本身（ey 开头的长串）。",
                "flow_token", "eyJhbGciOi...", "同步 Flow", flow_out,
                '<span class="sync-badge">Skill 列表</span>')
        + "</div>"
        '<label class="hint" style="display:block;margin-top:16px">'
        '<input type="checkbox" name="dry" value="1"' + (" checked" if dry else "") + "> 只干跑，不写库</label>"
        "</form>"
        '<div class="panel" style="margin-top:20px"><div class="panel-head">跑完之后</div>'
        '<div class="panel-body hint" style="line-height:1.9">'
        "· 结果里「新增 N，更新 M」就是同步了多少条；<br>"
        '· 到 <a href="/resources?platform=tv">资源库 → TV 预设</a> 或 '
        '<a href="/resources?platform=flow">Flow</a> 看效果；<br>'
        "· 登录态会过期（TV 的 Cookie 一般几小时），失效时会提示 401，重新复制一份即可。"
        "</div></div></div>"
    )

    return shell("和后台同步", body, user, "/admin/sync")
