# -*- coding: utf-8 -*-
"""全站 AI 助手（PRD 的 C02）。

两个模式：
- 检索模式（默认，不需要 Key）：在库里找相关资源，把标题/摘要/片段列出来；
- 回答模式（配了 DeepSeek Key）：把检索到的片段当资料发给模型，要求只依据资料回答并标注 [n] 来源。

Key 存在 lan/data/config.json，永远不出现在前端页面里。
"""
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402

CONFIG_PATH = store.DATA / "config.json"
DEFAULT_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
MAX_CONTEXT = 8          # 最多给模型 8 条资料
EXCERPT = 700            # 每条资料截多少字


def load_config():
    cfg = {"base_url": DEFAULT_BASE, "model": DEFAULT_MODEL, "api_key": ""}
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            for k in cfg:
                if raw.get(k):
                    cfg[k] = str(raw[k]).strip()
        except Exception:  # noqa: BLE001
            pass
    cfg["base_url"] = os.environ.get("AI_BASE_URL", cfg["base_url"])
    cfg["model"] = os.environ.get("AI_MODEL", cfg["model"])
    cfg["api_key"] = os.environ.get("AI_API_KEY", cfg["api_key"])
    return cfg


def save_config(base_url, model, api_key, clear=False):
    cfg = load_config()
    cfg["base_url"] = (base_url or DEFAULT_BASE).strip().rstrip("/")
    cfg["model"] = (model or DEFAULT_MODEL).strip()
    if clear:
        cfg["api_key"] = ""
    elif api_key:
        cfg["api_key"] = api_key.strip()
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def configured():
    return bool(load_config().get("api_key"))


# ---------------------------------------------------------------- 检索
PUNCT = re.compile(r"[\s,，。、;；:：!！?？\"'“”‘’()（）\[\]【】<>《》/|+*#`~-]+")


def _tokens(q):
    """把问题切成检索词：整句 + 空格/标点切开的片段 + 中文 2 字滑窗。"""
    q = (q or "").strip()
    out = []
    if q:
        out.append(q)
    for part in PUNCT.split(q):
        if len(part) >= 2:
            out.append(part)
    cjk = "".join(ch for ch in q if "\u4e00" <= ch <= "\u9fff")
    for i in range(len(cjk) - 1):
        out.append(cjk[i:i + 2])
    seen, uniq = set(), []
    for t in out:
        if t and t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq[:12]


def _plain(text, n):
    t = re.sub(r"[#>*`|_~\-]+", " ", text or "")
    t = re.sub(r"\s+", " ", t).strip()
    return t[:n]


def search(conn, q, limit=MAX_CONTEXT, platform=""):
    toks = _tokens(q)
    if not toks:
        return []
    where = " OR ".join(["title LIKE ?", "summary LIKE ?", "body LIKE ?", "tags LIKE ?", "grp LIKE ?"] * len(toks))
    args = []
    for t in toks:
        like = "%" + t + "%"
        args.extend([like] * 5)
    extra = " AND platform=?" if platform else ""
    sql = ("SELECT id, kind, platform, grp, cat, title, summary, body, tags FROM resources "
           "WHERE status='active'" + extra + " AND (" + where + ") LIMIT 400")
    if platform:
        args = [platform] + args
    rows = conn.execute(sql, args).fetchall()

    scored = []
    for r in rows:
        title = r["title"] or ""
        summ = r["summary"] or ""
        body = r["body"] or ""
        tags = r["tags"] or ""
        grp = r["grp"] or ""
        score = 0
        for t in toks:
            if t in title:
                score += 8
            if t in grp or t in tags:
                score += 4
            if t in summ:
                score += 3
            if t in body:
                score += 1
        # 精确整句命中给高分
        if q and q in title:
            score += 10
        if q and q in body:
            score += 2
        if score:
            scored.append((score, r))
    scored.sort(key=lambda x: (-x[0], x[1]["id"]))
    return [r for _, r in scored[:limit]]


def _source_item(r, idx, q):
    body = _plain(r["body"], 4000)
    snippet = ""
    toks = _tokens(q)
    for t in toks:
        pos = body.find(t)
        if pos >= 0:
            snippet = body[max(0, pos - 40):pos + 120]
            break
    if not snippet:
        snippet = body[:140]
    return {
        "n": idx,
        "id": r["id"],
        "title": r["title"],
        "kind": r["kind"],
        "platform": r["platform"],
        "grp": r["grp"],
        "cat": r["cat"],
        "summary": r["summary"] or "",
        "snippet": snippet,
        "url": "/r/%d" % r["id"],
    }


