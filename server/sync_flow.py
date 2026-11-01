"""从 DramaFlow 后台同步 Skill 列表到本地库。

用法：
  python sync_flow.py --token <maker-admin-token> [--dry-run] [--base https://makerflow-agent.kvread.com]
  python sync_flow.py --token-file data/flow_token.txt

token 从浏览器后台的 localStorage 取：
  makerflow-agent.kvread.com → F12 → Console → localStorage.getItem('maker-admin-token')
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
sys.path.insert(0, str(BASE))

import store  # noqa: E402

DEFAULT_BASE = "https://makerflow-agent.kvread.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

# Flow 后台的阶段 -> 本地库的「分类」提示
STAGE_HINT = {
    "剧本": "剧本预审",
    "分析手册": "剧本手册",
    "资产生成": "道具",
    "单集 Agent": "成片主控",
}


class SyncError(Exception):
    pass


def from_curl(text):
    """允许粘贴浏览器「Copy as cURL」，从中抽出 Bearer token。"""
    t = (text or "").strip()
    if not t.lower().startswith("curl"):
        return t
    import re
    m = re.search(r"(?i)-H\s+'authorization:\s*bearer\s+([^']+)'", t)
    if not m:
        m = re.search(r'(?i)-H\s+"authorization:\s*bearer\s+([^"]+)"', t)
    if m:
        return m.group(1).strip()
    m = re.search(r"eyJ[A-Za-z0-9_.\-]{20,}", t)
    return m.group(0) if m else t


def api_get(base, path, token, timeout=30):
    url = base.rstrip("/") + path
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": UA,
        "Authorization": "Bearer " + token,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        if e.code == 401:
            raise SyncError("登录态失效（401）。请在后台重新登录，再复制新的 token。")
        raise SyncError("接口返回 %d：%s" % (e.code, body))
    except urllib.error.URLError as e:
        raise SyncError("连不上后台：%s" % e.reason)


def api_get_bytes(base, path, token, timeout=120):
    url = base.rstrip("/") + path
    req = urllib.request.Request(url, headers={
        "Accept": "application/octet-stream, application/zip, */*",
        "User-Agent": UA,
        "Authorization": "Bearer " + token,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise SyncError("登录态失效（401）。请在后台重新登录，再复制新的 token。")
        if e.code in (404, 400):
            return None
        raise SyncError("下载包返回 %d" % e.code)
    except urllib.error.URLError as e:
        raise SyncError("连不上后台：%s" % e.reason)


def unwrap(j):
    """兼容 {ok,data:{list:[]}} / {data:[]} / [] 几种返回。"""
    if isinstance(j, list):
        return j
    if not isinstance(j, dict):
        return []
    for key in ("data", "list", "items", "records", "rows", "content"):
        v = j.get(key)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            for k2 in ("list", "items", "records", "rows", "content"):
                if isinstance(v.get(k2), list):
                    return v[k2]
    return []


STAGE_MAP = {
    "format": ("剧本", "剧本格式"),
    "diagnose": ("剧本", "剧本预审"),
    "prompt": ("剧本", "分镜提示词"),
    "script-analysis": ("分析手册", "剧本手册"),
    "asset": ("资产生成", "资产"),
    "setting": ("资产生成", "设定"),
    "video": ("单集 Agent", "视频生成"),
    "episode-agent": ("单集 Agent", "成片主控"),
}
ASSET_MAP = {"character": "角色", "scene": "场景", "prop": "道具", "": ""}


def map_item(it):
    name = (it.get("skillName") or it.get("name") or "").strip()
    if not name:
        return None
    if (it.get("scope") or "").strip() == "project":
        return None          # 项目自己上传的实例，不进公共库
    stage = (it.get("stage") or "").strip()
    grp, cat = STAGE_MAP.get(stage, ("其他", stage or "未分组"))
    asset = (it.get("assetType") or "").strip()
    if asset:
        cat = ASSET_MAP.get(asset, cat)
        if grp == "其他":
            grp = "资产生成"
    hidden = bool(it.get("adminHidden")) or it.get("enabled") is False
    version = (it.get("packageVersion") or it.get("version") or "").strip()
    title = (it.get("title") or "").strip() or name
    desc = (it.get("description") or "").strip()
    if not desc:
        desc = " · ".join([b for b in (cat, it.get("uploadedByName"), it.get("executorMode")) if b])[:80]
    return {
        "source_key": "flow:skill:" + name,
        "source": "DramaFlow 后台",
        "source_url": DEFAULT_BASE + "/admin/#/agent-skills",
        "kind": "skill",
        "platform": "flow",
        "grp": grp,
        "cat": cat,
        "external_key": name,
        "external_version": version,
        "title": title,
        "summary": desc,
        "body": "",
        "usage_count": 0,
        "tags": " ".join([t for t in (it.get("stage") or "", it.get("assetType") or "",
                                     it.get("productionType") or "") if t]),
        "status": "hidden" if hidden else "active",
        "flow_workflow": (it.get("workflowId") or "").strip(),
        "flow_variant": (it.get("workflowVariant") or "").strip(),
    }


def sync_packages(conn, base, token, items, dry_run=False, limit=0):
    """把 Flow 后台每个 Skill 的 ZIP 包下下来，作为版本入库（有包之后详情页能看全文/下载）。"""
    admin = conn.execute("SELECT * FROM users WHERE role='admin' AND status='active' LIMIT 1").fetchone()
    if admin is None:
        print("  ! 库里没有可用的管理员账号，跳过包同步")
        return 0, 0
    admin = dict(admin)
    got = skipped = failed = 0
    for it in items:
        row = map_item(it)
        if not row:
            continue
        name = row["external_key"]
        ver = (it.get("packageVersion") or it.get("version") or "").strip() or "latest"
        res = conn.execute("SELECT id, current_version_id FROM resources WHERE source_key=?",
                           (row["source_key"],)).fetchone()
        if res is None:
            continue
        if res["current_version_id"]:
            have = conn.execute("SELECT note FROM versions WHERE id=?",
                                (res["current_version_id"],)).fetchone()
            if have is not None and (have["note"] or "").startswith("Flow v" + ver):
                skipped += 1
                continue
        if limit and got + failed >= limit:
            break
        if dry_run:
            print("  [dry] 下载包 %-30s v%s" % (name, ver))
            continue
        try:
            data = api_get_bytes(base, "/api/admin/agent-skills/%s/download" % urllib.parse.quote(name), token)
        except SyncError as e:
            print("  ! %s 下载失败：%s" % (name, e))
            failed += 1
            continue
        if not data or not data[:2] == b"PK":
            print("  · %s 没有可下载的包（后台返回空），跳过" % name)
            skipped += 1
            continue
        try:
            store.ingest_zip(conn, data, "%s-%s.zip" % (name, ver), admin,
                             platform="flow", grp=row["grp"], cat=row["cat"],
                             title=row["title"], note="Flow v" + ver,
                             external_key=name, tags=row["tags"], summary=row["summary"])
        except store.IngestError as e:
            print("  ! %s 入库失败：%s" % (name, e))
            failed += 1
            continue
        conn.commit()
        got += 1
        if got % 10 == 0:
            print("  ... 已入库 %d 个包" % got)
    return got, skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("FLOW_BASE", DEFAULT_BASE))
    ap.add_argument("--token", default="")
    ap.add_argument("--token-file", default=str(DATA / "flow_token.txt"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--probe", action="store_true", help="只打印第一条原始 JSON，用于核对字段")
    ap.add_argument("--with-packages", action="store_true", help="同时下载每个 Skill 的 ZIP 包并入库")
    ap.add_argument("--packages-only", action="store_true", help="只补包，不更新列表")
    ap.add_argument("--limit", type=int, default=0, help="本次最多下载几个包（调试用）")
    args = ap.parse_args()

    token = from_curl((args.token or "").strip())
    if not token:
        p = Path(args.token_file)
        if p.exists():
            token = from_curl(p.read_text(encoding="utf-8").strip())
    if not token:
        print("缺少 token。用 --token 或把 token 写进 data/flow_token.txt")
        return 2

    try:
        j = api_get(args.base, "/api/admin/agent-skills?all=1&includeHidden=1", token)
    except SyncError as e:
        print("失败：%s" % e)
        return 1

    items = unwrap(j)
    print("接口返回 %d 条 Skill" % len(items))
    if args.probe and items:
        print(json.dumps(items[0], ensure_ascii=False, indent=2)[:1500])
        return 0

    with store.db() as conn:
        store.migrate(conn)
        added = updated = skipped = 0
        for it in items:
            row = map_item(it)
            if not row:
                skipped += 1
                continue
            if args.dry_run:
                print("  [dry] %-9s %-28s %s / %s" % (row["status"], row["external_key"], row["grp"], row["cat"]))
                continue
            r = store.upsert_external(conn, row)
            if r == "added":
                added += 1
            else:
                updated += 1
        if not args.dry_run:
            conn.commit()
            print("完成：新增 %d，更新 %d，跳过 %d" % (added, updated, skipped))
        else:
            print("干跑结束（未写库）")

        if args.with_packages or args.packages_only:
            got, sk = sync_packages(conn, args.base, token, items,
                                    dry_run=args.dry_run, limit=args.limit)
            if not args.dry_run:
                conn.commit()
            print("包同步：入库 %d 个，跳过 %d 个" % (got, sk))
    return 0


if __name__ == "__main__":
    sys.exit(main())
