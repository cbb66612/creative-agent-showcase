# -*- coding: utf-8 -*-
"""灵感引擎 · Skill 预设库 —— 局域网版（零依赖，Python 3.10+）。

在本机运行 app.py 后用浏览器访问
http://本机内网IP:8000 ，登录后即可浏览、上传、下载。

只用标准库：http.server / sqlite3 / zipfile / hashlib。
"""

import html
import json
import mimetypes
import os
import re
import datetime
import time
import socket
import sys
import traceback
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit, urlencode

# 便携版 Python（runtime\）不会自动把脚本目录加进 sys.path，这里显式加一次
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent  # noqa: E402
import ai  # noqa: E402
import md  # noqa: E402
import store  # noqa: E402
import sync_page  # noqa: E402

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
PORT = int(os.environ.get("PORT", "8000"))

esc = html.escape

_ASK_TS = {}   # 每个用户上次提问的时间，做 1.5 秒节流


def fmt_bytes(n):
    n = int(n or 0)
    if n < 1024:
        return "%d B" % n
    if n < 1024 * 1024:
        return "%.1f KB" % (n / 1024)
    if n < 1024 * 1024 * 1024:
        return "%.1f MB" % (n / 1024 / 1024)
    return "%.2f GB" % (n / 1024 / 1024 / 1024)


def fmt_time(s):
    return (s or "")[:16]


def qs(**kw):
    pairs = [(k, v) for k, v in kw.items() if v not in (None, "")]
    return ("?" + urlencode(pairs)) if pairs else ""


# --------------------------------------------------------------------------
# 页面外壳
# --------------------------------------------------------------------------

CSS = """
.plain-list{margin:0;padding:0;list-style:none}
.plain-list li{padding:10px 0;border-bottom:1px solid var(--line,#eef0f6);display:flex;gap:10px;align-items:center}
.plain-list li:last-child{border-bottom:0}
.form-row{display:flex;flex-direction:column;gap:6px;margin-bottom:14px}
.form-row label{font-size:13px;color:#5b6274;font-weight:600}
.form-row input[type=text],.form-row input[type=password],.form-row select,.form-row textarea{
  border:1px solid #dfe3ee;border-radius:10px;padding:10px 12px;font:inherit;background:#fff}
.form-row textarea{min-height:96px;resize:vertical}
.login-wrap{max-width:420px;margin:70px auto 40px}
.login-hero{text-align:center;margin-bottom:22px}
.login-hero .big{font-size:34px;font-weight:800;letter-spacing:-.5px}
.login-hero .sub{color:#6b7280;margin-top:6px}
.notice{border:1px solid #e6e9f5;background:#f7f8ff;border-radius:12px;padding:12px 14px;font-size:13px;color:#4b5563;margin-bottom:16px}
.err{color:#b91c1c;background:#fef2f2;border:1px solid #fecaca;border-radius:10px;padding:10px 12px;font-size:13px;margin-bottom:14px}
.ok{color:#166534;background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;padding:10px 12px;font-size:13px;margin-bottom:14px}
.inline{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.prompt-body{white-space:pre-wrap;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-size:13px;line-height:1.75;background:#0f172a;color:#e6edf7;border-radius:12px;padding:16px 18px;overflow:auto}
"""


def topbar(user, active):
    nav = [("/", "首页"), ("/resources", "资源库"), ("/agent", "Agent"), ("/me", "我的")]
    if store.is_admin(user):
        nav.append(("/insights", "数据概览"))
        nav.append(("/admin/users", "账号管理"))
        nav.append(("/admin/settings", "AI 设置"))
        nav.append(("/admin/sync", "后台同步"))
    nav_html = "".join(
        '<a href="%s" class="%s">%s</a>' % (href, "on" if active == href else "", label)
        for href, label in nav)
    role = "管理员" if store.is_admin(user) else ("成员 · 可维护" if user["can_maintain"] else "成员")
    addbtn = ('<a class="btn primary" href="/upload">＋ 新增资源</a>'
              if store.can_maintain(user) else "")
    initial = esc((user["name"] or "?")[:1])
    return (
        '<header class="topbar"><div class="wrap topbar-in">'
        '<a class="brand" href="/">'
        '<span class="brand-mark">剧</span>'
        '<span class="brand-text"><span class="brand-name">灵感引擎 Skill 预设库</span>'
        '<span class="brand-sub">Skill · 预设 · Agent</span></span></a>'
        '<nav class="nav">%s</nav>'
        '<div class="topbar-right">%s'
        '<span class="avatar" title="%s">%s</span>'
        '<a class="btn sm" href="/logout">退出</a>'
        '</div></div></header>'
        % (nav_html, addbtn, esc(user["name"] + " · " + role), initial))


KITTY_SVG = (
    '<svg viewBox="0 0 64 64" width="34" height="34" aria-hidden="true">'
    # 耳朵
    '<path d="M11 25 L9 9 L25 18 Z" fill="#ffffff" stroke="#2b2f3a" stroke-width="1.8" stroke-linejoin="round"/>'
    '<path d="M53 25 L55 9 L39 18 Z" fill="#ffffff" stroke="#2b2f3a" stroke-width="1.8" stroke-linejoin="round"/>'
    # 脸
    '<ellipse cx="32" cy="37" rx="22" ry="18.5" fill="#ffffff" stroke="#2b2f3a" stroke-width="1.8"/>'
    # 眼睛
    '<ellipse cx="24.2" cy="34.5" rx="2.7" ry="3.5" fill="#2b2f3a"/>'
    '<ellipse cx="39.8" cy="34.5" rx="2.7" ry="3.5" fill="#2b2f3a"/>'
    # 黄鼻子
    '<ellipse cx="32" cy="41.2" rx="2.8" ry="2.1" fill="#f7c948" stroke="#e0ae2f" stroke-width="0.8"/>'
    # 胡须（左右各三根）
    '<g stroke="#2b2f3a" stroke-width="1.5" stroke-linecap="round">'
    '<path d="M13.5 32 L4.5 29.5"/><path d="M13 37 L4 37"/><path d="M13.5 42 L4.5 44.5"/>'
    '<path d="M50.5 32 L59.5 29.5"/><path d="M51 37 L60 37"/><path d="M50.5 42 L59.5 44.5"/>'
    '</g>'
    # 红色蝴蝶结（她的右耳，画面左侧）
    '<g transform="translate(15.5,15) rotate(-18)">'
    '<path d="M0 0 L-9 -5.5 L-9 5.5 Z" fill="#e8436b"/>'
    '<path d="M0 0 L9 -5.5 L9 5.5 Z" fill="#e8436b"/>'
    '<circle cx="0" cy="0" r="3.1" fill="#f2638a"/>'
    '</g>'
    '</svg>'
)

MOTION_JS = (
    '<script>(function(){'
    'var rm=window.matchMedia&&matchMedia("(prefers-reduced-motion: reduce)").matches;'
    'if(rm) return;'
    'document.documentElement.classList.add("has-motion");'
    'var targets=[];'
    'function mark(sel,maxDelay,step){'
    '  var els=document.querySelectorAll(sel);'
    '  for(var i=0;i<els.length;i++){'
    '    var el=els[i]; if(el.closest(".reveal")) continue;'
    '    el.classList.add("reveal");'
    '    el.style.setProperty("--d", Math.min(maxDelay+i*step, 360)+"ms");'
    '    targets.push(el);}}'
    'mark(".hero .hero-kicker",0,0); mark(".hero h1",90,0); mark(".hero p",170,0);'
    'mark(".hero .searchbox",250,0); mark(".hero-chips .chip",310,24);'
    'mark(".section-head",80,0); mark(".kpi",40,45); mark(".panel",60,50);'
    'mark(".grid .rcard",40,42); mark(".lib .group",40,30);'
    'var io=("IntersectionObserver" in window)?new IntersectionObserver(function(es){'
    '  es.forEach(function(e){ if(e.isIntersecting){ e.target.classList.add("in"); io.unobserve(e.target);} });'
    '},{rootMargin:"0px 0px -8% 0px",threshold:0.06}):null;'
    'targets.forEach(function(el){'
    '  var r=el.getBoundingClientRect();'
    '  if(r.top < window.innerHeight*0.92){ requestAnimationFrame(function(){ el.classList.add("in"); }); }'
    '  else if(io){ io.observe(el); } else { el.classList.add("in"); }'
    '});'
    'setTimeout(function(){targets.forEach(function(el){el.classList.add("in");});},1400);'
    'var tb=document.querySelector(".topbar");'
    'function onScroll(){ if(!tb) return; tb.classList.toggle("scrolled", window.scrollY>8); }'
    'window.addEventListener("scroll", onScroll, {passive:true}); onScroll();'
    '})();</script>'
)

AI_PANEL = (
    '<div class="ai-fab" id="ai-fab" role="button" tabindex="0" aria-label="灵感引擎小助手" '
    'title="点我问问题 · 按住可以拖到任意位置">'
    '<img src="/static/kitty.png?v=2" alt="小助手"></div>'
    '<div class="ai-panel" id="ai-panel" hidden>'
    '<div class="ai-head"><div><b>灵感引擎助手</b>'
    '<span>基于库内资源回答 · 会标注来源</span></div>'
    '<button class="ai-x" id="ai-close" title="关闭">×</button></div>'
    '<div class="ai-body" id="ai-body">'
    '<div class="ai-msg bot"><p>我是灵感引擎助手。可以帮你找资源、解释 Skill 规则、'
    '按你的需求挑预设。回答只依据库里的内容，并标注来源。</p></div></div>'
    '<form class="ai-form" id="ai-form">'
    '<input id="ai-input" placeholder="例如：有没有生成角色三视图的预设？" autocomplete="off">'
    '<button class="btn primary" type="submit">发送</button></form>'
    '<div class="ai-foot" id="ai-foot"></div></div>'
)

AI_JS = (
    '<script>(function(){'
    'var fab=document.getElementById("ai-fab");if(!fab)return;'
    'var panel=document.getElementById("ai-panel"),body=document.getElementById("ai-body");'
    'var form=document.getElementById("ai-form"),input=document.getElementById("ai-input");'
    'var close=document.getElementById("ai-close");'
    'function open(){panel.hidden=false;try{placePanel();}catch(e){}setTimeout(function(){input.focus();},50);}'
    'function shut(){panel.hidden=true;}'
    'var KEY="jbFabPos",drag=false,moved=false,sx=0,sy=0,ox=0,oy=0;'
    'function clamp(v,a,b){return Math.max(a,Math.min(b,v));}'
    'function place(x,y){var w=fab.offsetWidth,h=fab.offsetHeight;'
    'fab.style.left=clamp(x,8,innerWidth-w-8)+"px";fab.style.top=clamp(y,8,innerHeight-h-8)+"px";'
    'fab.style.right="auto";fab.style.bottom="auto";}'
    'try{var sv=JSON.parse(localStorage.getItem(KEY)||"null");if(sv)place(sv.x,sv.y);}catch(e){}'
    'function placePanel(){var w=Math.min(400,innerWidth-32),h=Math.min(560,innerHeight-140);'
    'var r=fab.getBoundingClientRect();var left=clamp(r.left+r.width/2-w/2,12,innerWidth-w-12);'
    'var top=r.top-h-14;if(top<12)top=r.bottom+14;panel.style.width=w+"px";panel.style.height=h+"px";'
    'panel.style.left=left+"px";panel.style.top=clamp(top,12,innerHeight-h-12)+"px";'
    'panel.style.right="auto";panel.style.bottom="auto";}'
    'fab.addEventListener("pointerdown",function(e){if(e.button!==0)return;'
    'var r=fab.getBoundingClientRect();ox=r.left;oy=r.top;sx=e.clientX;sy=e.clientY;drag=true;'
    'moved=false;fab.classList.add("dragging");try{fab.setPointerCapture(e.pointerId);}catch(err){}});'
    'fab.addEventListener("pointermove",function(e){if(!drag)return;var dx=e.clientX-sx,dy=e.clientY-sy;'
    'if(Math.abs(dx)+Math.abs(dy)>4)moved=true;if(moved)place(ox+dx,oy+dy);});'
    'function drop(){if(!drag)return;drag=false;fab.classList.remove("dragging");'
    'if(moved){try{localStorage.setItem(KEY,JSON.stringify({x:parseFloat(fab.style.left),'
    'y:parseFloat(fab.style.top)}));}catch(err){}setTimeout(function(){moved=false;},60);}}'
    'fab.addEventListener("pointerup",drop);fab.addEventListener("pointercancel",drop);'
    'fab.addEventListener("click",function(){if(moved){moved=false;return;}'
    'if(panel.hidden){open();placePanel();}else{shut();}});'
    'fab.addEventListener("keydown",function(e){if(e.key==="Enter"||e.key===" "){e.preventDefault();'
    'if(panel.hidden){open();placePanel();}else{shut();}}});'
    'close.addEventListener("click",shut);'
    'var mm=location.search.match(/[?&]ask=([^&]+)/);'
    'if(mm){open();input.value=decodeURIComponent(mm[1]);setTimeout(function(){'
    'form.dispatchEvent(new Event("submit"));},400);}'
    'function add(cls,text){var d=document.createElement("div");d.className="ai-msg "+cls;'
    'var p=document.createElement("p");p.textContent=text;d.appendChild(p);'
    'body.appendChild(d);body.scrollTop=body.scrollHeight;return d;}'
    'form.addEventListener("submit",function(ev){ev.preventDefault();'
    'var q=input.value.trim();if(!q)return;add("me",q);input.value="";'
    'var t=add("bot","正在查库…");t.classList.add("thinking");'
    'fetch("/api/ask",{method:"POST",headers:{"Content-Type":"application/json"},'
    'body:JSON.stringify({q:q})}).then(function(r){return r.json();}).then(function(d){'
    't.classList.remove("thinking");t.querySelector("p").textContent=d.answer||"没有回答";'
    'if(d.sources&&d.sources.length){var box=document.createElement("div");box.className="ai-src";'
    'd.sources.forEach(function(s){var a=document.createElement("a");a.href=s.url;'
    'a.textContent="["+s.n+"] "+s.title;box.appendChild(a);});t.appendChild(box);}'
    'body.scrollTop=body.scrollHeight;}).catch(function(e){t.classList.remove("thinking");'
    't.querySelector("p").textContent="请求失败："+e;});});'
    '})();</script>'
)