SYSTEM = (
    "你是「创作资源工作台 · Skill 预设库」的站内助手。你只能依据下面给的【资料】回答，"
    "不得使用资料以外的知识，不得编造资源、版本或平台状态。"
    "回答用中文，先给结论，再给要点；引用资料时在句末标注 [编号]。"
    "如果资料不足以回答，就直接说「库里没有找到直接相关的内容」，并说明最接近的是什么。"
    "不要输出资料以外的东西，不要提你是模型。"
)


def _build_prompt(q, sources):
    parts = []
    for s in sources:
        label = {"flow": "Flow Skill", "tv": "TV 预设", "ext": "镜库外部资源"}.get(s["platform"], s["platform"])
        parts.append("[%d] %s（%s · %s）\n摘要：%s\n正文摘录：%s"
                     % (s["n"], s["title"], label, s["grp"], s["summary"], s["snippet"]))
    return "【资料】\n" + "\n\n".join(parts) + "\n\n【问题】" + q


def _call_deepseek(cfg, prompt):
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    payload = json.dumps({
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 900,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + cfg["api_key"],
    })
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()


def ask(conn, question):
    question = (question or "").strip()
    if not question:
        return {"ok": False, "error": "问题不能为空"}
    rows = search(conn, question, MAX_CONTEXT)
    sources = [_source_item(r, i + 1, question) for i, r in enumerate(rows)]
    cfg = load_config()

    if not sources:
        return {"ok": True, "mode": "retrieval", "answer":
                "库里没有找到和「%s」相关的内容。可以换个说法，或者用资源库的搜索试试。" % question,
                "sources": []}

    if not cfg.get("api_key"):
        lines = ["当前没配 DeepSeek Key，我先按检索结果给你（配 Key 后可以直接问、由模型归纳并引用）：", ""]
        for s in sources[:5]:
            lines.append("[%d]《%s》（%s）— %s" % (s["n"], s["title"], s["grp"], s["summary"][:60]))
        return {"ok": True, "mode": "retrieval", "answer": "\n".join(lines), "sources": sources}

    try:
        answer = _call_deepseek(cfg, _build_prompt(question, sources))
        return {"ok": True, "mode": "llm", "answer": answer, "sources": sources}
    except Exception as exc:  # noqa: BLE001
        lines = ["调用模型失败（%s）。这是检索到的相关内容：" % str(exc)[:120], ""]
        for s in sources[:5]:
            lines.append("[%d]《%s》（%s）— %s" % (s["n"], s["title"], s["grp"], s["summary"][:60]))
        return {"ok": True, "mode": "fallback", "answer": "\n".join(lines), "sources": sources}


# ---------------------------------------------------------------- 上传前的读包建议
TAX_TEXT = None


def _tax_text():
    global TAX_TEXT
    if TAX_TEXT:
        return TAX_TEXT
    lines = []
    lines.append("Flow（按阶段划分）：")
    for stage, cats in store.FLOW_STAGES:
        lines.append("  - %s：%s" % (stage, " / ".join(cats)))
    lines.append("TV 预设（按功能划分）：")
    for fn, cats in store.TV_FUNCS:
        lines.append("  - %s：%s" % (fn, " / ".join(cats)))
    TAX_TEXT = "\n".join(lines)
    return TAX_TEXT


SUGGEST_SYSTEM = (
    "你是影视短剧团队的资源库管理员。用户刚上传了一个 Skill 压缩包，你要根据包里的内容判断它是什么。"
    "只能依据给出的材料判断，不要编造。必须输出严格的 JSON，不要输出任何解释、不要用代码块包起来。"
    "JSON 字段：kind（固定 skill）、platform（flow 或 tv）、grp（阶段或功能，必须来自给定的分类表）、"
    "cat（分类，必须来自该阶段/功能下的分类）、title（中文短标题，≤16 字，不要带「入口/说明」这类后缀）、"
    "tags（数组，1-3 个短标签）、summary（一句话说明这个包做什么，≤40 字）、"
    "note（一句版本备注，说明这一版给谁用/解决什么，≤30 字）。"
)


