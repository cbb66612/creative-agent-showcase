# -*- coding: utf-8 -*-
"""从 TV 管理后台同步「预设 / 技能」到本地库。

数据源：https://dz-ailab.dzkjm.cn  （Canvas 管理后台）
接口前缀：/api-data/api/v1   登录态靠 Cookie（会话 Cookie）

用法（都在 lan 目录下执行）：

  1) 先探一下接口长什么样（不写库）
     runtime\\python.exe sync_tv.py --probe

  2) 干跑，看看会新增/更新多少条（不写库）
     runtime\\python.exe sync_tv.py --dry-run

  3) 正式同步（会下载封面到 data/covers/，并写库）
     runtime\\python.exe sync_tv.py

Cookie 三种给法，优先级从高到低：
  --cookie "name=value; name2=value2"
  --cookie-file data/tv_cookie.txt     （一行，就是浏览器里复制的 Cookie）
  环境变量 TV_COOKIE

只同步预设：--only presets      只同步技能：--only skills
"""

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import store  # noqa: E402

TV_BASE = os.environ.get("TV_BASE", "https://dz-ailab.dzkjm.cn").rstrip("/")
TV_API_PREFIX = os.environ.get("TV_API_PREFIX", "/api-data/api/v1")
TV_API = TV_BASE + TV_API_PREFIX
TV_PAGE_SIZE = 100
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")


class SyncError(Exception):
    pass


# ---------------------------------------------------------------- 取 cookie

def from_curl(text):
    """允许直接粘贴浏览器「Copy as cURL」，从中抽出 Cookie。"""
    t = (text or "").strip()
    if not t.lower().startswith("curl"):
        return t
    import re
    m = re.search(r"(?i)-H\s+'cookie:\s*([^']+)'", t) or re.search(r'(?i)-H\s+"cookie:\s*([^"]+)"', t)
    if m:
        return m.group(1).strip()
    m = re.search(r"(?i)-b\s+'([^']+)'", t) or re.search(r'(?i)-b\s+"([^"]+)"', t)
    if m:
        return m.group(1).strip()
    return t


def read_cookie(args):
    if args.cookie:
        return from_curl(args.cookie.strip())
    path = args.cookie_file or os.path.join(BASE_DIR, "data", "tv_cookie.txt")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            text = f.read().strip()
        if text:
            return from_curl(text)
    env = os.environ.get("TV_COOKIE", "").strip()
    if env:
        return from_curl(env)
    raise SyncError("没有 Cookie。请从浏览器复制 Cookie 再试：\n"
                    "  1. 浏览器登录 " + TV_BASE + "/admin-web/presets\n"
                    "  2. F12 → Network → 任选一个 api-data 请求 → 复制请求头里的 Cookie\n"
                    "  3. 存成 " + os.path.join(BASE_DIR, "data", "tv_cookie.txt") + "（一行）\n"
                    "  或者用 --cookie \"...\" 传进来")


# ---------------------------------------------------------------- HTTP

