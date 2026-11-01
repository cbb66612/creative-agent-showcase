# -*- coding: utf-8 -*-
"""数据层：SQLite + 本地磁盘存储（零第三方依赖）。

设计原则（与 docs/00-落地方案.md 一致）：
* 元数据进库，文件进盘；库里只存相对路径，绝不存绝对路径；
* 上传原始 ZIP 只读保存，解压副本用于阅读；
* data/ 目录与代码同级，整个目录搬家即可换机器。
"""

import contextlib
import datetime
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import time
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
DB_PATH = DATA / "aikb.db"
STORAGE = DATA / "storage"

MAX_ZIP_BYTES = 200 * 1024 * 1024      # 单个 ZIP 上限 200MB
MAX_TOTAL_BYTES = 1024 * 1024 * 1024   # 解压后上限 1GB
MAX_FILES = 2000                       # 包内文件数上限

FLOW_WORKFLOWS = [
    ("", "通用默认"),
    ("premium", "国内精品剧（标准版）"),
    ("narration", "海外解说剧（标准版）"),
    ("hangzhou-yl", "国内精品剧（杭州YL）"),
    ("hangzhou-xw", "海外精品剧（通用素材）"),
    ("beijing-zs", "海外解说剧（北京JS）"),
    ("chengdu-zy", "国内速推（成都ZY）"),
    ("overseas-premium-solo", "海外精品剧（单人模式）"),
    ("chongqing-djy", "国内精品剧（重庆DJY）"),
]
WORKFLOW_LABEL = dict(FLOW_WORKFLOWS)


FLOW_STAGES = [
    ("剧本", ["剧本预审"]),
    ("分析手册", ["剧本手册"]),
    ("资产生成", ["角色", "场景", "道具"]),
    ("单集 Agent", ["成片主控"]),
]

TV_FUNCS = [
    ("文生文", ["通用", "文本与脚本", "角色资产", "场景资产", "分镜与镜头",
                "风格与氛围", "画质修复与校色", "通用生成优化"]),
    ("图生图", ["角色资产", "场景资产", "分镜与镜头", "风格与氛围", "安全与合规"]),
    ("多模态生视频", ["角色资产"]),
]


class IngestError(Exception):
    pass


# --------------------------------------------------------------------------
# 基础
# --------------------------------------------------------------------------

def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_dirs():
    for p in (DATA, STORAGE, STORAGE / "skills", STORAGE / "resources"):
        p.mkdir(parents=True, exist_ok=True)