def _mk_result(platform, grp, cat, title, tags, summary, note, mode, why):
    return {"kind": "skill", "platform": platform, "grp": grp, "cat": cat,
            "title": title, "tags": tags, "summary": summary, "note": note,
            "mode": mode, "why": why}


KEYWORDS = [
    (("flow", "资产生成", "道具"), 3, ["道具", "prop", "陈设", "器物", "摆件", "旧卡"]),
    (("flow", "资产生成", "角色"), 3, ["角色", "character", "三视图", "造型", "服装", "立绘"]),
    (("flow", "资产生成", "场景"), 3, ["场景", "scene", "环境", "空间", "建筑"]),
    (("flow", "剧本", "剧本预审"), 3, ["预审", "审核", "合规", "风险", "问题清单"]),
    (("flow", "分析手册", "剧本手册"), 2, ["剧本", "手册", "分集", "大纲", "节奏"]),
    (("flow", "单集 Agent", "成片主控"), 2, ["成片", "单集", "主控", "编排", "agent"]),
    (("tv", "文生文", "文本与脚本"), 2, ["脚本", "台词", "改写", "润色"]),
    (("tv", "文生文", "通用"), 1, ["文案", "文本"]),
    (("tv", "图生图", "角色资产"), 2, ["角色", "三视图", "换装", "表情", "人物"]),
    (("tv", "图生图", "场景资产"), 2, ["场景", "建筑", "空间", "环境"]),
    (("tv", "图生图", "分镜与镜头"), 2, ["分镜", "镜头", "景别", "运镜", "线稿"]),
    (("tv", "图生图", "风格与氛围"), 1, ["风格", "氛围", "色调", "质感"]),
    (("tv", "图生图", "安全与合规"), 1, ["合规", "安全", "防撞脸", "敏感"]),
    (("tv", "多模态生视频", "角色资产"), 1, ["视频", "动态", "多模态"]),
]


def _clean_title(info, filename):
    heads = info.get("headings") or []
    fm = info.get("frontmatter") or {}
    desc = re.split(r"[。！？!?\n]", (fm.get("description") or "").strip())[0].strip()
    for h in heads:
        h = (h or "").strip()
        if not h:
            continue
        for sep in ("·", "｜", "|", " - "):
            if sep in h:
                head, tail = h.split(sep, 1)
                if any(k in tail for k in ("入口", "说明", "总览", "导读", "前言")):
                    h = head
        h = re.sub(r"\s*(使用说明|说明|入口|总览|导读)$", "", h).strip()
        if not h:
            continue
        # description 往往比 h1 更完整（「道具资产」vs「道具资产生成」）
        if desc and desc.startswith(h) and 2 < len(desc) <= 14:
            return desc
        if 1 < len(h) <= 20:
            return h[:16]
    fm = info.get("frontmatter") or {}
    d = (fm.get("description") or "").strip()
    if d:
        return re.split(r"[。！？!?\n]", d)[0][:16]
    return (filename or "未命名")[:16]