def api_get(path, cookie, params=None, raw=False):
    url = TV_API + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json, text/plain, */*")
    req.add_header("Referer", TV_BASE + "/admin-web/presets")
    req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            raise SyncError("HTTP %s：%s" % (e.code, body[:200]))
        if data.get("code") == 401:
            raise SyncError("登录态失效（401）。重新登录后台再复制一份 Cookie。")
        raise SyncError("HTTP %s：%s" % (e.code, json.dumps(data, ensure_ascii=False)[:300]))
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:
        raise SyncError("返回的不是 JSON（可能被重定向到登录页）：%s" % body[:200])
    if isinstance(data, dict) and data.get("code") not in (None, 0, 200, "0", "200"):
        if data.get("code") in (401, 403):
            raise SyncError("登录态失效（%s）：%s" % (data.get("code"), data.get("message")))
        raise SyncError("接口报错：%s" % json.dumps(data, ensure_ascii=False)[:300])
    return data


def pick(obj, *keys, default=None):
    for k in keys:
        if isinstance(obj, dict) and k in obj and obj[k] not in (None, ""):
            return obj[k]
    return default


def list_of(data):
    """把各种分页返回统一成 (items, total)。"""
    d = data.get("data", data) if isinstance(data, dict) else data
    total = None
    if isinstance(d, dict):
        total = pick(d, "total", "totalElements", "totalCount", "count")
        for key in ("records", "list", "items", "rows", "content", "data"):
            v = d.get(key)
            if isinstance(v, list):
                return v, total
        # 有些接口直接 {a:{list:[...]}}
        for v in d.values():
            if isinstance(v, list):
                return v, total
        return [], total
    if isinstance(d, list):
        return d, total
    return [], total


def fetch_all(path, cookie, params=None):
    params = dict(params or {})
    out, page = [], 1
    while True:
        params.update({"page": page, "pageSize": TV_PAGE_SIZE})
        data = api_get(path, cookie, params)
        items, total = list_of(data)
        items = [x for x in items if isinstance(x, dict)]
        out.extend(items)
        if not items or len(items) < TV_PAGE_SIZE:
            break
        if total and len(out) >= int(total):
            break
        page += 1
        if page > 200:
            break
    return out


# ---------------------------------------------------------------- 封面

def download_cover(conn, url_path, key):
    """把封面下载到 data/covers/，返回站内 /cover/... 地址；失败返回空串。"""
    if not url_path:
        return ""
    url = url_path if str(url_path).startswith("http") else TV_BASE + str(url_path)
    ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower() or ".png"
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        ext = ".png"
    name = "tv-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16] + ext
    dest_dir = os.path.join(store.DATA, "covers")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, name)
    if not os.path.exists(dest) or os.path.getsize(dest) < 800:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", UA)
        req.add_header("Referer", TV_BASE + "/")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
        except Exception as e:
            print("    ! 封面下载失败 %s（%s）" % (url_path, e))
            return ""
        if len(data) < 800:
            print("    ! 封面太小，跳过 %s" % url_path)
            return ""
        with open(dest, "wb") as f:
            f.write(data)
    return "/cover/" + name


# ---------------------------------------------------------------- 映射

def emoji_of(text):
    t = text or ""
    table = [("角色", "🧑‍🎤"), ("场景", "🏞"), ("道具", "🧰"), ("分镜", "🎬"), ("镜头", "🎥"),
             ("风格", "🎨"), ("安全", "🛡"), ("画质", "✨"), ("音乐", "🎵"), ("配音", "🎙"),
             ("文本", "📝"), ("剧本", "📖"), ("通用", "🧩")]
    for k, v in table:
        if k in t:
            return v
    return "📄"


def hue_of(text):
    h = hashlib.md5((text or "x").encode("utf-8")).hexdigest()
    return int(h[:6], 16) % 360


def feature_map(cookie):
    """featureId -> 功能名（文生图 / 图生图 / 多模态生视频 ...）"""
    try:
        items = fetch_all("/features", cookie)
    except SyncError as e:
        print("  ! 取功能列表失败：%s" % e)
        return {}
    m = {}
    for it in items:
        fid = pick(it, "id", "featureId")
        name = pick(it, "name", "label", "featureName", "code")
        if fid is not None and name:
            m[str(fid)] = str(name)
    return m


def map_preset(it, feats):
    fid = pick(it, "featureId", "feature")
    grp = feats.get(str(fid)) or pick(it, "featureName", "feature") or "预设"
    rid = pick(it, "id")
    name = pick(it, "presetName", "name", "title") or ""
    return {
        "source_key": "tv:preset:%s" % rid,
        "external_key": "tv-preset-%s" % rid,
        "kind": "preset",
        "platform": "tv",
        "grp": grp,
        "cat": pick(it, "category", "categoryName") or "",
        "title": name,
        "summary": pick(it, "presetDescription", "description", "desc") or "",
        "body": pick(it, "promptTemplate", "prompt", "content") or "",
        "usage_count": pick(it, "useCount", "usageCount", "usedCount") or 0,
        "cover_path": pick(it, "coverImage", "cover", "coverUrl", "image"),
        "status": "active" if pick(it, "isEnabled", "enabled", default=True) else "hidden",
        "source": "TV 后台",
        "source_url": TV_BASE + "/admin-web/presets",
        "raw": it,
    }


def map_skill(it, feats):
    sid = pick(it, "id")
    name = pick(it, "skillName", "name", "title", "label") or ""
    return {
        "source_key": "tv:skill:%s" % sid,
        "external_key": "tv-skill-%s" % sid,
        "kind": "skill",
        "platform": "tv",
        "grp": pick(it, "skillType", "type", "categoryName") or "TV Skill",
        "cat": pick(it, "category", "categoryName", "tag") or "",
        "title": name,
        "summary": pick(it, "description", "skillDescription", "desc") or "",
        "body": pick(it, "content", "promptTemplate", "skillContent", "body") or "",
        "usage_count": pick(it, "useCount", "usageCount") or 0,
        "cover_path": pick(it, "coverImage", "cover", "icon"),
        "status": "active" if pick(it, "isEnabled", "enabled", "status", default=True) not in (False, 0, "0", "disabled") else "hidden",
        "source": "TV 后台",
        "source_url": TV_BASE + "/admin-web",
        "raw": it,
    }


def upsert(conn, item, cover_url):
    fields = dict(item)
    fields.pop("raw", None)
    fields.pop("cover_path", None)
    fields["cover_url"] = cover_url
    fields["hue"] = hue_of(item["cat"] + item["grp"])
    fields["emoji"] = emoji_of(item["cat"] + item["grp"] + item["title"])
    fields["tags"] = "TV同步"
    return store.upsert_external(conn, fields)


# ---------------------------------------------------------------- 主流程

def run(kind, cookie, dry_run=False, raw_dir=""):
    with store.db() as conn:
        _run_with(conn, kind, cookie, dry_run, raw_dir)


def _load_raw(raw_dir):
    def load(name):
        p = os.path.join(raw_dir, name + ".json")
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return load


def _run_with(conn, kind, cookie, dry_run, raw_dir=""):
    store.migrate(conn)
    if raw_dir:
        load = _load_raw(raw_dir)
        feats = {}
        for it in load("features"):
            fid = pick(it, "id", "featureId")
            name = pick(it, "name", "label", "featureName", "code")
            if fid is not None and name:
                feats[str(fid)] = str(name)
    else:
        feats = feature_map(cookie)
    print("功能表：%s" % (", ".join(sorted(set(feats.values()))) or "（空）"))

    plan = []
    if kind in ("all", "presets"):
        rows = load("presets") if raw_dir else fetch_all("/presets", cookie)
        print("后台预设：%d 条" % len(rows))
        plan += [map_preset(r, feats) for r in rows]
    if kind in ("all", "skills"):
        rows = load("skills") if raw_dir else fetch_all("/skills", cookie)
        print("后台技能：%d 条" % len(rows))
        plan += [map_skill(r, feats) for r in rows]

    added = updated = skipped = 0
    for i, item in enumerate(plan, 1):
        if not item["title"]:
            skipped += 1
            continue
        if dry_run:
            exists = conn.execute("SELECT id FROM resources WHERE source_key=?",
                                  (item["source_key"],)).fetchone()
            print("  %s %-5s %-12s %s" % ("更新" if exists else "新增", item["kind"],
                                          item["grp"][:12], item["title"][:30]))
            continue
        cover = download_cover(conn, item["cover_path"], item["source_key"])
        act = upsert(conn, item, cover)
        if act == "added":
            added += 1
        else:
            updated += 1
        if i % 20 == 0:
            conn.commit()
            print("  ... 已处理 %d/%d" % (i, len(plan)))
    if not dry_run:
        conn.commit()
        print("完成：新增 %d，更新 %d，跳过 %d" % (added, updated, skipped))
    else:
        print("干跑结束（未写库）。共 %d 条待处理。" % len(plan))


def probe(cookie):
    print("接口：%s" % TV_API)
    print()
    print("① 会话检查")
    try:
        data = api_get("/auth/session", cookie)
        print("   ", json.dumps(data.get("data"), ensure_ascii=False)[:300])
    except SyncError as e:
        print("    !", e)
        return
    for path in ("/features", "/preset-categories", "/presets", "/skills"):
        print("\n② %s" % path)
        try:
            data = api_get(path, cookie, {"page": 1, "pageSize": 2})
        except SyncError as e:
            print("    !", e)
            continue
        items, total = list_of(data)
        print("    total=%s，本页 %d 条" % (total, len(items)))
        if items:
            print("    字段：%s" % ", ".join(sorted(items[0].keys())))
            print("    样例：%s" % json.dumps(items[0], ensure_ascii=False)[:600])


def main():
    ap = argparse.ArgumentParser(description="TV 后台 → 本地库同步")
    ap.add_argument("--cookie", default="")
    ap.add_argument("--cookie-file", default="")
    ap.add_argument("--only", choices=["all", "presets", "skills"], default="all")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--from-dir", default="", help="从 tv_dump.py 导出的目录读数据（不需要 Cookie）")
    ap.add_argument("--base", default="", help="后台域名，默认 https://dz-ailab.dzkjm.cn")
    ap.add_argument("--api-prefix", default="", help="接口前缀，默认 /api-data/api/v1")
    args = ap.parse_args()
    global TV_BASE, TV_API
    if args.base:
        TV_BASE = args.base.rstrip("/")
        TV_API = TV_BASE + TV_API_PREFIX
    if args.api_prefix:
        TV_API = TV_BASE + args.api_prefix.rstrip("/")
    try:
        if args.from_dir:
            run(args.only, "", args.dry_run, raw_dir=args.from_dir)
            return
        cookie = read_cookie(args)
        if args.probe:
            probe(cookie)
        else:
            run(args.only, cookie, args.dry_run)
    except SyncError as e:
        print("\n[失败] %s" % e)
        sys.exit(1)


if __name__ == "__main__":
    main()