def shell(title, body, user=None, active="", head_extra=""):
    head = ('<link rel="stylesheet" href="/static/style.css?v=67"><style>%s</style>%s'
            % (CSS, head_extra))
    bar = topbar(user, active) if user else ""
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta http-equiv="Cache-Control" content="no-store, must-revalidate">'
        '<meta http-equiv="Pragma" content="no-cache">'
        '<title>%s · 灵感引擎</title>%s</head><body>%s<main id="app">%s</main>'
        '<footer class="footer"><div class="wrap">灵感引擎 · Skill 预设库 · 个人项目展示 · '
        '文件保存在服务器 data/ 目录</div></footer>%s'
        '<script>'
        'document.addEventListener("click",function(e){'
        'var tg=e.target.closest("[data-toggle]");'
        'if(tg){e.preventDefault();var mode=tg.getAttribute("data-toggle");'
        'if(mode==="side"){var dz=document.querySelector(".ext-groups");'
        'if(dz){var dh=dz.style.display==="none";dz.style.display=dh?"":"none";'
        'tg.classList.toggle("closed",!dh);}return;}'
        'var grp=tg.closest(".group");if(!grp)return;'
        'grp.classList.toggle("closed");tg.classList.toggle("closed");'
        'return;}'
        'var b=e.target.closest("[data-copy]");if(!b)return;'
        'var el=document.getElementById(b.getAttribute("data-copy"));if(!el)return;'
        'var t=el.innerText;'
        'function ok(){var old=b.textContent;b.textContent="已复制";setTimeout(function(){b.textContent=old},1200);}'
        'if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(t).then(ok).catch(fb);}else{fb();}'
        'function fb(){var ta=document.createElement("textarea");ta.value=t;document.body.appendChild(ta);'
        'ta.select();try{document.execCommand("copy");ok();}catch(err){}document.body.removeChild(ta);}'
        '});'
        '</script>'
        '<script>'
        '(function(){var b=document.querySelector(".dpage-bar");if(!b)return;'
        'function fit(){var gap=document.documentElement.scrollHeight-window.innerHeight;'
        'b.style.display=(gap<120)?"none":"";}'
        'fit();window.addEventListener("resize",fit);window.addEventListener("load",fit);'
        'document.addEventListener("toggle",fit,true);setTimeout(fit,300);})();'
        '</script>'
        '<script>'
        '(function(){'
        'var side=document.getElementById("lib-side"),main=document.getElementById("lib-main");'
        'if(!side||!main)return;'
        'function paint(){var n=document.querySelectorAll("#lib-main .reveal");'
        'for(var i=0;i<n.length;i++)n[i].classList.add("in");}'
        'function go(href,push){'
        'var st=side.scrollTop;'
        'fetch(href,{headers:{"X-Partial":"1"}}).then(function(r){return r.text();}).then(function(html){'
        'var doc=new DOMParser().parseFromString(html,"text/html");'
        'var nm=doc.getElementById("lib-main"),ns=doc.getElementById("lib-side");'
        'if(!nm||!ns){location.href=href;return;}'
        'var sy=window.scrollY;'
        'main.innerHTML=nm.innerHTML;side.innerHTML=ns.innerHTML;side.scrollTop=st;'
        'window.scrollTo(0,sy);paint();'
        'if(push!==false)history.pushState({}, "", href);'
        '}).catch(function(){location.href=href;});}'
        'document.addEventListener("click",function(e){'
        'var a=e.target.closest("a");if(!a)return;'
        'var href=a.getAttribute("href")||"";'
        'if(href.indexOf("/resources")!==0)return;'
        'if(e.metaKey||e.ctrlKey||e.shiftKey||a.target==="_blank")return;'
        'if(!a.closest("#lib-side")&&!a.closest("#lib-main"))return;'
        'e.preventDefault();go(href);});'
        'window.addEventListener("popstate",function(){go(location.pathname+location.search,false);});'
        'paint();'
        '})();'
        '</script>%s</body></html>'
        % (esc(title), head, bar, body, (AI_PANEL + AI_JS) if user else "", MOTION_JS))


# --------------------------------------------------------------------------
# 卡片与详情
# --------------------------------------------------------------------------

def wf_tag(r):
    """Flow 资源显示所属团队流程（渠道）。"""
    try:
        if r["platform"] != "flow":
            return ""
        wf = r["flow_workflow"] or ""
    except Exception:
        return ""
    label = store.WORKFLOW_LABEL.get(wf, "")
    if not label or label == "通用默认":
        return ""
    return '<span class="tag wf">%s</span>' % esc(label)


def skill_card(r, version=None):
    v = ("v%d" % version["seq"]) if version else ""
    tags = " ".join('<span class="tag plain">%s</span>' % esc(t) for t in (r["tags"] or "").split() if t)
    size = ""
    if version:
        size = "%d 个文件 · %s" % (version["file_count"], fmt_bytes(version["bytes"]))
    elif (r["body"] or "").strip():
        size = "文本 %d 字" % len(r["body"])
    else:
        size = "包内容待上传"
    return (
        '<a class="rcard skill" href="/r/%s">'
        '<div class="rcard-head"><div class="skill-icon">📦</div>'
        '<div style="min-width:0;flex:1">'
        '<div class="rcard-title"><span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">%s</span>'
        '%s<span class="tag skill">SKILL</span></div>'
        '<div class="skill-meta"><span class="mono">%s</span><span>%s</span><span>%s</span></div></div></div>'
        '<div class="rcard-body"><div class="rcard-desc">%s</div>'
        '<div class="skill-meta" style="gap:4px">%s%s</div></div>'
        '<div class="rcard-foot"><span class="cat">%s</span><span>%s</span>'
        '<span class="go">查看 →</span></div></a>'
        % (r["id"], esc(r["title"]),
           ('<span class="tag">未上架</span>' if r["platform"] == "ext"
            else ("" if r["owner_id"] else '<span class="tag">官方</span>')),
           esc(r["external_key"] or ""), esc(v), esc(fmt_time(r["updated_at"])),
           esc(r["summary"] or ""), tags, wf_tag(r), esc(r["cat"] or ""), esc(size)))


def preset_card(r):
    keys = r.keys()
    cover = r["cover_url"] if "cover_url" in keys else ""
    ext = ("platform" in keys and r["platform"] == "ext")
    if cover:
        head = ('<img class="cover-img" src="%s" alt="" loading="lazy">' % esc(cover))
    else:
        head = '<span class="emoji">%s</span>' % esc(r["emoji"] or "📄")
    badge = '<span class="badge-official">未上架</span>' if ext else '<span class="badge-official">官方</span>'
    use = ('<span class="badge-use">🔥 %d</span>' % (r["usage_count"] or 0)
           if (r["usage_count"] or 0) > 0 else "")
    foot = "查看全文" if ext else "使用此预设"
    return (
        '<a class="rcard" href="/r/%d">'
        '<div class="rcard-cover" style="--hue:%d">%s%s%s</div>'
        '<div class="rcard-body"><div class="rcard-title">'
        '<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">%s</span></div>'
        '<div class="rcard-desc">%s</div></div>'
        '<div class="rcard-foot"><span class="tag preset">%s</span><span class="cat">%s</span>'
        '<span class="go">%s</span></div></a>'
        % (r["id"], r["hue"] or 215, head, badge, use,
           esc(r["title"]), esc(r["summary"] or ""), esc(r["grp"]), esc(r["cat"] or ""), foot))



def card(r, conn):
    if r["kind"] == "skill":
        v = store.get_version(conn, r["current_version_id"]) if r["current_version_id"] else None
        return skill_card(r, v)
    return preset_card(r)


def render_md_for(conn, version, text, current_path):
    files = {f["path"] for f in store.get_files(conn, version["id"])}

    def resolve(href):
        href = href.split("#")[0].strip()
        if not href:
            return None
        base = href.split("/")[-1]
        if href in files:
            return "?p=" + quote(href)
        for f in files:
            if f.split("/")[-1] == base:
                return "?p=" + quote(f)
        return None

    return md.render(text, resolve)


def file_tree(files, current):
    groups = {}
    order = []
    for f in files:
        d = f["path"].rsplit("/", 1)[0] if "/" in f["path"] else ""
        if d not in groups:
            groups[d] = []
            order.append(d)
        groups[d].append(f)
    out = []
    for d in order:
        items = groups[d]
        label = d if d else "根目录"
        out.append('<div class="tree-dir">%s · %d</div>' % (esc(label), len(items)))
        for f in items:
            name = f["path"][len(d) + 1:] if d else f["path"]
            on = "on" if f["path"] == current else ""
            out.append(
                '<a class="tree-file %s" href="?p=%s"><span class="dot"></span>'
                '<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">%s</span>'
                '<span class="sz">%s</span></a>'
                % (on, quote(f["path"]), esc(name), esc(fmt_bytes(f["size"]) if f["is_md"] else "文件")))
    return "".join(out)