def _heuristic(info, filename, hint):
    fm = info.get("frontmatter") or {}
    hay = " ".join([
        filename or "", fm.get("name", ""), fm.get("description", ""),
        " ".join(info.get("headings") or []), info.get("excerpt", "")[:3000], hint or "",
    ]).lower()

    tv_marks = [("图生图", 3), ("文生图", 3), ("文生文", 3), ("多模态", 3),
                ("提示词", 2), ("预设", 2)]
    flow_marks = [("skill", 2), ("规则包", 2), ("agent", 2), ("工作流", 2),
                  ("工具调用", 2), ("scripts", 2)]
    tv_score = sum(w * min(hay.count(m), 4) for m, w in tv_marks)
    flow_score = sum(w * min(hay.count(m), 4) for m, w in flow_marks)

    # 只有出现「图生图/文生图/文生文/多模态」这种 TV 专属字眼才算 TV，
    # 否则一律 Flow（Flow Skill 是主力，且它的正文里也常出现「预设」「提示词」）
    strong_tv = any(hay.count(m) for m in ("图生图", "文生图", "文生文", "多模态"))
    plat = "tv" if (strong_tv and tv_score > flow_score) else "flow"
    pool = [k for k in KEYWORDS if k[0][0] == plat] or KEYWORDS

    best, best_score = None, 0
    for key, weight, words in pool:
        score = 0
        hit = []
        for w in words:
            n = hay.count(w.lower())
            if n:
                score += weight * min(n, 4)
                hit.append(w)
        if score > best_score:
            best, best_score = (key, hit), score

    if best:
        (platform, grp, cat), hit = best
        why = "正文里出现「%s」，归到 %s / %s" % ("、".join(hit[:3]), grp, cat)
        if plat == "tv" and tv_score > flow_score:
            why += "（也因为包里写了图生图/提示词这类 TV 的字眼）"
    else:
        platform, grp, cat = "flow", "资产生成", "角色"
        why = "没读出明确线索，先放在 Flow / 资产生成 / 角色，请手动改"

    title = _clean_title(info, filename)
    d = (fm.get("description") or "").strip()
    summary = re.split(r"[。！？!?\n]", d)[0][:40] if d else (info.get("excerpt", "")[:40] or title)
    tags = ["DSH"]
    if "道具" in hay:
        tags.append("道具")
    elif "场景" in hay:
        tags.append("场景")
    elif "角色" in hay:
        tags.append("角色")
    note = "首次上传（%d 个文件，AI 判断为 %s / %s）" % (info.get("file_count", 0), grp, cat)
    return _mk_result(platform, grp, cat, title, tags, summary, note, "rule", why)


def _pick(value, options):
    value = (value or "").strip()
    for o in options:
        if o == value:
            return o
    for o in options:
        if value and (value in o or o in value):
            return o
    return None


def _llm_suggest(cfg, info, filename, hint):
    fm = info.get("frontmatter") or {}
    prompt = "\n".join([
        "【文件】" + (filename or "unknown.zip"),
        "【文件数】%d，解压后约 %d KB" % (info.get("file_count", 0), info.get("total_bytes", 0) // 1024),
        "【目录】" + "、".join(info.get("folders") or [])[:300],
        "【文件清单】" + "、".join((info.get("files") or [])[:30]),
        "【主文件】" + (info.get("main") or ""),
        "【frontmatter】name=%s | description=%s" % (fm.get("name", ""), fm.get("description", "")),
        "【正文标题】" + " / ".join((info.get("headings") or [])[:8]),
        "【正文摘录】" + (info.get("excerpt") or "")[:1800],
        "【上传者补充】" + (hint or "无"),
        "【可选分类表】\n" + _tax_text(),
    ])
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    payload = json.dumps({
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": SUGGEST_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 400,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + cfg["api_key"],
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    raw = (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
    raw = re.sub(r"^```[a-zA-Z]*", "", raw).strip().strip("`").strip()
    got = json.loads(raw)
    platform = "tv" if str(got.get("platform", "")).lower() == "tv" else "flow"
    tax = store.TV_FUNCS if platform == "tv" else store.FLOW_STAGES
    grp = _pick(got.get("grp"), [g for g, _ in tax])
    if not grp:
        grp = tax[0][0]
    cats = dict(tax).get(grp) or []
    cat = _pick(got.get("cat"), cats) or (cats[0] if cats else "")
    title = (got.get("title") or "").strip()[:16] or _clean_title(info, filename)
    tags = got.get("tags") or ["DSH"]
    if isinstance(tags, str):
        tags = [t for t in re.split(r"[，,、\s]+", tags) if t]
    tags = ("DSH " + " ".join(tags)).split()
    seen, uniq = set(), []
    for t in tags:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    summary = (got.get("summary") or "").strip()[:60]
    note = (got.get("note") or "").strip()[:60] or "首次上传"
    return _mk_result(platform, grp, cat, title, uniq[:4], summary, note, "llm", "")


def suggest(conn, info, filename="", hint=""):
    # 读包建议：有 Key 走模型，失败或没 Key 用规则兜底
    cfg = load_config()
    if cfg.get("api_key"):
        try:
            return _llm_suggest(cfg, info, filename, hint)
        except Exception as exc:  # noqa: BLE001
            res = _heuristic(info, filename, hint)
            res["why"] = "%s（模型调用失败已回落：%s）" % (res.get("why") or "", str(exc)[:60])
            return res
    return _heuristic(info, filename, hint)