@contextlib.contextmanager
def db():
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # 本机 SQLite 建临时文件会报「unable to open database file」（排序溢出），
    # 数据量不大，直接让临时表/排序结果放内存。
    conn.execute("PRAGMA temp_store=MEMORY")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'member',
  can_maintain INTEGER NOT NULL DEFAULT 0,
  pw_salt TEXT NOT NULL,
  pw_hash TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions(
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS resources(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  platform TEXT NOT NULL,
  grp TEXT NOT NULL DEFAULT '',
  cat TEXT NOT NULL DEFAULT '',
  external_key TEXT DEFAULT '',
  source TEXT DEFAULT '',
  source_url TEXT DEFAULT '',
  source_key TEXT DEFAULT '',
  cover_url TEXT DEFAULT '',
  title TEXT NOT NULL,
  summary TEXT DEFAULT '',
  body TEXT DEFAULT '',
  tags TEXT DEFAULT '',
  usage_count INTEGER DEFAULT 0,
  hue INTEGER DEFAULT 215,
  emoji TEXT DEFAULT '📄',
  status TEXT DEFAULT 'active',
  on_shelf INTEGER DEFAULT 0,
  owner_id INTEGER,
  current_version_id INTEGER,
  created_at TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS versions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  resource_id INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  zip_name TEXT DEFAULT '',
  zip_path TEXT DEFAULT '',
  bytes INTEGER DEFAULT 0,
  file_count INTEGER DEFAULT 0,
  note TEXT DEFAULT '',
  created_by INTEGER,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  version_id INTEGER NOT NULL,
  path TEXT NOT NULL,
  size INTEGER DEFAULT 0,
  is_md INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  action TEXT NOT NULL,
  resource_id INTEGER,
  detail TEXT DEFAULT '',
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_files_version ON files(version_id);
CREATE INDEX IF NOT EXISTS idx_versions_res ON versions(resource_id);
CREATE INDEX IF NOT EXISTS idx_res_plat ON resources(platform, grp);
CREATE INDEX IF NOT EXISTS idx_res_kind ON resources(kind);
CREATE TABLE IF NOT EXISTS agent_sessions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  title TEXT DEFAULT '',
  created_at TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS agent_messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL,
  role TEXT NOT NULL,
  content TEXT DEFAULT '',
  steps TEXT DEFAULT '',
  sources TEXT DEFAULT '',
  mode TEXT DEFAULT '',
  elapsed_ms INTEGER DEFAULT 0,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_amsg_session ON agent_messages(session_id);
"""


def migrate(conn):
    conn.executescript(SCHEMA)
    # 老库补列（新增外部导入所需的来源字段）
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(resources)")}
    for name, ddl in (("source", "TEXT DEFAULT ''"), ("source_url", "TEXT DEFAULT ''"),
                      ("source_key", "TEXT DEFAULT ''"), ("cover_url", "TEXT DEFAULT ''"),
                      ("external_version", "TEXT DEFAULT ''"), ("on_shelf", "INTEGER DEFAULT 0"),
                      ("flow_workflow", "TEXT DEFAULT ''"), ("flow_variant", "TEXT DEFAULT ''")):
        if name not in cols:
            conn.execute("ALTER TABLE resources ADD COLUMN %s %s" % (name, ddl))


# --------------------------------------------------------------------------
# 账号
# --------------------------------------------------------------------------

def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 120000)
    return salt, dk.hex()


def check_password(row, password):
    salt, expect = row["pw_salt"], row["pw_hash"]
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 120000)
    return secrets.compare_digest(dk.hex(), expect)


def create_user(conn, username, name, password, role="member", can_maintain=0):
    salt, pw = hash_password(password)
    cur = conn.execute(
        "INSERT INTO users(username,name,role,can_maintain,pw_salt,pw_hash,status,created_at)"
        " VALUES(?,?,?,?,?,?, 'active', ?)",
        (username, name, role, can_maintain, salt, pw, now()))
    return cur.lastrowid


def set_password(conn, user_id, password):
    salt, pw = hash_password(password)
    conn.execute("UPDATE users SET pw_salt=?, pw_hash=? WHERE id=?", (salt, pw, user_id))


def get_user(conn, user_id):
    return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def get_user_by_name(conn, username):
    return conn.execute("SELECT * FROM users WHERE username=? AND status='active'", (username,)).fetchone()


def list_users(conn):
    return conn.execute("SELECT * FROM users ORDER BY role DESC, id").fetchall()


def new_session(conn, user_id, days=7):
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO sessions(token,user_id,expires_at) VALUES(?,?,?)",
                 (token, user_id, int(time.time()) + days * 86400))
    return token


def session_user(conn, token):
    if not token:
        return None
    row = conn.execute("SELECT * FROM sessions WHERE token=?", (token,)).fetchone()
    if not row:
        return None
    if row["expires_at"] < time.time():
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        return None
    return conn.execute("SELECT * FROM users WHERE id=? AND status='active'", (row["user_id"],)).fetchone()


def drop_session(conn, token):
    conn.execute("DELETE FROM sessions WHERE token=?", (token,))


# --------------------------------------------------------------------------
# 查询
# --------------------------------------------------------------------------

def can_maintain(user):
    return bool(user and (user["role"] == "admin" or user["can_maintain"]))


def is_admin(user):
    return bool(user and user["role"] == "admin")


def _res_filters(platform="", grp="", cat="", q="", kind="", status="active", wf=None):
    sql = " WHERE 1=1"
    args = []
    if status:
        sql += " AND status=?"
        args.append(status)
    if platform and platform != "all":
        sql += " AND platform=?"
        args.append(platform)
    if grp:
        sql += " AND grp=?"
        args.append(grp)
    if cat:
        sql += " AND cat=?"
        args.append(cat)
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    if wf is not None and wf != "":
        if wf == "default":
            sql += " AND (flow_workflow IS NULL OR flow_workflow='')"
        else:
            sql += " AND flow_workflow=?"
            args.append(wf)
    if q:
        like = "%" + q + "%"
        sql += " AND (title LIKE ? OR summary LIKE ? OR external_key LIKE ? OR body LIKE ?)"
        args += [like, like, like, like]
    return sql, args


def list_resources(conn, platform="", grp="", cat="", q="", kind="", status="active",
                   limit=500, offset=0, order="updated", wf=None):
    where, args = _res_filters(platform, grp, cat, q, kind, status, wf)
    if order == "id":
        order_sql = "id ASC"
    elif order == "cover":
        order_sql = "(cover_url != '') DESC, id ASC"
    else:
        order_sql = "updated_at DESC, id DESC"
    sql = "SELECT * FROM resources" + where + " ORDER BY " + order_sql + " LIMIT ? OFFSET ?"
    args += [limit, offset]
    return conn.execute(sql, args).fetchall()


def count_resources(conn, platform="", grp="", cat="", q="", kind="", status="active", wf=None):
    where, args = _res_filters(platform, grp, cat, q, kind, status, wf)
    return conn.execute("SELECT COUNT(*) c FROM resources" + where, args).fetchone()["c"]


def groups_of(conn, platform):
    """某个平台下实际存在的分组及数量（外部导入库用）"""
    return conn.execute(
        "SELECT grp, COUNT(*) c FROM resources WHERE status='active' AND platform=?"
        " GROUP BY grp ORDER BY c DESC", (platform,)).fetchall()



def get_resource(conn, rid):
    return conn.execute("SELECT * FROM resources WHERE id=?", (rid,)).fetchone()


def get_versions(conn, rid):
    return conn.execute("SELECT * FROM versions WHERE resource_id=? ORDER BY seq DESC", (rid,)).fetchall()


def get_version(conn, vid):
    return conn.execute("SELECT * FROM versions WHERE id=?", (vid,)).fetchone()


def get_files(conn, vid):
    return conn.execute("SELECT * FROM files WHERE version_id=? ORDER BY path", (vid,)).fetchall()


def count_by(conn):
    out = {}
    out["resources"] = conn.execute("SELECT COUNT(*) c FROM resources WHERE status='active'").fetchone()["c"]
    out["skills"] = conn.execute("SELECT COUNT(*) c FROM resources WHERE status='active' AND kind='skill'"
                                 " AND platform='flow'").fetchone()["c"]
    out["presets"] = conn.execute("SELECT COUNT(*) c FROM resources WHERE status='active' AND kind='preset'"
                                  " AND platform='tv'").fetchone()["c"]
    out["ext"] = conn.execute("SELECT COUNT(*) c FROM resources WHERE status='active'"
                              " AND platform='ext'").fetchone()["c"]
    out["users"] = conn.execute("SELECT COUNT(*) c FROM users WHERE status='active'").fetchone()["c"]
    out["downloads"] = conn.execute("SELECT COUNT(*) c FROM events WHERE action='download'").fetchone()["c"]
    out["uploads"] = conn.execute("SELECT COUNT(*) c FROM events WHERE action='upload'").fetchone()["c"]
    out["views"] = conn.execute("SELECT COUNT(*) c FROM events WHERE action='view'").fetchone()["c"]
    return out


def top_used(conn, limit=8):
    return conn.execute(
        "SELECT * FROM resources WHERE status='active' AND kind='preset'"
        " ORDER BY usage_count DESC LIMIT ?", (limit,)).fetchall()


def recent_events(conn, limit=20):
    return conn.execute(
        "SELECT e.*, u.name AS uname, r.title AS rtitle FROM events e"
        " LEFT JOIN users u ON u.id=e.user_id LEFT JOIN resources r ON r.id=e.resource_id"
        " ORDER BY e.id DESC LIMIT ?", (limit,)).fetchall()


def log(conn, user_id, action, resource_id=None, detail=""):
    conn.execute("INSERT INTO events(user_id,action,resource_id,detail,created_at) VALUES(?,?,?,?,?)",
                 (user_id, action, resource_id, detail, now()))


# --------------------------------------------------------------------------
# 上传入库
# --------------------------------------------------------------------------

def _fix_name(info):
    name = info.filename
    if not name:
        return ""
    if info.flag_bits & 0x800:
        return name
    try:
        raw = name.encode("cp437")
    except Exception:
        return name
    for enc in ("utf-8", "gbk", "big5"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return name


def _clean_rel(name):
    name = name.replace("\\", "/")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise IngestError("ZIP 内出现绝对路径：%s" % name)
    parts = []
    for p in name.split("/"):
        if p in ("", "."):
            continue
        if p == "..":
            raise IngestError("ZIP 内出现路径穿越：%s" % name)
        parts.append(p)
    return "/".join(parts)


def _parse_frontmatter(text):
    meta = {}
    if not text.startswith("---"):
        return meta, text
    end = text.find("\n---", 3)
    if end < 0:
        return meta, text
    block = text[3:end]
    body = text[end + 4:]
    for line in block.replace("\r", "").split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip().lower()] = v.strip()
    return meta, body.lstrip("\n")


def _first_sentence(text, limit=46):
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return ""
    cut = re.split(r"[。！？!?\n]", text)[0]
    return cut[:limit]


def ingest_zip(conn, zip_bytes, filename, user, platform="flow", grp="",
               cat="", title="", note="", external_key="", tags=None, summary=None):
    """把一个 ZIP 解析入库，返回 (resource_id, seq, summary, info)。"""
    if len(zip_bytes) > MAX_ZIP_BYTES:
        raise IngestError("ZIP 超过 %d MB 上限" % (MAX_ZIP_BYTES // 1024 // 1024))
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        raise IngestError("这不是一个有效的 ZIP 文件")

    infos = []
    total = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = _fix_name(info)
        rel = _clean_rel(name)
        if not rel:
            continue
        mode = (info.external_attr >> 16) & 0o170000
        if mode == 0o120000:
            raise IngestError("ZIP 内含符号链接，已拒绝：%s" % rel)
        total += info.file_size
        if total > MAX_TOTAL_BYTES:
            raise IngestError("解压后超过 1GB 上限")
        infos.append((rel, info))

    if not infos:
        raise IngestError("ZIP 里没有文件")
    if len(infos) > MAX_FILES:
        raise IngestError("文件数超过 %d 上限" % MAX_FILES)

    names = [n for n, _ in infos]
    roots = {n.split("/")[0] for n in names if "/" in n}
    has_top_md = any("/" not in n and n.lower() in ("skill.md", "readme.md") for n in names)
    prefix = ""
    if not has_top_md and len(roots) == 1:
        prefix = list(roots)[0] + "/"

    rels = []
    for n, info in infos:
        rel = n[len(prefix):] if prefix and n.startswith(prefix) else n
        if not rel:
            continue
        rels.append((rel, info))

    contents = {}
    for rel, info in rels:
        if rel.lower().endswith((".md", ".txt", ".json", ".yaml", ".yml")):
            try:
                contents[rel] = zf.read(info).decode("utf-8", "replace")
            except Exception:
                contents[rel] = ""

    main = None
    for cand in ("SKILL.md", "skill.md", "README.md", "readme.md"):
        if cand in contents:
            main = cand
            break
    if main is None:
        for rel in contents:
            main = rel
            break

    fm = {}
    main_body = ""
    if main:
        fm, main_body = _parse_frontmatter(contents.get(main, ""))

    key = (external_key or fm.get("name") or "").strip()
    desc = (fm.get("description") or "").strip()
    short = (summary or "").strip() or _first_sentence(desc) or _first_sentence(main_body)
    title = (title or "").strip() or short or Path(filename).stem
    if len(title) > 24:
        title = title[:24] + "…"

    existing = None
    if key:
        existing = conn.execute(
            "SELECT * FROM resources WHERE kind='skill' AND external_key=? AND status!='deleted'",
            (key,)).fetchone()

    updated = now()
    if existing is not None:
        if not can_maintain(user):
            raise IngestError("「%s」已存在（%s），只有可维护成员才能发布新版本" % (title, existing["title"]))
        rid = existing["id"]
        seq = (conn.execute("SELECT MAX(seq) c FROM versions WHERE resource_id=?", (rid,)).fetchone()["c"] or 0) + 1
        version_dir = STORAGE / "skills" / str(rid) / "versions" / ("v%04d" % seq)
    else:
        cur = conn.execute(
            "INSERT INTO resources(kind,platform,grp,cat,external_key,title,summary,tags,"
            "usage_count,hue,emoji,status,on_shelf,owner_id,created_at,updated_at,body)"
            " VALUES('skill',?,?,?,?,?,?,?,0,215,'📦','active',0,?,?,?,'')",
            (platform, grp, cat or "", key, title, short, (tags or "DSH"), user["id"], updated, updated))
        rid = cur.lastrowid
        seq = 1
        version_dir = STORAGE / "skills" / str(rid) / "versions" / ("v%04d" % seq)

    extracted = version_dir / "extracted"
    version_dir.mkdir(parents=True, exist_ok=True)
    extracted.mkdir(parents=True, exist_ok=True)

    for rel, info in rels:
        target = extracted / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 256)

    zip_name = Path(filename).name
    zip_rel = os.path.relpath(str(version_dir / "original.zip"), str(DATA)).replace("\\", "/")
    (version_dir / "original.zip").write_bytes(zip_bytes)

    cur = conn.execute(
        "INSERT INTO versions(resource_id,seq,zip_name,zip_path,bytes,file_count,note,created_by,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (rid, seq, zip_name, zip_rel, len(zip_bytes), len(rels), note or ("首次上传" if seq == 1 else "新版本"),
         user["id"], updated))
    vid = cur.lastrowid

    conn.executemany(
        "INSERT INTO files(version_id,path,size,is_md) VALUES(?,?,?,?)",
        [(vid, rel, info.file_size, 1 if rel.lower().endswith(".md") else 0) for rel, info in rels])

    if existing is not None:
        conn.execute(
            "UPDATE resources SET current_version_id=?, updated_at=?, title=COALESCE(NULLIF(title,''),?),"
            " summary=COALESCE(NULLIF(?,''), summary) WHERE id=?",
            (vid, updated, title, short, rid))
    else:
        conn.execute("UPDATE resources SET current_version_id=? WHERE id=?", (vid, rid))

    log(conn, user["id"], "upload", rid, "%s · v%d · %d 个文件" % (zip_name, seq, len(rels)))

    info = {
        "main": main or "",
        "files": [r for r, _ in rels],
        "frontmatter": fm,
        "prefix": prefix,
        "is_new": existing is None,
    }
    return rid, seq, short, info


def upsert_external(conn, it):
    """按 source_key 同步一条外部资源（TV 后台 / Flow 后台这类）。

    与 import_items 的区别：更新时会覆盖标题、分类、用量、状态，适合反复同步。
    返回 "added" / "updated"。
    """
    key = (it.get("source_key") or "").strip()
    if not key:
        raise ValueError("upsert_external 需要 source_key")
    ts = now()
    row = conn.execute("SELECT id, status FROM resources WHERE source_key=?", (key,)).fetchone()
    if row:
        conn.execute(
            "UPDATE resources SET kind=?, platform=?, grp=?, cat=?, external_key=?, title=?,"
            " summary=?, body=?, usage_count=?, cover_url=?, tags=?, source=?, source_url=?,"
            " hue=?, emoji=?, status=?, external_version=?, flow_workflow=?, flow_variant=?,"
            " updated_at=? WHERE id=?",
            (it.get("kind") or "preset", it.get("platform") or "tv", it.get("grp") or "",
             it.get("cat") or "", it.get("external_key") or "", (it.get("title") or "")[:200],
             it.get("summary") or "", it.get("body") or "", int(it.get("usage_count") or 0),
             it.get("cover_url") or "", it.get("tags") or "", it.get("source") or "",
             it.get("source_url") or "", it.get("hue") or 215, it.get("emoji") or "📄",
             it.get("status") or "active", str(it.get("external_version") or ""),
             it.get("flow_workflow") or "", it.get("flow_variant") or "", ts, row["id"]))
        return "updated"
    conn.execute(
        "INSERT INTO resources(kind,platform,grp,cat,external_key,source,source_url,source_key,"
        "cover_url,title,summary,body,tags,usage_count,hue,emoji,status,on_shelf,owner_id,"
        "current_version_id,flow_workflow,flow_variant,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,NULL,?,?,?,?)",
        (it.get("kind") or "preset", it.get("platform") or "tv", it.get("grp") or "",
         it.get("cat") or "", it.get("external_key") or "", it.get("source") or "",
         it.get("source_url") or "", key, it.get("cover_url") or "",
         (it.get("title") or "")[:200], it.get("summary") or "", it.get("body") or "",
         it.get("tags") or "", int(it.get("usage_count") or 0), it.get("hue") or 215,
         it.get("emoji") or "📄", it.get("status") or "active",
         it.get("flow_workflow") or "", it.get("flow_variant") or "", ts, ts))
    return "added"


def import_items(conn, items):
    """批量导入外部条目（如镜库）。按 source_key 去重，返回 (新增, 更新)。

    item 字段：kind/platform/grp/cat/title/summary/body/tags(list)/source/source_url/
    source_key/cover_url/hue/emoji/created_at
    """
    added = updated = 0
    for it in items:
        key = (it.get("source_key") or "").strip()
        tags = it.get("tags") or []
        if isinstance(tags, (list, tuple)):
            tags = " ".join(t for t in tags if t)
        row = None
        if key:
            row = conn.execute("SELECT id FROM resources WHERE source_key=?", (key,)).fetchone()
        ts = it.get("created_at") or now()
        if row:
            conn.execute(
                "UPDATE resources SET summary=?, body=?, tags=?, source=?, source_url=?,"
                " cover_url=?, hue=?, emoji=?, updated_at=? WHERE id=?",
                (it.get("summary") or "", it.get("body") or "", tags, it.get("source") or "",
                 it.get("source_url") or "", it.get("cover_url") or "", it.get("hue") or 215,
                 it.get("emoji") or "📄", ts, row["id"]))
            updated += 1
        else:
            conn.execute(
                "INSERT INTO resources(kind,platform,grp,cat,external_key,source,source_url,"
                "source_key,cover_url,title,summary,body,tags,usage_count,hue,emoji,status,"
                "on_shelf,owner_id,current_version_id,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',0,NULL,NULL,?,?)",
                (it.get("kind") or "preset", it.get("platform") or "ext", it.get("grp") or "",
                 it.get("cat") or "", it.get("external_key") or "", it.get("source") or "",
                 it.get("source_url") or "", key, it.get("cover_url") or "",
                 (it.get("title") or "")[:200], it.get("summary") or "",
                 it.get("body") or "", tags, int(it.get("usage_count") or 0),
                 it.get("hue") or 215, it.get("emoji") or "📄", ts, ts))
            added += 1
    return added, updated


def resource_dir(rid):
    return STORAGE / "skills" / str(rid)


def read_version_file(version, rel):
    base = (DATA / Path(version["zip_path"]).parent / "extracted").resolve()
    target = (base / rel).resolve()
    if not str(target).startswith(str(base)):
        return None
    if not target.exists():
        return None
    return target.read_text(encoding="utf-8", errors="replace")


def zip_abs_path(version):
    p = DATA / Path(version["zip_path"])
    return p if p.exists() else None


# --------------------------------------------------------------------------
# 初始化
# --------------------------------------------------------------------------

def seed_taxonomy_text():
    return {"flow": FLOW_STAGES, "tv": TV_FUNCS}


def init_storage(verbose=True):
    ensure_dirs()
    with db() as conn:
        migrate(conn)
        n = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        if n == 0:
            pw = secrets.token_urlsafe(18)
            create_user(conn, "demo_admin", "管理员", pw, role="admin", can_maintain=1)
            lines = [
                "灵感引擎 · Skill 预设库 —— 初始账号",
                "",
                "管理员账号：demo_admin",
                "初始密码：%s" % pw,
                "",
                "登录后请到「账号管理」修改密码、并为组里其他 5 位同事创建账号。",
                "这份文件只在首次初始化时生成，删除前请先抄下密码。",
            ]
            (DATA / "初始账号.txt").write_text("\n".join(lines), encoding="utf-8")
            if verbose:
                print("[init] 已创建管理员账号 demo_admin（密码见 data/初始账号.txt）")
        if not conn.execute("SELECT COUNT(*) c FROM resources").fetchone()["c"]:
            seed_presets(conn, verbose)
            seed_skills(conn, verbose)


def seed_presets(conn, verbose=True):
    datajs = BASE.parent / "prototype" / "assets" / "data.js"
    if not datajs.exists():
        return
    text = datajs.read_text(encoding="utf-8", errors="replace")
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return
    try:
        data = json.loads(text[start:end + 1])
    except Exception as exc:  # noqa: BLE001
        print("[init] 读取预设种子失败：%s" % exc)
        return
    rows = data.get("resources") or []
    count = 0
    for r in rows:
        if r.get("kind") != "preset":
            continue
        cats = r.get("categories") or []
        conn.execute(
            "INSERT INTO resources(kind,platform,grp,cat,external_key,title,summary,body,tags,"
            "usage_count,hue,emoji,status,on_shelf,owner_id,created_at,updated_at)"
            " VALUES('preset','tv',?,?,?,?,?,?,?,?,?,?,'active',1,1,?,?)",
            (r.get("func") or "文生文", cats[0] if cats else "",
             r.get("id") or "", r.get("title") or "", r.get("summary") or "",
             r.get("body") or "", "官方", int(r.get("usageCount") or 0),
             int(r.get("hue") or 215), r.get("emoji") or "📄", now(), now()))
        count += 1
    if verbose and count:
        print("[init] 已导入 %d 条预设" % count)


def seed_skills(conn, verbose=True):
    src = Path("D:/skill/资产（美化）")
    plan = [
        ("asset-generation-optimized-20261028.zip", "资产生成", "道具"),
        ("changjing-20261011124156987-98e4ed39 (1).zip", "资产生成", "场景"),
        ("jiaose-20261022140344561-cd5e9f07.zip", "资产生成", "角色"),
    ]
    if not src.exists():
        return
    admin = conn.execute("SELECT * FROM users ORDER BY id LIMIT 1").fetchone()
    if not admin:
        return
    for name, grp, cat in plan:
        p = src / name
        if not p.exists():
            continue
        try:
            rid, seq, short, _ = ingest_zip(conn, p.read_bytes(), name, admin,
                                            platform="flow", grp=grp, cat=cat)
            if verbose:
                print("[init] 已导入 Skill：%s（id=%d v%d）" % (short or name, rid, seq))
        except IngestError as exc:
            print("[init] 跳过 %s：%s" % (name, exc))


if __name__ == "__main__":
    init_storage()
    print("数据目录：", DATA)


# --------------------------------------------------------------------------
# 导入前先"读一遍"：暂存 ZIP + 只读分析（不入库）
# --------------------------------------------------------------------------
STAGING = DATA / "staging"


def stage_zip(zip_bytes):
    STAGING.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(16)
    (STAGING / (token + ".zip")).write_bytes(zip_bytes)
    return token


def staged_zip(token):
    token = (token or "").strip()
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        return None
    path = STAGING / (token + ".zip")
    try:
        return path.read_bytes()
    except Exception:
        return None


def drop_staged(token):
    token = (token or "").strip()
    if re.fullmatch(r"[0-9a-f]{32}", token):
        for suffix in (".zip", ".name"):
            try:
                (STAGING / (token + suffix)).unlink()
            except Exception:
                pass


def sweep_staging(hours=24):
    if not STAGING.exists():
        return
    cutoff = time.time() - hours * 3600
    for p in STAGING.glob("*.zip"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except Exception:
            pass


def inspect_zip(zip_bytes):
    # 只读分析，不写库、不落盘：给上传确认页用
    if len(zip_bytes) > MAX_ZIP_BYTES:
        raise IngestError("ZIP 超过 %d MB 上限" % (MAX_ZIP_BYTES // 1024 // 1024))
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        raise IngestError("这不是一个有效的 ZIP 文件")

    infos, total = [], 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        rel = _clean_rel(_fix_name(info))
        if not rel:
            continue
        mode = (info.external_attr >> 16) & 0o170000
        if mode == 0o120000:
            raise IngestError("ZIP 内含符号链接，已拒绝：%s" % rel)
        total += info.file_size
        if total > MAX_TOTAL_BYTES:
            raise IngestError("解压后超过 1GB 上限")
        infos.append((rel, info))
    if not infos:
        raise IngestError("ZIP 里没有文件")
    if len(infos) > MAX_FILES:
        raise IngestError("文件数超过 %d 上限" % MAX_FILES)

    names = [n for n, _ in infos]
    roots = {n.split("/")[0] for n in names if "/" in n}
    has_top = any("/" not in n and n.lower() in ("skill.md", "readme.md") for n in names)
    prefix = ""
    if not has_top and len(roots) == 1:
        prefix = list(roots)[0] + "/"
    rels = []
    for n, info in infos:
        rel = n[len(prefix):] if prefix and n.startswith(prefix) else n
        if rel:
            rels.append((rel, info))

    texts = {}
    for rel, info in rels:
        if rel.lower().endswith((".md", ".txt", ".json", ".yaml", ".yml")) and info.file_size <= 2 * 1024 * 1024:
            try:
                texts[rel] = zf.read(info).decode("utf-8", "replace")
            except Exception:
                texts[rel] = ""

    main = None
    for cand in ("SKILL.md", "skill.md", "README.md", "readme.md"):
        if cand in texts:
            main = cand
            break
    if main is None and texts:
        main = sorted(texts)[0]

    fm, body = _parse_frontmatter(texts.get(main, "")) if main else ({}, "")
    heads = []
    for line in body.split("\n"):
        s = line.strip()
        if s.startswith("## "):
            heads.append(s[3:].strip())
        elif s.startswith("# "):
            heads.insert(0, s[2:].strip())
        if len(heads) >= 10:
            break

    flat = re.sub(r"[#>*`|_~\-]+", " ", body)
    flat = re.sub(r"\s+", " ", flat).strip()

    return {
        "files": [r for r, _ in rels],
        "file_count": len(rels),
        "total_bytes": total,
        "main": main or "",
        "frontmatter": fm,
        "headings": heads,
        "excerpt": flat[:2600],
        "prefix": prefix,
        "folders": sorted({r.split("/")[0] for r in [x for x, _ in rels] if "/" in r}),
    }


def note_staged_name(token, filename):
    try:
        (STAGING / (token + ".name")).write_text(filename or "", encoding="utf-8")
    except Exception:
        pass


def staged_name(token):
    try:
        return (STAGING / (token + ".name")).read_text(encoding="utf-8").strip()
    except Exception:
        return ""


# ---------------------------------------------------------------- 内置 Agent 会话
def agent_session(conn, user_id, sid=None, title=""):
    """取会话；sid 不存在或不属于本人时新建。"""
    if sid:
        row = conn.execute("SELECT * FROM agent_sessions WHERE id=? AND user_id=?",
                           (sid, user_id)).fetchone()
        if row:
            return row
    cur = conn.execute(
        "INSERT INTO agent_sessions(user_id, title, created_at, updated_at) VALUES(?,?,?,?)",
        (user_id, title[:60], now(), now()))
    conn.commit()
    return conn.execute("SELECT * FROM agent_sessions WHERE id=?", (cur.lastrowid,)).fetchone()


def agent_add_message(conn, session_id, role, content, steps=None, sources=None,
                      mode="", elapsed_ms=0):
    conn.execute(
        "INSERT INTO agent_messages(session_id, role, content, steps, sources, mode, elapsed_ms, created_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (session_id, role, content or "",
         json.dumps(steps or [], ensure_ascii=False) if steps is not None else "",
         json.dumps(sources or [], ensure_ascii=False) if sources is not None else "",
         mode or "", int(elapsed_ms or 0), now()))
    conn.execute("UPDATE agent_sessions SET updated_at=? WHERE id=?", (now(), session_id))
    conn.commit()


def agent_history(conn, session_id, limit=12):
    rows = conn.execute(
        "SELECT * FROM agent_messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
        (session_id, limit)).fetchall()
    return list(reversed(rows))


def agent_sessions(conn, user_id, limit=20):
    return conn.execute(
        "SELECT * FROM agent_sessions WHERE user_id=? ORDER BY updated_at DESC LIMIT ?",
        (user_id, limit)).fetchall()

def ensure_insights(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS skill_insights("
        " resource_id INTEGER NOT NULL,"
        " version_id INTEGER NOT NULL PRIMARY KEY,"
        " json TEXT NOT NULL,"
        " created_at TEXT NOT NULL)"
    )
    conn.commit()


def get_insight(conn, version_id, resource_id=None):
    import json as _json
    ensure_insights(conn)
    row = conn.execute("SELECT json FROM skill_insights WHERE version_id=?", (version_id,)).fetchone()
    if not row and resource_id:
        row = conn.execute(
            "SELECT json FROM skill_insights WHERE resource_id=? ORDER BY created_at DESC LIMIT 1",
            (resource_id,)).fetchone()
    if not row:
        return None
    try:
        return _json.loads(row["json"])
    except Exception:
        return None