def skill_detail(conn, r, query, user):
    versions = store.get_versions(conn, r["id"])
    if not versions:
        return ('<div class="wrap detail"><div class="empty"><div class="big">📦</div>'
                '这个 Skill 还没有上传 ZIP 包。</div></div>')
    cur = versions[0]
    files = store.get_files(conn, cur["id"])
    want = (query.get("p") or [""])[0]
    names = [f["path"] for f in files]
    current = want if want in names else ("SKILL.md" if "SKILL.md" in names else (names[0] if names else ""))
    raw = store.read_version_file(cur, current) if current else ""

    fm = {}
    body = raw or ""
    if body.startswith("---"):
        end = body.find("\n---", 3)
        if end > 0:
            block = body[3:end]
            body = body[end + 4:].lstrip("\n")
            for line in block.replace("\r", "").split("\n"):
                if ":" in line and not line.strip().startswith("#"):
                    k, v = line.split(":", 1)
                    fm[k.strip()] = v.strip()

    fm_html = ""
    if fm:
        rows = "".join('<div class="k">%s</div><div class="v">%s</div>' % (esc(k), esc(v))
                       for k, v in list(fm.items())[:5])
        fm_html = '<div class="fm">%s</div>' % rows

    body_html = render_md_for(conn, cur, body, current) if body else \
        '<div class="empty" style="border:0">（该文件不是文本，无法在线预览，可下载原包查看）</div>'

    version_rows = []
    for v in versions:
        is_cur = v["id"] == cur["id"]
        version_rows.append(
            '<div class="vrow"><span class="vbadge %s">v%d</span>'
            '<div class="vinfo"><span class="vtitle">%s%s</span>'
            '<span class="vsub">%s · %d 个文件 · %s · %s</span></div>'
            '<div class="vact"><a class="btn sm" href="/download/%d?v=%d">下载原包</a></div></div>'
            % ("cur" if is_cur else "", v["seq"], esc(v["note"] or ""),
               " · 当前版本" if is_cur else "", esc(fmt_time(v["created_at"])),
               v["file_count"], fmt_bytes(v["bytes"]), esc(v["zip_name"] or ""), r["id"], v["seq"]))

    uploader_top = ('<a class="btn big ghost-strong" href="/upload?rid=%d"><span class="ic">↑</span>上传新版本</a>' % r["id"]) if store.can_maintain(user) else ""
    uploader = ""
    _fb_up = ('<a class="btn big ghost-strong" href="/upload?rid=%d"><span class="ic">↑</span>上传新版本</a>'
              % r["id"]) if store.can_maintain(user) else ""
    floatbar = ('<div class="dpage-bar">' + _fb_up +
                '<a class="btn primary big" href="/download/%d"><span class="ic">↓</span>下载原包</a></div>'
                % r["id"])

    insight = store.get_insight(conn, cur["id"], r["id"]) or {}
    purpose = (insight.get("purpose") or r["summary"] or "").strip()

    def _ul(items, fallback=""):
        items = [x for x in (items or []) if str(x).strip()]
        if not items:
            return ('<p class="sk-empty">%s</p>' % esc(fallback)) if fallback else '<p class="sk-empty">—</p>'
        return "<ul>" + "".join("<li>%s</li>" % esc(str(x)) for x in items) + "</ul>"

    sk_grid = (
        '<div class="sk-grid">'
        '<div class="sk-cell"><h4>输入</h4>' + _ul(insight.get("inputs"), "用户提供剧本 / 素材") + '</div>'
        '<div class="sk-cell"><h4>输出</h4>' + _ul(insight.get("outputs"), "生成结果与文档") + '</div>'
        '<div class="sk-cell"><h4>给谁用</h4>' + _ul(insight.get("audience"), "使用者") + '</div>'
        '<div class="sk-cell"><h4>什么时候用</h4><p>%s</p></div>'
        % esc(insight.get("when") or "需要完成这一步工作时")
        + '</div>')

    # ---- 版本对比 ----
    prev = versions[1] if len(versions) > 1 else None
    diff_html = ""
    if prev:
        cur_files = {f["path"]: f["size"] for f in store.get_files(conn, cur["id"])}
        prev_files = {f["path"]: f["size"] for f in store.get_files(conn, prev["id"])}
        added = [p for p in cur_files if p not in prev_files]
        removed = [p for p in prev_files if p not in cur_files]
        changed = [p for p in cur_files if p in prev_files and cur_files[p] != prev_files[p]]
        delta = (cur["bytes"] or 0) - (prev["bytes"] or 0)
        bits = []
        if added:
            bits.append('<span class="up">+%d 新增</span>' % len(added))
        if removed:
            bits.append('<span class="del">−%d 移除</span>' % len(removed))
        if changed:
            bits.append('<span class="mod">%d 修改</span>' % len(changed))
        vol = ('<span class="%s">体积 %s%s</span>'
               % ("up" if delta > 0 else ("del" if delta < 0 else "same"),
                  "+" if delta > 0 else "", fmt_bytes(abs(delta))))
        sample = ""
        if added:
            sample = ('<div class="vd-files">新增：%s%s</div>'
                      % (esc("、".join(added[:4])), " 等" if len(added) > 4 else ""))
        if removed:
            sample += ('<div class="vd-files del">移除：%s%s</div>'
                       % (esc("、".join(removed[:4])), " 等" if len(removed) > 4 else ""))
        if not bits:
            bits.append('<span class="same">文件结构无变化</span>')
        diff_html = ('<div class="vd"><div class="vd-head">相比 v%d' % prev["seq"] +
                     '<span class="muted">（%s）</span></div>' % esc(fmt_time(prev["created_at"])) +
                     '<div class="vd-stats">' + "".join(bits) + vol + '</div>' + sample + '</div>')
    else:
        diff_html = ('<div class="vd"><div class="vd-head">这是第一版</div>'
                     '<div class="vd-stats"><span class="same">没有更早的版本可对比</span></div></div>')

    version_rows = []
    for i, v in enumerate(versions):
        is_cur = v["id"] == cur["id"]
        version_rows.append(
            '<div class="vrow%s"><span class="vbadge %s">v%d</span>'
            '<div class="vinfo"><span class="vtitle">%s%s</span>'
            '<span class="vsub">%s · %d 个文件 · %s</span></div>'
            '<div class="vact"><a class="btn sm" href="/download/%d?v=%d">下载</a></div></div>'
            % (" cur" if is_cur else "", "cur" if is_cur else "", v["seq"],
               esc(v["note"] or ("当前版本" if is_cur else "")),
               " · 当前" if is_cur else "", esc(fmt_time(v["created_at"])),
               v["file_count"], fmt_bytes(v["bytes"]), r["id"], v["seq"]))
        if is_cur and len(versions) > 1:
            pass

    uploader_top = ('<a class="btn big ghost-strong" href="/upload?rid=%d"><span class="ic">↑</span>上传新版本</a>' % r["id"]) if store.can_maintain(user) else ""
    _fb_up = ('<a class="btn big ghost-strong" href="/upload?rid=%d"><span class="ic">↑</span>上传新版本</a>'
              % r["id"]) if store.can_maintain(user) else ""
    floatbar = ('<div class="dpage-bar">' + _fb_up +
                '<a class="btn primary big" href="/download/%d"><span class="ic">↓</span>下载原包</a></div>'
                % r["id"])

    hero = (
        '<div class="sk-hero">'
        '<div class="sk-hero-main">'
        '<h1>' + esc(r["title"]) + ' <span class="tag skill">SKILL</span></h1>'
        '<div class="meta"><code>' + esc(r["external_key"] or "") + '</code>'
        '<span class="vchip">当前 v' + str(cur["seq"]) + '</span>'
        '<span>更新于 ' + esc(fmt_time(r["updated_at"])) + '</span>'
        '<span>' + str(cur["file_count"]) + ' 个文件 · ' + fmt_bytes(cur["bytes"]) + '</span>'
        '<span>' + esc(r["grp"] or "") + ' / ' + esc(r["cat"] or "") + '</span>' +
        (('<span class="vchip">' + esc(store.WORKFLOW_LABEL.get(r["flow_workflow"] or "", "")) + '</span>')
         if (r["platform"] == "flow" and (r["flow_workflow"] or "")) else "") + '</div>'
        + ('<p class="sk-purpose">' + esc(purpose) + '</p>' if purpose else "") +
        '</div>'
        '<div class="dhead-actions">' + uploader_top +
        '<a class="btn primary big" href="/download/' + str(r["id"]) + '"><span class="ic">↓</span>下载原包</a>'
        '</div></div>')

    raw_block = (
        '<details class="panel sk-raw">'
        '<summary>完整原文<span class="muted"> ' + str(len(files)) + ' 个文件 · 点开看 SKILL.md 与 references</span></summary>'
        '<div class="dgrid"><aside class="tree"><div class="tree-head">包内文件 · ' + str(len(files)) + '</div>'
        '<div class="tree-head muted" style="padding-bottom:10px">点击切换阅读</div>' + file_tree(files, current) + '</aside>'
        '<main><div class="panel"><div class="panel-head"><span class="file">' + esc(current) + '</span>'
        '<span class="tag plain">只读原文</span><div class="right"><span class="muted">' + str(len(body)) + ' 字</span></div></div>'
        '<div class="panel-body">' + fm_html + '<div class="md">' + body_html + '</div></div></div></main></div>'
        '</details>')

    ver_panel = (
        '<div class="panel sk-ver"><div class="panel-head">版本迭代'
        '<span class="muted">共 ' + str(len(versions)) + ' 个版本 · 历史只读</span></div>'
        '<div class="panel-body"><div class="ver-cols"><div>' + diff_html + '</div>'
        '<div class="ver-list">' + "".join(version_rows) + '</div></div></div></div>')

    return (
        '<div class="wrap detail">'
        '<div class="crumb"><a href="/resources">资源库</a><span>/</span>'
        '<a href="/resources' + qs(platform=r["platform"]) + '">' + esc(r["grp"] or "资源") + '</a>'
        '<span>/</span><span>' + esc(r["title"]) + '</span></div>'
        + hero + sk_grid + ver_panel + raw_block +
        '</div>' + floatbar)


def preset_detail(conn, r, user):
    """预设详情：组内预设用大头图，未上架资源用小头图 + 信息并排。"""
    is_ext = r["platform"] == "ext"
    body = (r["body"] or "").strip() or "（这条还没有正文）"
    tag_html = "".join('<span class="tag">%s</span>' % esc(t) for t in (r["tags"] or "").split())
    tags = ('<div class="tags" style="display:flex;gap:6px;flex-wrap:wrap">%s</div>' % tag_html) if tag_html else ""
    badge = '<span class="tag plain">未上架</span>' if is_ext else '<span class="tag plain">官方</span>'
    use = "" if is_ext else ('<span class="pill use">使用 %d 次</span>' % int(r["usage_count"] or 0))
    back = '<a class="btn" href="/resources%s">← 返回列表</a>' % qs(platform=r["platform"])
    copybtn = '<button class="btn primary" data-copy="prompt-body">复制全文</button>'

    crumb_platform = "未上架" if is_ext else ("TV" if r["platform"] == "tv" else "Flow")
    crumb = ('<div class="crumb"><a href="/resources">资源库</a><span>/</span>'
             '<a href="/resources%s">%s</a><span>/</span><span>%s</span></div>'
             % (qs(platform=r["platform"]), esc(crumb_platform), esc(r["title"])))

    if is_ext:
        if r["cover_url"]:
            cover = ('<div class="ext-cover" style="background-image:url(%s)"></div>' % esc(r["cover_url"]))
        else:
            cover = ('<div class="ext-cover fallback" style="--hue:%d"><span class="emoji">%s</span></div>'
                     % (r["hue"] or 215, esc(r["emoji"] or "◆")))
        head = ('<div class="ext-head">' + cover + '<div class="ext-info">'
                '<div class="ext-top"><span class="tag plain">未上架</span></div>'
                '<h1>' + esc(r["title"]) + '</h1>'
                '<div class="meta"><span>更新于 ' + esc(fmt_time(r["updated_at"])) + '</span>'
                '<span>' + esc(r["grp"] or "未分类") + '</span></div>'
                + tags +
                '<div class="ext-actions">' + back + copybtn + '</div>'
                '</div></div>')
    else:
        if r["cover_url"]:
            hero_style = ' style="background-image:url(%s);background-size:cover;background-position:center"' % esc(r["cover_url"])
            hero_inner = ""
        else:
            hue = int(r["hue"] or 215)
            hero_style = (' style="background:radial-gradient(120% 140% at 20% 0%, hsl(' + str(hue) +
                          ' 90% 86%), rgba(255,255,255,0) 60%), linear-gradient(135deg, hsl(' +
                          str(hue) + ' 70% 62%), hsl(' + str((hue + 30) % 360) + ' 65% 45%))"')
            hero_inner = '<span style="position:absolute;left:50%%;top:50%%;transform:translate(-50%%,-50%%);font-size:64px;opacity:.9">%s</span>' % esc(r["emoji"] or "●")
        head = ('<div class="preset-hero tall"' + hero_style + '>'
                '<div class="hero-shade"></div>' + hero_inner + '<span style="position:absolute;left:16px;top:16px" class="tag plain">' + ('未上架' if is_ext else '官方') + '</span>' + use +
                '<div class="hero-caption"><h1>' + esc(r["title"]) + '</h1>'
                '<div class="meta">' + tags + '</div></div></div>'
                '<div class="dhead" style="align-items:center"><div class="meta">'
                '<span>更新于 ' + esc(fmt_time(r["updated_at"])) + '</span></div>'
                '<div class="dhead-actions">' + back + copybtn + '</div></div>')

    if is_ext:
        kv_pairs = [("功能", r["grp"] or ""), ("分类", r["cat"] or "未分类"),
                    ("来源", r["source_url"] or r["source"] or ""),
                    ("版本", "未上架 · 无版本列表")]
    else:
        kv_pairs = [("功能", r["grp"] or ""), ("分类", r["cat"] or "未分类"),
                    ("使用次数", "%d 次" % int(r["usage_count"] or 0)),
                    ("版本", "无版本列表，改动只留操作记录")]
    kv = '<div class="kv">' + "".join(
        '<div class="k">%s</div><div class="v">%s</div>' % (esc(k), esc(v)) for k, v in kv_pairs) + '</div>'

    return ('<div class="wrap detail">' + crumb + head +
            '<div class="two-col" style="display:grid;grid-template-columns:1.35fr .9fr;gap:18px">'
            '<div class="panel"><div class="panel-head">提示词全文'
            '<span class="muted">只读 · 复制使用</span></div>'
            '<div class="panel-body"><div class="prompt-body" id="prompt-body">' + esc(body) + '</div></div></div>'
            '<div class="panel"><div class="panel-head">描述</div><div class="panel-body">'
            '<p>' + esc(r["summary"] or "") + '</p>' + kv + '</div></div></div></div>')


def split_frontmatter(text):
    """把 YAML frontmatter 从正文里摘出来，返回 (dict, body)。"""
    fm = {}
    body = text or ""
    if body.startswith("---"):
        end = body.find("\n---", 3)
        if end > 0:
            block = body[3:end]
            body = body[end + 4:].lstrip("\n")
            for line in block.replace("\r", "").split("\n"):
                if ":" in line and not line.strip().startswith("#"):
                    k, v = line.split(":", 1)
                    fm[k.strip()] = v.strip()
    return fm, body

def ext_skill_detail(conn, r, user):
    """未上架导入的 Skill：只有正文（没有 ZIP 包、没有版本历史）"""
    fm, body = split_frontmatter(r["body"] or "")
    fm_html = ""
    if fm:
        rows = "".join('<div class="k">%s</div><div class="v">%s</div>' % (esc(k), esc(v))
                       for k, v in list(fm.items())[:5])
        fm_html = '<div class="fm" style="margin-bottom:16px">%s</div>' % rows
    tags = " ".join('<span class="tag plain">%s</span>' % esc(t)
                    for t in (r["tags"] or "").split() if t)
    if r["source_url"]:
        src = ('<a href="%s" target="_blank" rel="noreferrer">%s</a>'
               % (esc(r["source_url"]), esc(r["source_url"])))
    else:
        src = "—"
    return (
        '<div class="wrap detail">'
        '<div class="crumb"><a href="/resources">资源库</a><span>/</span>'
        '<a href="/resources%s">未上架</a><span>/</span><span>%s</span></div>'
        '<div class="dhead"><div style="flex:1;min-width:0"><h1>%s</h1>'
        '<div class="meta"><span class="tag skill">未上架 Skill</span>'
        '<span class="tag plain">%s</span>%s<span>导入于 %s</span></div></div>'
        '<div class="dhead-actions"><a class="btn" href="/resources%s">返回列表</a></div></div>'
        '<div class="panel"><div class="panel-head">正文'
        '<span class="muted">未上架 · 只有正文，没有 ZIP 包</span></div>'
        '<div class="panel-body">%s</div></div>'
        '<div class="panel" style="margin-top:16px"><div class="panel-head">来源</div>'
        '<div class="panel-body"><div class="kv"><span>抓取来源</span><b>%s</b></div>'
        '<div class="kv"><span>原始链接</span><b>%s</b></div></div></div></div>'
        % (qs(platform="ext", group=r["grp"]), esc(r["title"]), esc(r["title"]),
           esc(r["grp"] or "未分类"), tags, esc(fmt_time(r["updated_at"])),
           qs(platform="ext", group=r["grp"]),
           fm_html + '<div class="md">' + md.render(body) + '</div>',
           esc(r["source"] or ""), src))


# --------------------------------------------------------------------------
# 各个页面
# --------------------------------------------------------------------------

def page_login(msg="", err=""):
    body = (
        '<div class="wrap login-wrap">'
        '<div class="login-hero"><div class="big">灵感引擎 · Skill 预设库</div>'
        '<div class="sub">Skill · 预设 · Agent</div></div>'
        '<div class="panel"><div class="panel-body">'
        + ('<div class="err">%s</div>' % esc(err) if err else "")
        + ('<div class="ok">%s</div>' % esc(msg) if msg else "")
        + '<form method="post" action="/login">'
        '<div class="form-row"><label>账号</label><input type="text" name="username" autofocus required></div>'
        '<div class="form-row"><label>密码</label><input type="password" name="password" required></div>'
        '<button class="btn primary" style="width:100%%;justify-content:center">登录</button>'
        '</form>'
        '<div class="notice" style="margin-top:16px">请使用管理员提供的账号登录。'
        '账号由管理员在「账号管理」里创建，不开放自助注册。</div>'
        '</div></div></div>')
    return shell("登录", body)


def page_home(conn, user):
    skills = conn.execute(
        "SELECT * FROM resources WHERE kind='skill' AND platform!='ext' AND status='active'"
        " ORDER BY updated_at DESC LIMIT 8").fetchall()
    presets = store.list_resources(conn, platform="tv", kind="preset", limit=8)
    hot = store.top_used(conn, 8)
    counts = store.count_by(conn)
    ext = conn.execute(
        "SELECT * FROM resources WHERE platform='ext' AND status='active' AND cover_url!=''"
        " AND kind='preset' ORDER BY id ASC LIMIT 8").fetchall()
    cats = conn.execute(
        "SELECT DISTINCT cat FROM resources WHERE status='active' AND cat!='' LIMIT 8").fetchall()
    chips = "".join('<a class="chip" href="/resources%s">%s</a>' % (qs(cat=c["cat"]), esc(c["cat"]))
                    for c in cats)

    return shell("首页", (
        '<div class="hero"><div class="wrap">'
        '<div class="hero-kicker">个人创作资源工作台</div>'
        '<h1>Skill 和预设，<span class="grad">一个地方找得到</span></h1>'
        '<p class="muted">Flow 的规则包、TV 的提示词预设、未上架的第三方提示词，按平台 / 功能 / 分类统一收纳；'
        '能搜、能读、能下载、能看历史版本。</p>'
        '<form class="searchbox" method="get" action="/resources">'
        '<input type="text" name="q" placeholder="搜 Skill 名称、标识、预设描述，例如「三视图」「道具」">'
        '<button class="btn primary">搜索</button></form>'
        '<div class="hero-chips">%s</div></div></div>'
        '<div class="wrap">'
        '<div class="section-head"><h2>最近更新</h2>'
        '<span class="muted">Skill 新版本会更新到这里</span>'
        '<a class="go" href="/resources">全部资源 →</a></div>'
        '<div class="grid grid-home">%s</div>'
        '<div class="section-head"><h2>热门预设</h2><span class="muted">按真实使用次数排序</span>'
        '<a class="go" href="/resources%s">进入 TV 预设 →</a></div>'
        '<div class="grid grid-home">%s</div>'
        '<div class="section-head"><h2>未上架 · 第三方提示词</h2>'
        '<span class="muted">从公开提示词站采集，平台里没有 · %d 条</span>'
        '<a class="go" href="/resources?platform=ext">进入未上架 →</a></div>'
        '<div class="grid grid-home">%s</div>'
        '<div class="section-head"><h2>按平台找</h2></div>'
        '<div class="grid" style="grid-template-columns:repeat(3,1fr)">'
        '<a class="panel" style="padding:18px" href="/resources?platform=flow">'
        '<b style="font-size:16px">Flow</b><div class="muted" style="margin:6px 0 10px">'
        'Skill 以 zip 包发布，按阶段划分：剧本 / 分析手册 / 资产生成 / 单集 Agent。</div>'
        '<span class="tag plain">%d 个 Skill</span></a>'
        '<a class="panel" style="padding:18px" href="/resources?platform=tv">'
        '<b style="font-size:16px">TV</b><div class="muted" style="margin:6px 0 10px">'
        '预设＝提示词，按功能与分类管理，带封面与真实使用次数。</div>'
        '<span class="tag plain">%d 条预设</span></a>'
        '<a class="panel" style="padding:18px" href="/resources?platform=ext">'
        '<b style="font-size:16px">未上架</b><div class="muted" style="margin:6px 0 10px">'
        '从公开提示词站采集的第三方提示词与 Skill 正文，39 个分类；平台里没有，只能在这里复制使用。</div>'
        '<span class="tag plain">%d 条未上架资源</span></a></div>'
        '</div>'
        % (chips, "".join(card(r, conn) for r in skills),
           qs(platform="tv"), "".join(card(r, conn) for r in (presets or hot)),
           counts["ext"], "".join(card(r, conn) for r in ext),
           counts["skills"], counts["presets"], counts["ext"])), user, "/")


PAGE_SIZE = 48


def pager(platform, grp, cat, kind, q, page, pages, total, wf=None):
    if pages <= 1:
        return ""
    def link(p, label, cls=""):
        return ('<a class="page-btn %s" href="/resources%s">%s</a>'
                % (cls, qs(platform=platform, group=grp, cat=cat, type=kind, q=q, wf=wf, page=p), label))
    out = ['<div class="pager"><span class="muted">共 %d 条 · 第 %d/%d 页</span>' % (total, page, pages)]
    if page > 1:
        out.append(link(page - 1, "上一页"))
    start, end = max(1, page - 2), min(pages, page + 2)
    if start > 1:
        out.append(link(1, "1"))
        if start > 2:
            out.append('<span class="muted">…</span>')
    for p in range(start, end + 1):
        out.append(link(p, str(p), "on" if p == page else ""))
    if end < pages - 1:
        out.append('<span class="muted">…</span>')
    if end < pages:
        out.append(link(pages, str(pages)))
    if page < pages:
        out.append(link(page + 1, "下一页"))
    out.append('</div>')
    return "".join(out)


def page_resources(conn, user, query):
    platform = (query.get("platform") or ["all"])[0]
    grp = (query.get("group") or [""])[0]
    cat = (query.get("cat") or [""])[0]
    kind = (query.get("type") or [""])[0]
    if kind == "all":
        kind = ""
    q = (query.get("q") or [""])[0]
    wf = (query.get("wf") or [""])[0]        # Flow 渠道（团队流程）；空=全部，default=通用默认
    try:
        page = max(1, int((query.get("page") or ["1"])[0]))
    except ValueError:
        page = 1

    total = store.count_resources(conn, platform=platform, grp=grp, cat=cat, kind=kind, q=q, wf=wf or None)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages)
    order = "cover" if platform == "ext" else "updated"
    rows = store.list_resources(conn, platform=platform, grp=grp, cat=cat, kind=kind, q=q,
                                limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE, order=order,
                                wf=wf or None)

    counts = {}
    _csql = "SELECT platform, grp, cat, COUNT(*) c FROM resources WHERE status='active'"
    _cargs = []
    if wf == "default":
        _csql += " AND platform!='flow'"
        _csql += " UNION ALL SELECT 'flow', grp, cat, COUNT(*) FROM resources"
        _csql += " WHERE status='active' AND platform='flow' AND (flow_workflow IS NULL OR flow_workflow='')"
        _csql += " GROUP BY grp, cat"
    elif wf:
        _csql += " AND (platform!='flow' OR flow_workflow=?)"
        _cargs.append(wf)
    _csql += " GROUP BY platform, grp, cat"
    for row in conn.execute(_csql, _cargs):
        counts["%s|%s|%s" % (row["platform"], row["grp"], row["cat"])] = row["c"]
    plat_counts = {row["platform"]: row["c"] for row in conn.execute(
        "SELECT platform, COUNT(*) c FROM resources WHERE status='active' GROUP BY platform")}
    total_all = sum(plat_counts.values())
    shown = total_all if platform == "all" else plat_counts.get(platform, 0)

    side = ['<aside class="side" id="lib-side"><h3>全部资源</h3>',
            '<a class="item %s" href="/resources%s">全部<span class="n">%d</span></a>'
            % ("" if (grp or cat) else "on", qs(platform=platform), shown)]
    for p, label, heads in (("flow", "Flow · 阶段", store.FLOW_STAGES),
                            ("tv", "TV · 功能", store.TV_FUNCS)):
        # 始终展示两套分组：在 TV 页也能直接跳到 Flow，反之亦然
        side.append('<h3>%s</h3>' % label)
        for h, cats in heads:
            active = (grp == h) or (cat in cats)
            _gq = qs(platform=p, group=h, wf=(wf if p == "flow" else ""))
            _gn = sum(counts.get("%s|%s|%s" % (p, h, c), 0) for c in cats)
            side.append(
                '<div class="group"><button type="button" class="g-row %s" data-toggle="grp">'
                '<svg class="chev" viewBox="0 0 16 16" aria-hidden="true">'
                '<path d="M6 3.5 10.5 8 6 12.5"/></svg>'
                '<span class="g-name">%s</span><span class="g-count">%d</span></button>'
                '<div class="items open">'
                % ("active" if active else "", esc(h), _gn))
            side.append('<a class="item sub %s" href="/resources%s">全部%s'
                        '<span class="n">%d</span></a>'
                        % ("on" if (grp == h and not cat) else "", _gq, esc(h), _gn))
            for c in cats:
                n = counts.get("%s|%s|%s" % (p, h, c), 0)
                on = "on" if (grp == h and cat == c) else ""
                side.append('<a class="item %s" href="/resources%s">%s<span class="n">%d</span></a>'
                            % (on, qs(platform=p, group=h, cat=c,
                                     wf=(wf if p == "flow" else "")), esc(c), n))
            side.append('</div></div>')
    if True:  # 未上架分组始终可见，方便跨平台切换
        side.append('<h3>未上架 · 分类</h3>'
                    '<button type="button" class="g-row" data-toggle="side">'
                    '<svg class="chev" viewBox="0 0 16 16" aria-hidden="true">'
                    '<path d="M6 3.5 10.5 8 6 12.5"/></svg>'
                    '<span class="g-name">全部分类</span><span class="g-count">%d</span></button>'
                    '<div class="items open ext-groups">' % plat_counts.get("ext", 0))
        side.append('<a class="item %s" href="/resources%s">全部未上架资源<span class="n">%d</span></a>'
                    % ("" if grp else "on", qs(platform="ext"), plat_counts.get("ext", 0)))
        for g in store.groups_of(conn, "ext"):
            side.append('<a class="item %s" href="/resources%s">%s<span class="n">%d</span></a>'
                        % ("on" if grp == g["grp"] else "",
                           qs(platform="ext", group=g["grp"]), esc(g["grp"]), g["c"]))
        side.append('</div>')
    side.append('</aside>')

    flow_grouped = (platform == "flow" and not wf and not grp and not cat and not q
                    and not kind and page == 1)
    split = (platform == "tv" and not kind and not q and not grp and not cat and page == 1)
    kind_counts = {row["kind"]: row["c"] for row in conn.execute(
        "SELECT kind, COUNT(*) c FROM resources WHERE platform='tv' AND status='active' GROUP BY kind")}

    platchips = "".join(
        '<a class="chip %s" href="/resources%s">%s</a>'
        % ("on" if platform == v else "", qs(platform=v, q=q), label)
        for v, label in (("all", "全部平台"), ("flow", "Flow"), ("tv", "TV"), ("ext", "未上架")))
    wfchips = ""
    if platform == "flow":
        wf_rows = conn.execute(
            "SELECT COALESCE(flow_workflow,'') w, COUNT(*) c FROM resources"
            " WHERE platform='flow' AND status='active' GROUP BY w").fetchall()
        wf_count = {r["w"]: r["c"] for r in wf_rows}
        total_flow = sum(wf_count.values())
        items = [('', '全部渠道', total_flow)] + [(k, label, wf_count.get(k, 0))
                                                 for k, label in store.FLOW_WORKFLOWS]
        bits = []
        for k, label, n in items:
            if not n:
                continue
            val = "default" if k == "" else k
            on = (wf == val) or (wf == "" and k == "")
            bits.append('<a class="chip wf %s" href="/resources%s">%s<span class="n">%d</span></a>'
                        % ("on" if on else "", qs(platform="flow", wf=val if k else None), esc(label), n))
        wfchips = ('<div class="wf-row"><span class="wf-label">团队流程</span>'
                   '<div class="wf-chips">' + "".join(bits) + '</div></div>')
    typechips = "".join(
        '<a class="chip %s" href="/resources%s">%s</a>'
        % ("on" if kind == v else "", qs(platform=platform, group=grp, cat=cat, type=v, q=q), label)
        for v, label in (("all", "全部类型"), ("skill", "Skill"), ("preset", "预设")))
    title = (cat or grp or {"flow": "Flow 资源", "tv": "TV 预设",
                            "ext": "未上架 · 第三方提示词"}.get(platform, "全部资源"))
    ext_note = ""
    if platform == "ext":
        ext_note = ('<div class="notice"><span><b>未上架</b>　来自公开提示词站，'
                    'Flow / TV 平台里没有，只能在这里复制或下载使用；保留原始来源信息。</span></div>')

    if flow_grouped:
        teams = []
        for key, label in store.FLOW_WORKFLOWS:
            n = conn.execute(
                "SELECT COUNT(*) c FROM resources WHERE platform='flow' AND status='active'"
                " AND COALESCE(flow_workflow,'')=?", (key,)).fetchone()["c"]
            if n:
                teams.append((key, label, n))
        parts = []
        for key, label, n in teams:
            krows = store.list_resources(conn, platform="flow", limit=6,
                                         wf=("default" if key == "" else key))
            more = ('<a class="sec-more" href="/resources%s">全部 <b>%d</b> 条 '
                    '<span class="arr">→</span></a>'
                    % (qs(platform="flow", wf=("default" if key == "" else key)), n)) if n > len(krows) else ""
            parts.append('<div class="section-head split" style="margin-top:18px"><h2>%s</h2>'
                         '<span class="hint">%d 个 Skill</span>%s</div><div class="grid">%s</div>'
                         % (esc(label), n, more, "".join(card(r, conn) for r in krows)))
        list_body = ('<div class="section-head" style="margin-top:6px"><h2>Flow 资源</h2>'
                     '<span class="muted">共 %d 个，按团队流程分组</span></div>' % total) + "".join(parts)
    elif split:
        parts = []
        for knd, klabel, kdesc in (("preset", "TV · 预设", "提示词模板，按功能与分类整理，带封面与真实使用次数"),
                                   ("skill", "TV · Skill", "TV 后台的技能说明，纯文本正文")):
            krows = store.list_resources(conn, platform="tv", kind=knd, limit=12, order="updated")
            ktotal = kind_counts.get(knd, 0)
            more = ('<a class="sec-more" href="/resources%s">全部 <b>%d</b> 条 '
                    '<span class="arr">→</span></a>'
                    % (qs(platform="tv", type=knd), ktotal)) if ktotal > len(krows) else ""
            grid = "".join(card(r, conn) for r in krows) or (
                '<div class="empty" style="grid-column:1/-1">这一类还没有资源。</div>')
            parts.append('<div class="section-head split" style="margin-top:4px"><h2>%s</h2>'
                         '<span class="hint">%s</span>%s</div><div class="grid">%s</div>'
                         % (esc(klabel), esc(kdesc), more, grid))
        list_body = "".join(parts)
    else:
        list_body = ('<div class="section-head" style="margin-top:6px"><h2>%s</h2>'
                     '<span class="muted">%d 个资源</span></div>'
                     '<div class="grid">%s</div>%s'
                     % (esc(title), total,
                        "".join(card(r, conn) for r in rows) or
                        '<div class="empty" style="grid-column:1/-1"><div class="big">🗂️</div>'
                        '这个筛选下还没有资源。</div>',
                        pager(platform, grp, cat, kind, q, page, pages, total, wf)))

    body = (
        '<div class="wrap"><div class="lib">%s<div class="main" id="lib-main">'
        '<div class="toolbar" style="flex-wrap:wrap">'
        '<form method="get" action="/resources" class="searchbox" style="margin:0;flex:1;min-width:260px">'
        '<input type="hidden" name="platform" value="%s">'
        '<input type="hidden" name="group" value="%s"><input type="hidden" name="cat" value="%s">'
        '<input type="text" name="q" value="%s" placeholder="在结果里搜..."></form>'
        '<div class="chips">%s</div><div class="chips">%s</div></div>'
        '%s%s%s'
        '</div></div></div>'
        % ("".join(side), esc(platform), esc(grp), esc(cat), esc(q), platchips, typechips,
           wfchips, ext_note, list_body))
    return shell("资源库", body, user, "/resources")


def page_me(conn, user, err="", msg=""):
    mine = conn.execute(
        "SELECT * FROM resources WHERE owner_id=? AND status='active' ORDER BY updated_at DESC",
        (user["id"],)).fetchall()
    events = conn.execute(
        "SELECT e.*, r.title AS rtitle FROM events e LEFT JOIN resources r ON r.id=e.resource_id"
        " WHERE e.user_id=? ORDER BY e.id DESC LIMIT 20", (user["id"],)).fetchall()
    ev = "".join('<li><span class="tag plain">%s</span><span>%s</span>'
                 '<span class="muted" style="margin-left:auto">%s</span></li>'
                 % ({"upload": "上传", "download": "下载", "view": "浏览", "login": "登录",
                     "password": "改密", "reset-password": "重置密码"}.get(e["action"], e["action"]),
                    esc(e["rtitle"] or e["detail"] or ""), esc(fmt_time(e["created_at"])))
                 for e in events)
    return shell("我的", (
        '<div class="wrap page"><div class="page-head"><h1>我的</h1>'
        '<span class="muted">%s</span></div>'
        '<div class="section-head"><h2>我上传的资源</h2><span class="muted">%d 个</span></div>'
        '<div class="grid">%s</div>'
        '<div class="section-head"><h2>浏览与使用记录</h2></div>'
        '<div class="panel"><div class="panel-body"><ul class="plain-list">%s</ul></div></div>'
        '<div class="section-head"><h2>修改密码</h2>'
        '<span class="muted">首次登录后请改掉管理员给的初始密码</span></div>'
        '<div class="panel"><div class="panel-body">%s%s'
        '<form method="post" action="/account/password" style="max-width:420px">'
        '<div class="form-row"><label>当前密码</label>'
        '<input type="password" name="old" required></div>'
        '<div class="form-row"><label>新密码（至少 6 位）</label>'
        '<input type="password" name="new1" required></div>'
        '<div class="form-row"><label>再输一次新密码</label>'
        '<input type="password" name="new2" required></div>'
        '<button class="btn primary">保存新密码</button></form></div></div>'
        '</div>'
        % (esc(user["name"]), len(mine), "".join(card(r, conn) for r in mine) or
           '<div class="empty" style="grid-column:1/-1">还没有上传过资源。</div>', ev,
           ('<div class="err">%s</div>' % esc(err) if err else ""),
           ('<div class="ok">%s</div>' % esc(msg) if msg else ""))), user, "/me")


AGENT_EMPTY = (
    '<div class="agent-empty">'
    '<div class="big">问点什么试试</div>'
    '<div class="muted">我会先查库、再回答，每条结论都会标出处；查不到就说没查到。</div>'
    '<div class="asks">'
    '<button class="ask" data-ask="有没有生成角色三视图的预设？怎么用">找「角色三视图」预设</button>'
    '<button class="ask" data-ask="道具资产生成和场景资产生成这两条 Skill 有什么区别">对比两条 Skill</button>'
    '<button class="ask" data-ask="我们库里有哪些分类？要传一个 TV 换装预设该放哪">分类怎么选</button>'
    '<button class="ask" data-ask="最近新加的 Skill 有哪些">最近更新了什么</button>'
    '</div></div>')


def _steps_html(steps, elapsed_ms, open_=False):
    if not steps:
        return ""
    items = []
    for st in steps:
        args = st.get("args") or {}
        arg_txt = json.dumps(args, ensure_ascii=False) if args else ""
        items.append('<li><span class="toolname">%s</span>%s<span class="res%s">%s</span></li>' % (
            esc(st.get("tool", "")),
            ('<code class="args">%s</code>' % esc(arg_txt)) if arg_txt else "",
            "" if st.get("ok") else " bad",
            esc(st.get("summary", ""))))
    secs = ("%.1f 秒" % (elapsed_ms / 1000.0)) if elapsed_ms else ""
    return ('<details class="trace"%s><summary>用了 %d 步工具%s</summary><ol class="steplist">%s</ol></details>'
            % (" open" if open_ else "", len(steps), (" · " + secs) if secs else "", "".join(items)))


def _sources_html(sources):
    if not sources:
        return ""
    chips = "".join('<a class="srcchip" href="/r/%d">[%d] %s</a>' % (s["id"], s["id"], esc(s["title"]))
                    for s in sources[:8])
    return '<div class="srcs"><span class="muted">引用来源</span>%s</div>' % chips


def _inline_md(line):
    h = esc(line)
    h = re.sub(r"`([^`]+)`", r"<code>\1</code>", h)
    h = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", h)
    h = re.sub(r"\[(\d+)\]", r'<b class="cite">[\1]</b>', h)
    return h


def _agent_msg_html(m, open_steps=False):
    role = m["role"]
    if role == "user":
        return '<div class="msg me"><div class="bubble">%s</div></div>' % esc(m["content"]).replace(chr(10), "<br>")
    steps = []
    sources = []
    try:
        steps = json.loads(m["steps"] or "[]")
    except Exception:  # noqa: BLE001
        steps = []
    try:
        sources = json.loads(m["sources"] or "[]")
    except Exception:  # noqa: BLE001
        sources = []
    body = m["content"] or ""
    parts = []
    for ln in body.split(chr(10)):
        if not ln.strip():
            parts.append('<div class="gap"></div>')
            continue
        m2 = re.match(r"^\s*[-*]\s+(.*)$", ln)
        if m2:
            parts.append('<div class="line li">• %s</div>' % _inline_md(m2.group(1)))
        else:
            parts.append('<div class="line">%s</div>' % _inline_md(ln.strip()))
    lines = "".join(parts)
    return ('<div class="msg bot"><div class="bubble">'
            '<div class="bothead"><span class="mark">剧</span><span class="name">灵感引擎 Agent</span>'
            '%s</div><div class="answer">%s</div>%s%s</div></div>'
            % ('<span class="pill ok">工具调用</span>' if steps else "",
               lines, _steps_html(steps, m["elapsed_ms"] or 0, open_steps), _sources_html(sources)))


def page_agent(conn, user, sid="", open_steps=False):
    sess = store.agent_session(conn, user["id"], sid, title="Agent 会话")
    msgs = store.agent_history(conn, sess["id"], 40)
    cfg = ai.load_config()
    have_key = bool(cfg.get("api_key"))

    notice = ""
    if not have_key:
        notice = ('<div class="agent-warn">还没配 DeepSeek Key，Agent 现在不会推理。'
                  '去 <a href="/admin/settings">AI 设置</a> 里贴一个 Key（只需一次）。</div>')

    history_html = "".join(_agent_msg_html(m, open_steps) for m in msgs) or AGENT_EMPTY

    body = (
        '<div class="wrap page agent-page">'
        '<div class="agent-head"><div><h1>内置 Agent</h1>'
        '<div class="muted">会自己查库、比对、再给你答案。每一步查了什么都能展开看。</div></div>'
        '<div class="agent-tools muted">可用工具：search_library · open_resource · list_taxonomy · library_stats</div>'
        '</div>' + ('<div class="agent-bar"><a class="btn ghost" href="/agent">＋ 新会话</a></div>' if sid else '')
        + '%s'
        '<div id="chat" class="chat" data-sid="%s">%s</div>'
        '<form id="agent-form" class="composer" autocomplete="off">'
        '<textarea id="agent-input" rows="1" placeholder="问一句，例如：有没有能直接用的换装预设？"></textarea>'
        '<button id="agent-send" class="btn primary" type="submit">发 送</button>'
        '</form>'
        '<div class="composer-hint muted">Enter 发送，Shift + Enter 换行 · 回答末尾的 [数字] 就是引用的资源</div>'
        '</div>'
        '<script src="/static/agent.js?v=1"></script>'
        % (notice, sess["id"], history_html))

    return shell("内置 Agent", body, user, "/agent")


def _day_list(n):
    today = datetime.date.today()
    return [(today - datetime.timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]


def _spark(days, series):
    """series: [(label, color, {day: n})] -> 折线图（纯 HTML + 内联 SVG 线段，无 JS）。"""
    maxv = 1
    for _l, _c, data in series:
        for v in data.values():
            maxv = max(maxv, v)
    # 取整到好看的刻度
    step = 10 ** max(0, len(str(int(maxv))) - 2)
    top = int(maxv / float(step) + 1) * step
    W, H = 1000, 240
    lines = []
    for i in range(5):
        y = H * i / 4.0
        lines.append('<line x1="0" y1="%.1f" x2="%d" y2="%.1f" stroke="#eef0f8" stroke-width="1"/>'
                     % (y, W, y))
    for _l, color, data in series:
        pts = []
        for i, day in enumerate(days):
            v = data.get(day, 0)
            x = W * i / float(max(1, len(days) - 1))
            y = H - (v / float(top)) * H
            pts.append("%.1f,%.1f" % (x, y))
        lines.append('<polyline fill="none" stroke="%s" stroke-width="2.6" stroke-linejoin="round" '
                     'stroke-linecap="round" points="%s"/>' % (color, " ".join(pts)))
        last_v = data.get(days[-1], 0)
        lines.append('<circle cx="%d" cy="%.1f" r="4" fill="%s"/>'
                     % (W, H - (last_v / float(top)) * H, color))
    svg = ('<svg class="chart" viewBox="0 0 %d %d" preserveAspectRatio="none">%s</svg>'
           % (W, H, "".join(lines)))

    ylab = "".join('<span>%d</span>' % int(round(top * (1 - i / 4.0))) for i in range(5))
    xlab = "".join('<span>%s</span>' % day[5:] for day in days)
    legend = "".join('<span class="lg"><i style="background:%s"></i>%s</span>' % (c, esc(l))
                     for l, c, _d in series)
    return ('<div class="legend">%s</div>'
            '<div class="chart-wrap"><div class="chart-y">%s</div><div class="chart-main">%s'
            '<div class="chart-x">%s</div></div></div>' % (legend, ylab, svg, xlab))


def page_insights(conn, user):
    c = store.count_by(conn)
    days = _day_list(14)
    since = days[0]

    rows = conn.execute(
        "SELECT substr(created_at,1,10) d, action, COUNT(*) n FROM events"
        " WHERE created_at >= ? GROUP BY d, action", (since,)).fetchall()
    daily = {}
    for r in rows:
        daily.setdefault(r["action"], {})[r["d"]] = r["n"]
    views = daily.get("view", {})
    downs = daily.get("download", {})
    ups = daily.get("upload", {})

    n_views = sum(views.values())
    n_down = sum(downs.values())
    n_up = sum(ups.values())
    n_ai = sum(daily.get("ai-ask", {}).values()) + sum(daily.get("agent-ask", {}).values())

    week_ago = _day_list(7)[0]
    w = conn.execute("SELECT action, COUNT(*) n FROM events WHERE created_at >= ? GROUP BY action",
                     (week_ago,)).fetchall()
    wk = {r["action"]: r["n"] for r in w}
    wk_views = wk.get("view", 0)
    wk_down = wk.get("download", 0)
    wk_ai = wk.get("ai-ask", 0) + wk.get("agent-ask", 0)
    active_users = conn.execute(
        "SELECT COUNT(DISTINCT user_id) n FROM events WHERE created_at >= ?", (week_ago,)).fetchone()["n"]

    charts = _spark(days, [("浏览", "#6366f1", views), ("下载", "#22c55e", downs), ("上传/新版本", "#f59e0b", ups)])
    first_ev = conn.execute("SELECT MIN(substr(created_at,1,10)) d FROM events").fetchone()["d"]
    if first_ev:
        charts += ('<div class="hint">操作记录从 ' + esc(first_ev) + ' 开始采集，之前的日期为 0 属正常；'
                   '浏览=打开详情页，下载=真实下载原包。</div>')

    plat = conn.execute("SELECT platform, COUNT(*) n FROM resources WHERE status != 'deleted'"
                        " GROUP BY platform").fetchall()
    plat_map = {r["platform"]: r["n"] for r in plat}
    total_res = sum(plat_map.values()) or 1
    plat_names = [("flow", "Flow", "#6366f1"), ("tv", "TV", "#0ea5e9"), ("ext", "未上架", "#a855f7")]
    segs = []
    for key, name, color in plat_names:
        pct = 100.0 * plat_map.get(key, 0) / total_res
        if pct > 0:
            segs.append('<i style="width:%.1f%%;background:%s"></i>' % (pct, color))
    cov_n = conn.execute("SELECT COUNT(*) n FROM resources WHERE status != 'deleted' AND cover_url != ''").fetchone()["n"]
    new7 = conn.execute("SELECT COUNT(*) n FROM resources WHERE status != 'deleted' AND created_at >= ?",
                        (week_ago,)).fetchone()["n"]
    plat_extra = ('<div class="pl-extra"><div><span>有封面</span><b>%d</b></div>'
                  '<div><span>近 7 天新增</span><b>%d</b></div></div>' % (cov_n, new7))
    plat_legend = "".join(
        '<div class="pl-row"><span class="dot" style="background:%s"></span>%s<b>%d</b>'
        '<em>%.0f%%</em></div>' % (color, name, plat_map.get(key, 0), 100.0 * plat_map.get(key, 0) / total_res)
        for key, name, color in plat_names)

    def _cat_bars(where_sql, limit):
        rows2 = conn.execute("SELECT platform, grp, COUNT(*) n FROM resources"
                             " WHERE status != 'deleted' AND " + where_sql +
                             " GROUP BY platform, grp ORDER BY n DESC LIMIT %d" % limit).fetchall()
        mx = max([r["n"] for r in rows2] + [1])
        return "".join(
            '<div class="bar-row"><span>%s · %s</span><div class="bar"><i style="width:%d%%"></i></div><b>%d</b></div>'
            % ({"flow": "Flow", "tv": "TV", "ext": "未上架"}.get(r["platform"], r["platform"]),
               esc(r["grp"] or "未分类"), int(100.0 * r["n"] / mx), r["n"]) for r in rows2)

    cat_bars = _cat_bars("platform IN ('flow','tv')", 8)
    ext_bars = _cat_bars("platform = 'ext'", 6)

    hot = store.top_used(conn, 10)
    hot_max = max([r["usage_count"] or 0 for r in hot] + [1])
    hot_bars = "".join(
        '<div class="bar-row"><span>%s</span><div class="bar"><i style="width:%d%%"></i></div><b>%d</b></div>'
        % (esc(r["title"]), int(100.0 * (r["usage_count"] or 0) / hot_max), r["usage_count"] or 0) for r in hot)

    up = conn.execute(
        "SELECT v.seq, v.file_count, v.bytes, v.created_at, r.id rid, r.title, u.name uname"
        " FROM versions v JOIN resources r ON r.id = v.resource_id"
        " LEFT JOIN users u ON u.id = v.created_by ORDER BY v.id DESC LIMIT 10").fetchall()
    up_rows = "".join(
        '<tr><td>%s</td><td><a href="/r/%d">%s</a></td><td><span class="tag plain">v%d</span></td>'
        '<td>%s</td><td>%d</td><td>%s</td></tr>'
        % (esc(fmt_time(r["created_at"])), r["rid"], esc(r["title"]), r["seq"], esc(r["uname"] or "—"),
           r["file_count"], fmt_bytes(r["bytes"])) for r in up)

    acts = conn.execute(
        "SELECT u.id, u.name, u.role, COUNT(*) n, MAX(e.created_at) last FROM events e"
        " JOIN users u ON u.id = e.user_id GROUP BY u.id ORDER BY n DESC LIMIT 10").fetchall()
    mem_rows = []
    for m in acts:
        per = conn.execute(
            "SELECT action, COUNT(*) n FROM events WHERE user_id = ? GROUP BY action", (m["id"],)).fetchall()
        pm = {r["action"]: r["n"] for r in per}
        mem_rows.append(
            '<tr><td>%s</td><td>%s</td><td>%d</td><td>%d</td><td>%d</td><td>%d</td><td>%s</td></tr>'
            % (esc(m["name"]), "管理员" if m["role"] == "admin" else "成员",
               pm.get("view", 0), pm.get("download", 0), pm.get("upload", 0),
               pm.get("ai-ask", 0) + pm.get("agent-ask", 0), esc(fmt_time(m["last"]))))
    if not mem_rows:
        mem_rows.append('<tr><td colspan="7" class="muted">还没有成员操作记录</td></tr>')

    ev = "".join('<tr><td>%s</td><td>%s</td><td>%s</td></tr>'
                 % (esc(fmt_time(e["created_at"])), esc(e["uname"] or ""),
                    esc({"upload": "上传", "download": "下载", "view": "浏览", "login": "登录",
                         "ai-ask": "问 AI", "agent-ask": "问 Agent", "password": "改密",
                         "reset-password": "重置密码"}.get(e["action"], e["action"])
                        + ((" · " + (e["rtitle"] or e["detail"] or "")) if (e["rtitle"] or e["detail"]) else "")))
                 for e in store.recent_events(conn, 15))

    kpis = "".join([
        '<div class="kpi"><span>资源总数</span><b>%d</b><i>Flow %d · TV %d · 未上架 %d</i></div>'
        % (c["resources"], plat_map.get("flow", 0), plat_map.get("tv", 0), plat_map.get("ext", 0)),
        '<div class="kpi"><span>近 7 天浏览</span><b>%d</b><i>14 天共 %d 次</i></div>' % (wk_views, n_views),
        '<div class="kpi"><span>近 7 天下载</span><b>%d</b><i>真实下载，不含点击</i></div>' % wk_down,
        '<div class="kpi"><span>近 14 天上传/新版本</span><b>%d</b><i>历史累计 %d</i></div>' % (n_up, c["uploads"]),
        '<div class="kpi"><span>近 7 天活跃成员</span><b>%d</b><i>共 %d 个账号</i></div>' % (active_users, c["users"]),
        '<div class="kpi"><span>AI / Agent 提问</span><b>%d</b><i>近 14 天 %d 次</i></div>' % (wk_ai, n_ai),
    ])

    return shell("数据概览", (
        '<div class="wrap page"><div class="page-head"><h1>数据概览</h1>'
        '<span class="muted">口径：浏览=打开详情页，下载=真实下载原包，数据来自 events 表</span></div>'
        '<div class="kpis kpis-6">' + kpis + '</div>'
        '<div class="section-head"><h2>近 14 天趋势</h2><span class="muted">按天聚合</span></div>'
        '<div class="panel"><div class="panel-body">' + charts + '</div></div>'
        '<div class="grid-split">'
        '<div><div class="section-head"><h2>平台分布</h2></div><div class="panel"><div class="panel-body">'
        '<div class="stack">' + "".join(segs) + '</div>' + plat_legend + plat_extra + '</div></div></div>'
        '<div><div class="section-head"><h2>资源分类 Top 8</h2><span class="muted">Flow + TV</span></div>'
        '<div class="panel"><div class="panel-body">'
        + (cat_bars or '<span class="muted">暂无分类数据</span>') + '</div></div></div>'
        '</div>'
        '<div class="section-head"><h2>未上架分类 Top 6</h2></div>'
        '<div class="panel"><div class="panel-body bars2">'
        + (ext_bars or '<span class="muted">暂无未上架资源</span>') + '</div></div>'
        '<div class="section-head"><h2>使用最多的预设</h2><span class="muted">来自 TV 后台真实使用次数</span></div>'
        '<div class="panel"><div class="panel-body bars2">' + (hot_bars or '<span class="muted">暂无数据</span>') + '</div></div>'
        '<div class="section-head"><h2>最近上传 / 新版本</h2></div>'
        '<div class="panel"><table class="tbl"><thead><tr><th>时间</th><th>资源</th><th>版本</th>'
        '<th>上传人</th><th>文件数</th><th>包大小</th></tr></thead><tbody>' + up_rows + '</tbody></table></div>'
        '<div class="section-head"><h2>成员活跃度</h2><span class="muted">按操作次数排序</span></div>'
        '<div class="panel"><table class="tbl"><thead><tr><th>成员</th><th>角色</th><th>浏览</th><th>下载</th>'
        '<th>上传</th><th>AI/Agent</th><th>最近活跃</th></tr></thead><tbody>' + "".join(mem_rows) + '</tbody></table></div>'
        '<div class="section-head"><h2>最近操作</h2></div>'
        '<div class="panel"><table class="tbl"><thead><tr><th>时间</th><th>谁</th><th>做了什么</th></tr></thead>'
        '<tbody>' + ev + '</tbody></table></div></div>'), user, "/insights")


def _json(obj):
    return json.dumps(obj, ensure_ascii=False)


def page_upload(conn, user, rid="", err="", msg="", staged="", info=None,
                sug=None, filename=""):
    target = store.get_resource(conn, int(rid)) if rid.isdigit() else None
    out = ['<div class="wrap page" style="max-width:900px">']
    if err:
        out.append('<div class="err">%s</div>' % esc(err))
    if msg:
        out.append('<div class="ok">%s</div>' % esc(msg))

    # ---------- 给已有 Skill 发新版本：分类不变，只要文件 ----------
    if target is not None:
        out.append(
            '<div class="page-head"><h1>给「%s」发新版本</h1>'
            '<span class="muted">上传后立即成为最新版，旧版本保留且只读；分类沿用「%s · %s」</span></div>'
            % (esc(target["title"]), esc(target["grp"]), esc(target["cat"] or "—")))
        out.append(
            '<div class="panel"><div class="panel-body">'
            '<form method="post" action="/upload" enctype="multipart/form-data">'
            '<input type="hidden" name="action" value="direct">'
            '<input type="hidden" name="rid" value="%s">' % esc(rid))
        out.append(
            '<label class="drop"><input type="file" name="zip" accept=".zip" required>'
            '<b>选择这一版的 ZIP</b><small>包内的 SKILL.md 必须还是同一个 name，否则会被当成新资源</small></label>')
        out.append(
            '<div class="form-row"><label>这一版改了什么（写进版本记录）</label>'
            '<input type="text" name="note" placeholder="例：补充明朝道具清单"></div>')
        out.append('<div class="form-actions"><button class="btn primary">上传并发布新版本</button>'
                   '<a class="btn" href="/r/%s">取消</a></div></form></div></div></div>' % esc(rid))
        return shell("发布新版本", "".join(out), user, "/upload")

    # ---------- 第一步：只上传 ----------
    if not staged:
        out.append(
            '<div class="page-head"><h1>新增资源</h1>'
            '<span class="muted">只上传 ZIP 就行。AI 会读包里的 SKILL.md，判断这是什么、'
            '该放哪个平台和分类，你确认一下才入库。</span></div>')
        out.append('<div class="steps"><i class="on">1</i>上传 ZIP<em></em><i>2</i>确认 AI 的判断'
                   '<em></em><i>3</i>入库</div>')
        out.append(
            '<div class="panel"><div class="panel-body">'
            '<form method="post" action="/upload" enctype="multipart/form-data">'
            '<input type="hidden" name="action" value="analyze">'
            '<label class="drop"><input type="file" name="zip" accept=".zip" required>'
            '<b>把 Skill ZIP 拖进来，或点这里选文件</b>'
            '<small>必须能找到 SKILL.md 或 README.md；单包 ≤ 200MB，解压后 ≤ 1GB，文件数 ≤ 2000</small></label>'
            '<div class="form-row"><label>补充说明（可留空）</label>'
            '<input type="text" name="hint" placeholder="例：这是给 TV 图生图用的换装预设">'
            '<span class="hint">AI 拿不准时，这句话会帮它判断</span></div>'
            '<div class="form-actions"><button class="btn primary">上传并让 AI 读一遍</button>'
            '<span class="muted">上传后先不会入库，你确认之前别人看不到</span></div>'
            '</form></div></div></div>')
        return shell("新增资源", "".join(out), user, "/upload")

    # ---------- 第二步：确认 AI 的判断 ----------
    info = info or {}
    sug = sug or {}
    fm = info.get("frontmatter") or {}
    heads = info.get("headings") or []
    files = info.get("files") or []
    mode = sug.get("mode") or "rule"
    pill = "大模型读的" if mode == "llm" else "规则读的（没配 API Key）"

    out.append(
        '<div class="page-head"><h1>确认一下就能入库</h1>'
        '<span class="muted">下面每一项都是 AI 从包里读出来的，都可以改；确认前别人看不到</span></div>')
    out.append('<div class="steps"><i>1</i>上传 ZIP<em></em><i class="on">2</i>确认 AI 的判断'
               '<em></em><i>3</i>入库</div>')

    facts = [
        ("文件", esc(filename or "package.zip")),
        ("体积", "%d 个文件 · %.1f KB" % (info.get("file_count", 0), info.get("total_bytes", 0) / 1024.0)),
        ("主文件", esc(info.get("main") or "（没找到 SKILL.md）")),
        ("包内标识", esc(fm.get("name") or "（frontmatter 里没有 name）")),
    ]
    if heads:
        facts.append(("正文小标题", esc(" / ".join(heads[:3]))))
    if files:
        facts.append(("目录", esc("、".join(sorted({f.split("/")[0] for f in files if "/" in f})[:6]) or "（扁平结构）")))

    rows = "".join('<dt>%s</dt><dd>%s</dd>' % (k, v) for k, v in facts)
    out.append('<div class="panel ai-read"><div class="panel-head">AI 读到了什么'
               '<span class="pill">%s</span></div><div class="panel-body"><dl class="kv">%s</dl>'
               '</div></div>' % (pill, rows))
    if sug.get("why"):
        out.append('<div class="notice">判断依据：%s</div>' % esc(sug["why"]))

    out.append('<div class="panel"><div class="panel-head">确认分类与标题</div><div class="panel-body">'
               '<form method="post" action="/upload">'
               '<input type="hidden" name="action" value="confirm">'
               '<input type="hidden" name="staged" value="%s">'
               '<input type="hidden" name="filename" value="%s">'
               % (esc(staged), esc(filename or "package.zip")) +
               '<div class="form-grid3">'
               '<div class="form-row"><label>平台</label><select name="platform" id="f-plat">'
               '<option value="flow">Flow（按阶段）</option><option value="tv">TV（按功能）</option></select></div>'
               '<div class="form-row"><label>阶段 / 功能</label><select name="grp" id="f-grp"></select></div>'
               '<div class="form-row"><label>分类</label><select name="cat" id="f-cat"></select></div>'
               '</div>'
               '<div class="form-row"><label>标题</label>'
               '<input type="text" name="title" value="%s" placeholder="例：道具资产生成"></div>'
               '<div class="form-grid">'
               '<div class="form-row"><label>标签（空格分隔）</label>'
               '<input type="text" name="tags" value="%s"></div>'
               '<div class="form-row"><label>一句话用途</label>'
               '<input type="text" name="summary" value="%s"></div>'
               '</div>'
               '<div class="form-row"><label>版本备注（AI 已写好，可改）</label>'
               '<input type="text" name="note" value="%s"></div>'
               '<div class="form-actions"><button class="btn primary">确认入库</button>'
               '<a class="btn" href="/upload">重新上传</a></div>'
               '</form></div></div></div>'
               % (esc(sug.get("title") or ""), esc(" ".join(sug.get("tags") or ["DSH"])),
                  esc(sug.get("summary") or ""), esc(sug.get("note") or "")))

    tax_flow = [[g, list(c)] for g, c in store.FLOW_STAGES]
    tax_tv = [[f, list(c)] for f, c in store.TV_FUNCS]
    init = {"plat": sug.get("platform") or "flow", "grp": sug.get("grp") or "",
            "cat": sug.get("cat") or ""}
    out.append(
        '<script>'
        'var FLOW=' + _json(tax_flow) + ',TV=' + _json(tax_tv) + ',INIT=' + _json(init) + ';'
        'function cats(g){var p=document.getElementById("f-plat").value;'
        'var tax=p==="tv"?TV:FLOW;for(var i=0;i<tax.length;i++){if(tax[i][0]===g)return tax[i][1];}return [];}'
        'function fillGrp(){var p=document.getElementById("f-plat").value;var tax=p==="tv"?TV:FLOW;'
        'var g=document.getElementById("f-grp");g.innerHTML="";'
        'for(var i=0;i<tax.length;i++){var o=document.createElement("option");o.value=tax[i][0];'
        'o.textContent=tax[i][0];g.appendChild(o);}fillCat();}'
        'function fillCat(){var c=document.getElementById("f-cat");var cs=cats(document.getElementById("f-grp").value);'
        'c.innerHTML="";for(var i=0;i<cs.length;i++){var o=document.createElement("option");o.value=cs[i];'
        'o.textContent=cs[i];c.appendChild(o);}}'
        'function apply(p,g,cc){document.getElementById("f-plat").value=p;fillGrp();'
        'if(g){document.getElementById("f-grp").value=g;}fillCat();if(cc){document.getElementById("f-cat").value=cc;}}'
        'document.getElementById("f-plat").onchange=function(){fillGrp();};'
        'document.getElementById("f-grp").onchange=function(){fillCat();};'
        'apply(INIT.plat,INIT.grp,INIT.cat);'
        '</script>')
    return shell("确认入库", "".join(out), user, "/upload")


def page_ai_settings(conn, user, msg="", err=""):
    cfg = ai.load_config()
    key = cfg.get("api_key") or ""
    masked = (key[:6] + "…" + key[-4:]) if len(key) > 12 else ("已配置" if key else "未配置")
    body = ('<div class="wrap page" style="max-width:760px">'
            '<div class="page-head"><h1>AI 助手设置</h1>'
            '<span class="muted">右下角「问 AI」用的模型配置，Key 只存在服务器上</span></div>')
    if msg:
        body += '<div class="ok">' + esc(msg) + '</div>'
    if err:
        body += '<div class="err">' + esc(err) + '</div>'
    body += ('<div class="panel"><div class="panel-body">'
             '<form method="post" action="/admin/settings">'
             '<div class="form-row"><label>API Base</label>'
             '<input type="text" name="base_url" value="' + esc(cfg.get("base_url", "")) + '"></div>'
             '<div class="form-row"><label>模型</label>'
             '<input type="text" name="model" value="' + esc(cfg.get("model", "")) + '"></div>'
             '<div class="form-row"><label>API Key（当前：' + esc(masked) + '，留空＝不改）</label>'
             '<input type="password" name="api_key" placeholder="sk-..."></div>'
             '<div class="form-row"><label>'
             '<input type="checkbox" name="clear_key" value="1"> 清除已保存的 Key</label></div>'
             '<div class="form-actions"><button class="btn primary" type="submit">保存</button>'
             '<a class="btn" href="/">返回首页</a></div>'
             '</form></div></div>'
             '<div class="panel" style="margin-top:16px"><div class="panel-head">怎么用</div>'
             '<div class="panel-body"><p class="muted">'
             '没配 Key 时：助手只做检索，把最相关的资源列给你。<br>'
             '配了 Key 后：先把检索到的资料发给模型，再让它归并成答案，句末用 [编号] 标来源。<br>'
             'Key 建议在 DeepSeek 控制台建一个独立 Key，只给这台机器用。</p></div></div></div>')
    return shell("AI 助手设置", body, user, "/admin/settings")


def page_users(conn, user, err="", msg=""):
    rows = store.list_users(conn)
    uopts = "".join('<option value="%s">%s（%s）</option>'
                    % (esc(u["username"]), esc(u["name"]), esc(u["username"]))
                    for u in rows)
    trs = "".join(
        '<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>'
        % (esc(u["username"]), esc(u["name"]),
           "管理员" if u["role"] == "admin" else "成员",
           "可维护" if u["can_maintain"] else "只读",
           esc(fmt_time(u["created_at"])))
        for u in rows)
    return shell("账号管理", (
        '<div class="wrap page"><div class="page-head"><h1>账号管理</h1>'
        '<span class="muted">每位使用者单独创建账号；不开放自助注册</span></div>'
        + ('<div class="err">%s</div>' % esc(err) if err else "")
        + ('<div class="ok">%s</div>' % esc(msg) if msg else "")
        + '<div class="panel"><table class="tbl"><thead><tr><th>账号</th><th>姓名</th>'
        '<th>角色</th><th>维护权限</th><th>创建时间</th></tr></thead><tbody>%s</tbody></table></div>'
        '<div class="section-head"><h2>新建账号</h2></div>'
        '<div class="panel"><div class="panel-body">'
        '<form method="post" action="/admin/users" style="max-width:460px">'
        '<div class="form-row"><label>账号（英文/数字）</label>'
        '<input type="text" name="username" required placeholder="例：cui"></div>'
        '<div class="form-row"><label>姓名</label>'
        '<input type="text" name="name" required placeholder="例：崔冰冰"></div>'
        '<div class="form-row"><label>初始密码（至少 6 位）</label>'
        '<input type="password" name="password" autocomplete="new-password" required></div>'
        '<div class="form-row"><label>权限</label>'
        '<select name="perm"><option value="maintain">成员 · 可维护（能上传、发新版本）</option>'
        '<option value="read">成员 · 只读（只能浏览下载）</option>'
        '<option value="admin">管理员</option></select></div>'
        '<button class="btn primary">创建账号</button></form></div></div>'
        '<div class="section-head"><h2>重置密码</h2>'
        '<span class="muted">同事忘了密码时用这里，重置后把新密码告诉本人</span></div>'
        '<div class="panel"><div class="panel-body">'
        '<form method="post" action="/admin/users" style="max-width:460px">'
        '<input type="hidden" name="action" value="reset">'
        '<div class="form-row"><label>账号</label><select name="username">%s</select></div>'
        '<div class="form-row"><label>新密码（至少 6 位）</label>'
        '<input type="password" name="password" autocomplete="new-password" required></div>'
        '<button class="btn">重置密码</button></form></div></div></div>'
        % (trs, uopts)), user, "/admin/users")


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "JuMaSkillHub/0.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    # -- 基础工具 ---------------------------------------------------------
    def send_bytes(self, status, data, ctype, headers=None, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if filename:
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''%s" % quote(filename))
        for k, v in (headers or []):
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_html(self, body, status=200, headers=None, user=None):
        data = body.encode("utf-8")
        self.send_bytes(status, data, "text/html; charset=utf-8", headers)

    def send_json(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_bytes(status, data, "application/json; charset=utf-8")

    def redirect(self, location, set_cookie=None):
        headers = [("Location", location)]
        if set_cookie:
            headers.append(("Set-Cookie", set_cookie))
        self.send_response(303)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def cookie(self, name):
        raw = self.headers.get("Cookie", "")
        jar = SimpleCookie()
        try:
            jar.load(raw)
        except Exception:
            return ""
        return jar[name].value if name in jar else ""

    def current(self, conn):
        if not hasattr(self, "_user"):
            self._token = self.cookie("sid")
            self._user = store.session_user(conn, self._token)
        return self._user

    # -- 路由 -------------------------------------------------------------
    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def handle_any(self, method):
        parts = urlsplit(self.path)
        path = parts.path.rstrip("/") or "/"
        query = parse_qs(parts.query)
        try:
            with store.db() as conn:
                self.route(conn, method, path, query)
        except store.IngestError as exc:
            self.send_html(shell("出错了", '<div class="wrap page"><div class="err">%s</div>'
                                        '<a class="btn" href="/upload">返回上传页</a></div>' % esc(str(exc))),
                           status=400)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self.send_html(shell("服务器错误", '<div class="wrap page"><div class="err">'
                                             '服务器内部错误，请把控制台日志发给管理员。</div></div>'),
                           status=500)

    def route(self, conn, method, path, query):
        if path.startswith("/static/"):
            name = path[len("/static/"):]
            if "/" in name or ".." in name:
                return self.send_bytes(404, b"not found", "text/plain")
            f = STATIC / name
            if f.exists():
                ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
                if name.endswith(".css"):
                    ctype = "text/css; charset=utf-8"
                if name.endswith(".js"):
                    ctype = "application/javascript; charset=utf-8"
                return self.send_bytes(200, f.read_bytes(), ctype,
                                       [("Cache-Control", "max-age=3600")])
            return self.send_bytes(404, b"not found", "text/plain")

        if path == "/login":
            if method == "POST":
                return self.do_login(conn)
            return self.send_html(page_login())

        if path == "/logout":
            store.drop_session(conn, self.cookie("sid"))
            return self.redirect("/login", "sid=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")

        user = self.current(conn)
        if user is None:
            return self.redirect("/login")

        if path == "/":
            return self.send_html(page_home(conn, user))

        if path == "/api/ask" and method == "POST":
            return self.do_ask(conn, user)

        if path == "/agent":
            sid = ""
            if "sid" in query and query["sid"]:
                sid = query["sid"][0]
            return self.send_html(page_agent(conn, user, sid, bool(query.get("open"))))

        if path == "/api/agent/ask" and method == "POST":
            return self.do_agent(conn, user)

        if path == "/admin/settings":
            if not store.is_admin(user):
                return self.send_html(shell("没有权限", '<div class="wrap page"><div class="err">'
                                            '只有管理员能改 AI 设置。</div></div>', user), status=403)
            if method == "POST":
                return self.do_save_ai_settings(conn, user)
            return self.send_html(page_ai_settings(conn, user))

        if path == "/admin/sync":
            if not store.is_admin(user):
                return self.send_html(shell("没有权限",
                                            '<div class="wrap page"><div class="err">只有管理员能做同步。</div></div>',
                                            user), status=403)
            form = self.read_form() if method == "POST" else {}
            return self.send_html(sync_page.render(conn, user, form, shell))

        if path == "/resources":
            return self.send_html(page_resources(conn, user, query))

        m = re.match(r"^/r/(\d+)$", path)
        if m:
            rid = int(m.group(1))
            r = store.get_resource(conn, rid)
            if not r:
                return self.send_html(shell("找不到", '<div class="wrap page"><div class="empty">'
                                                     '资源不存在或已删除。</div></div>'), status=404)
            store.log(conn, user["id"], "view", rid)
            try:
                if r["kind"] == "skill" and r["current_version_id"]:
                    page = skill_detail(conn, r, query, user)
                elif r["kind"] == "skill":
                    page = ext_skill_detail(conn, r, user)
                else:
                    page = preset_detail(conn, r, user)
            except Exception:
                tb = traceback.format_exc()
                try:
                    with open(store.DATA / "logs" / "error.log", "a", encoding="utf-8") as fh:
                        fh.write("[r/%d] %s%s%s" % (rid, time.strftime("%Y-%m-%d %H:%M:%S"), chr(10), tb))
                except Exception:
                    pass
                log(tb)
                page = ('<div class="wrap page"><div class="empty"><div class="big">!</div>'
                        '这条资源的内容渲染失败（已记录到 data/logs/error.log）。<br>'
                        '可以先下载原包查看，或联系管理员修数据。</div>'
                        '<div style="text-align:center;margin-top:16px">'
                        '<a class="btn" href="/resources">← 返回资源库</a></div></div></div>')
            return self.send_html(shell(r["title"], page, user, "/resources"))

        m = re.match(r"^/cover/([A-Za-z0-9_-]{6,80}\.(?:png|jpg|jpeg|webp|gif))$", path, re.I)
        if m:
            f = store.DATA / "covers" / m.group(1)
            if f.exists():
                ctype = mimetypes.guess_type(str(f))[0] or "image/png"
                return self.send_bytes(200, f.read_bytes(), ctype,
                                       [("Cache-Control", "max-age=86400")])
            return self.send_bytes(404, b"not found", "text/plain")

        m = re.match(r"^/download/(\d+)$", path)
        if m:
            return self.do_download(conn, int(m.group(1)), query, user)

        if path == "/upload":
            if method == "POST":
                return self.do_upload(conn, user)
            token = (query.get("token") or [""])[0]
            if token:
                data = store.staged_zip(token)
                if data:
                    try:
                        info = store.inspect_zip(data)
                    except store.IngestError:
                        data = None
                    else:
                        sug = ai.suggest(conn, info, filename=store.staged_name(token) or "package.zip")
                        return self.send_html(page_upload(conn, user, staged=token, info=info,
                                                          sug=sug, filename=store.staged_name(token) or "package.zip"))
                return self.send_html(page_upload(conn, user, err="这次上传已过期（超过 24 小时或已入库），请重新上传"),
                                      status=400)
            return self.send_html(page_upload(conn, user, rid=(query.get("rid") or [""])[0]))

        if path == "/account/password":
            if method == "POST":
                return self.do_change_password(conn, user)
            return self.redirect("/me")

        if path == "/me":
            return self.send_html(page_me(conn, user))

        if path == "/insights":
            if not store.is_admin(user):
                return self.send_html(shell("无权访问", '<div class="wrap page"><div class="empty">'
                                                       '只有管理员能看数据概览。</div></div>'), status=403)
            return self.send_html(page_insights(conn, user))

        if path == "/admin/users":
            if not store.is_admin(user):
                return self.send_html(shell("无权访问", '<div class="wrap page"><div class="empty">'
                                                       '只有管理员能管理账号。</div></div>'), status=403)
            if method == "POST":
                return self.do_create_user(conn, user)
            return self.send_html(page_users(conn, user))

        return self.send_html(shell("404", '<div class="wrap page"><div class="empty">'
                                           '<div class="big">404</div>页面不存在。</div></div>'), status=404)

    # -- 动作 -------------------------------------------------------------
    def do_agent(self, conn, user):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n > 0 else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            data = {}
        q = (data.get("q") or "").strip()[:400]
        sid = data.get("sid") or ""
        if not q:
            return self.send_json(400, {"ok": False, "error": "问题为空"})
        now = time.time()
        if now - _ASK_TS.get(user["id"], 0) < 1.2:
            return self.send_json(429, {"ok": False, "error": "慢一点，1.2 秒一次"})
        _ASK_TS[user["id"]] = now
        try:
            sess = store.agent_session(conn, user["id"], sid, title=q[:40])
        except Exception:  # noqa: BLE001
            sess = store.agent_session(conn, user["id"], None, title=q[:40])
        hist = store.agent_history(conn, sess["id"], 12)
        history = [{"role": m["role"], "content": m["content"]} for m in hist
                   if m["role"] in ("user", "assistant") and m["content"]]
        store.agent_add_message(conn, sess["id"], "user", q)
        res = agent.run(conn, user, q, history)
        if res.get("ok"):
            store.agent_add_message(conn, sess["id"], "assistant", res.get("answer", ""),
                                    res.get("steps") or [], res.get("sources") or [],
                                    res.get("mode", ""), res.get("elapsed_ms") or 0)
        res["sid"] = sess["id"]
        store.log(conn, user["id"], "agent-ask", None, q[:80])
        return self.send_json(200, res)

    def do_ask(self, conn, user):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n > 0 else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            data = {}
        q = (data.get("q") or "").strip()[:500]
        if not q:
            return self.send_json(400, {"ok": False, "error": "问题为空"})
        now = time.time()
        if now - _ASK_TS.get(user["id"], 0) < 1.5:
            return self.send_json(429, {"ok": False, "error": "慢一点，1.5 秒一次"})
        _ASK_TS[user["id"]] = now
        res = ai.ask(conn, q)
        store.log(conn, user["id"], "ai-ask", None, q[:80])
        return self.send_json(200, res)

    def do_save_ai_settings(self, conn, user):
        form = self.read_form()
        try:
            ai.save_config(form.get("base_url", ""), form.get("model", ""), form.get("api_key", ""),
                           clear=bool(form.get("clear_key")))
        except Exception as exc:  # noqa: BLE001
            return self.send_html(page_ai_settings(conn, user, err="保存失败：%s" % exc), status=400)
        store.log(conn, user["id"], "ai-config")
        return self.send_html(page_ai_settings(conn, user, msg="已保存，回去点右下角「问 AI」试试"))

    def do_login(self, conn):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        form = {k: v[0] for k, v in parse_qs(raw).items()}
        username = (form.get("username") or "").strip()
        password = form.get("password") or ""
        row = store.get_user_by_name(conn, username)
        if not row or not store.check_password(row, password):
            return self.send_html(page_login(err="账号或密码不对，再试一次。"), status=401)
        token = store.new_session(conn, row["id"])
        store.log(conn, row["id"], "login")
        return self.redirect("/", "sid=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=604800" % token)

    def do_change_password(self, conn, user):
        form = self.read_form()
        old, new1, new2 = (form.get("old") or ""), (form.get("new1") or ""), (form.get("new2") or "")
        if not store.check_password(user, old):
            return self.send_html(page_me(conn, user, err="当前密码不对，再试一次。"), status=400)
        if len(new1) < 6:
            return self.send_html(page_me(conn, user, err="新密码至少 6 位。"), status=400)
        if new1 != new2:
            return self.send_html(page_me(conn, user, err="两次输入的新密码不一致。"), status=400)
        store.set_password(conn, user["id"], new1)
        store.log(conn, user["id"], "password")
        return self.send_html(page_me(conn, user, msg="密码已更新，下次登录请用新密码。"))

    def read_form(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        return {k: v[0] for k, v in parse_qs(raw).items()}

    def do_download(self, conn, rid, query, user):
        r = store.get_resource(conn, rid)
        if not r:
            return self.send_bytes(404, b"not found", "text/plain")
        versions = store.get_versions(conn, rid)
        if not versions:
            return self.send_bytes(404, b"no package", "text/plain")
        seq = int((query.get("v") or ["0"])[0] or 0)
        ver = next((v for v in versions if v["seq"] == seq), versions[0])
        p = store.zip_abs_path(ver)
        if not p:
            return self.send_bytes(404, b"file missing", "text/plain")
        store.log(conn, user["id"], "download", rid, "%s v%d" % (ver["zip_name"] or "", ver["seq"]))
        name = ver["zip_name"] or ("%s-v%d.zip" % (r["title"], ver["seq"]))
        return self.send_bytes(200, p.read_bytes(), "application/zip", filename=name)

    def do_create_user(self, conn, user):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        form = {k: v[0] for k, v in parse_qs(raw).items()}
        if (form.get("action") or "") == "reset":
            uname = (form.get("username") or "").strip()
            pw = (form.get("password") or "").strip()
            target = store.get_user_by_name(conn, uname)
            if not target:
                return self.send_html(page_users(conn, user, err="没有这个账号"), status=400)
            if len(pw) < 6:
                return self.send_html(page_users(conn, user, err="密码至少 6 位"), status=400)
            store.set_password(conn, target["id"], pw)
            store.log(conn, user["id"], "reset-password", detail=uname)
            return self.send_html(page_users(conn, user,
                                             msg="已把 %s 的密码重置为 %s" % (uname, pw)))
        uname = (form.get("username") or "").strip()
        name = (form.get("name") or "").strip()
        pw = (form.get("password") or "").strip()
        perm = form.get("perm") or "read"
        if not re.match(r"^[a-zA-Z0-9_.-]{2,32}$", uname):
            return self.send_html(page_users(conn, user, err="账号只能是英文/数字，2–32 位"), status=400)
        if not name or not pw:
            return self.send_html(page_users(conn, user, err="姓名和密码不能为空"), status=400)
        if store.get_user_by_name(conn, uname):
            return self.send_html(page_users(conn, user, err="这个账号已存在"), status=400)
        role = "admin" if perm == "admin" else "member"
        can = 1 if perm in ("admin", "maintain") else 0
        store.create_user(conn, uname, name, pw, role=role, can_maintain=can)
        return self.send_html(page_users(conn, user, msg="已创建账号 %s（%s），初始密码 %s" % (uname, name, pw)))

    def do_upload(self, conn, user):
        if not store.can_maintain(user):
            return self.send_html(shell("无权上传", '<div class="wrap page"><div class="empty">'
                                                   '只有可维护成员能上传。</div></div>'), status=403)
        ctype = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length") or 0)
        if length > store.MAX_ZIP_BYTES + 1024 * 1024:
            return self.send_html(page_upload(conn, user, err="上传内容超过 200MB"), status=413)
        body = self.rfile.read(length)
        if "multipart/form-data" in ctype:
            boundary = ctype.split("boundary=", 1)[-1].strip().strip('"')
            fields, files = parse_multipart(body, boundary)
        else:
            qs = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
            fields = {k: v[0] for k, v in qs.items()}
            files = {}
        action = (fields.get("action") or "direct").strip()

        # 第一步：只读包，给建议，暂存文件
        if action == "analyze":
            if "zip" not in files or not files["zip"][1]:
                return self.send_html(page_upload(conn, user, err="请选择 ZIP 文件"), status=400)
            filename, data = files["zip"]
            try:
                info = store.inspect_zip(data)
            except store.IngestError as exc:
                return self.send_html(page_upload(conn, user, err=str(exc)), status=400)
            store.sweep_staging()
            token = store.stage_zip(data)
            store.note_staged_name(token, filename)
            sug = ai.suggest(conn, info, filename=filename, hint=fields.get("hint", ""))
            return self.send_html(page_upload(conn, user, staged=token, info=info,
                                              sug=sug, filename=filename))

        # 第二步：按确认后的字段入库
        if action == "confirm":
            token = fields.get("staged", "")
            data = store.staged_zip(token)
            if not data:
                return self.send_html(page_upload(conn, user, err="上传的文件已过期，请重新上传"), status=400)
            platform = "tv" if (fields.get("platform") or "flow") == "tv" else "flow"
            try:
                rid, seq, short, info = store.ingest_zip(
                    conn, data, fields.get("filename") or "package.zip", user,
                    platform=platform, grp=fields.get("grp") or "", cat=fields.get("cat") or "",
                    title=fields.get("title") or "", note=fields.get("note") or "",
                    tags=fields.get("tags") or "DSH", summary=fields.get("summary") or "")
            except store.IngestError as exc:
                return self.send_html(page_upload(conn, user, err=str(exc)), status=400)
            store.drop_staged(token)
            return self.redirect("/r/%d?uploaded=%d" % (rid, seq))

        # 给已有 Skill 发新版本 / 兼容旧表单
        if "zip" not in files or not files["zip"][1]:
            return self.send_html(page_upload(conn, user, rid=fields.get("rid", ""), err="请选择 ZIP 文件"), status=400)
        filename, data = files["zip"]
        platform = "tv" if (fields.get("platform") or "flow") == "tv" else "flow"
        grp = fields.get("grp") or fields.get("grp_tv") or fields.get("grp_flow") or ""
        cat = fields.get("cat") or fields.get("cat_tv") or fields.get("cat_flow") or ""
        try:
            rid, seq, short, info = store.ingest_zip(
                conn, data, filename, user, platform=platform, grp=grp, cat=cat,
                title=fields.get("title") or "", note=fields.get("note") or "",
                tags=fields.get("tags") or None, summary=fields.get("summary") or None)
        except store.IngestError as exc:
            return self.send_html(page_upload(conn, user, rid=fields.get("rid", ""), err=str(exc)), status=400)
        return self.redirect("/r/%d?uploaded=%d" % (rid, seq))


def parse_multipart(body, boundary):
    delim = b"--" + boundary.encode("latin-1")
    fields, files = {}, {}
    chunks = body.split(delim)
    for part in chunks[1:]:
        if part.startswith(b"--"):
            break
        part = part.lstrip(b"\r\n")
        head, sep, data = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]
        head_txt = head.decode("utf-8", "replace")
        m = re.search(r'name="([^"]*)"', head_txt)
        if not m:
            continue
        key = m.group(1)
        fm = re.search(r'filename="([^"]*)"', head_txt)
        if fm:
            files[key] = (fm.group(1), data)
        else:
            fields[key] = data.decode("utf-8", "replace")
    return fields, files


def lan_ips():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ip = info[4][0]
            if ":" not in ip and not ip.startswith("127."):
                ips.add(ip)
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    return sorted(ips)


def port_free(port):
    """Windows 的 http.server 会带 SO_REUSEADDR，直接 bind 可能"假成功"，所以先探一次。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def main():
    store.init_storage()
    addr = ("", PORT)
    if not port_free(PORT):
        print("=" * 62)
        print(" 启动失败：端口 %d 已经被占用（多半是已经开了一个窗口在跑）。" % PORT)
        print(" 先关掉那个黑窗口；或者换端口：命令行里 set PORT=8010 再双击 start.bat。")
        print("=" * 62)
        sys.exit(1)
    try:
        httpd = ThreadingHTTPServer(addr, Handler)
    except OSError as exc:
        print("=" * 62)
        print(" 启动失败：端口 %d 用不了（%s）" % (PORT, exc))
        print(" 多半是已经开了一个窗口在跑。先关掉那个黑窗口，或者换一个端口：")
        print("   set PORT=8010   然后重新双击 start.bat")
        print("=" * 62)
        sys.exit(1)
    httpd.daemon_threads = True
    print("=" * 62)
    print(" 灵感引擎 · Skill 预设库  已启动")
    print(" 本机访问：http://127.0.0.1:%d" % PORT)
    for ip in lan_ips():
        print(" 网络访问：http://%s:%d" % (ip, PORT))
    print(" 数据目录：%s" % store.DATA)
    print(" 关闭这个窗口 = 停止服务。其他设备打不开时，先看窗口是否还开着。")
    print("=" * 62)
    if os.environ.get("AUTO_OPEN", "0") == "1":
        try:
            import threading as _th
            import webbrowser
            _th.Timer(1.2, lambda: webbrowser.open("http://127.0.0.1:%d/" % PORT)).start()
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
