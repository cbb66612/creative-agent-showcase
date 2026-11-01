# -*- coding: utf-8 -*-
"""内置 Agent：Harness 化的工具调用循环。

采用工具调用、会话记忆与可追溯步骤：
- Harness = Workflow + Tool + Memory/Context + Guardrail + Sandbox + Trace + RAG
- Tool 有 schema / 风险等级 / 触发条件 / 禁用条件 / 审计
- Agent 主循环只做「推理 → 工具 → 观察 → 输出」，横切逻辑放中间件
- 上下文先确定性裁剪，再交给模型；记忆分四层（窗口 / 工作 / 会话 / 长期）
"""
import json
import os
import re
import time
import urllib.request

import ai
import store

MAX_STEPS = 5           # 单轮最多几次模型推理
MAX_TOOL_CALLS = 8      # 单轮最多几次工具调用
DEADLINE = 75           # 秒
MAX_QUESTION = 400
MAX_ANSWER = 4000
MAX_TOOL_RESULT = 2200  # 单条工具结果进上下文前的裁剪长度


class AgentError(Exception):
    pass


# ---------------------------------------------------------------- 工具实现（全部只读）
def _t_search(conn, args, user):
    q = str(args.get("query") or "").strip()[:80]
    if not q:
        raise AgentError("query 不能为空")
    platform = args.get("platform") or ""
    if platform not in ("", "flow", "tv", "ext"):
        platform = ""
    limit = args.get("limit") or 6
    try:
        limit = max(1, min(int(limit), 12))
    except Exception:
        limit = 6
    rows = ai.search(conn, q, limit=limit, platform=platform)
    out = []
    for row in rows:
        r = dict(row)
        out.append({
            "id": r["id"],
            "title": r["title"],
            "platform": r.get("platform", ""),
            "grp": r.get("grp", ""),
            "cat": r.get("cat", ""),
            "kind": r.get("kind", ""),
            "summary": (r.get("summary") or "")[:120],
        })
    return {"count": len(out), "items": out}


def _t_open(conn, args, user):
    rid = args.get("id")
    try:
        rid = int(rid)
    except Exception:
        raise AgentError("id 必须是数字")
    row = store.get_resource(conn, rid)
    if not row:
        raise AgentError("没有 id=%s 的资源" % rid)
    r = dict(row)
    text = r.get("body") or ""
    files = []
    main_file = ""
    vid = r.get("current_version_id")
    if vid:
        files = [f["path"] for f in store.get_files(conn, vid)][:40]
        # 有版本包的 Skill：正文在 SKILL.md / README.md 里，取主文件原文
        pick = [p for p in files if p.lower() in ("skill.md", "readme.md")] or files[:1]
        if pick:
            main_file = pick[0]
            ver = store.get_version(conn, vid)
            try:
                text = store.read_version_file(ver, main_file) or text
            except Exception:  # noqa: BLE001
                pass
    return {
        "id": r["id"],
        "title": r["title"],
        "platform": r["platform"],
        "grp": r.get("grp", ""),
        "cat": r.get("cat", ""),
        "kind": r.get("kind", ""),
        "summary": r.get("summary", ""),
        "usage_count": r.get("usage_count", 0),
        "main_file": main_file,
        "text": (text or "")[:3000],
        "files": files,
    }


def _t_taxonomy(conn, args, user):
    out = {"flow": {}, "tv": {}, "ext_groups": []}
    for stage, cats in store.FLOW_STAGES:
        out["flow"][stage] = cats
    for fn, cats in store.TV_FUNCS:
        out["tv"][fn] = cats
    out["ext_groups"] = [g["grp"] for g in store.groups_of(conn, "ext")]
    return out


def _t_stats(conn, args, user):
    c = store.count_by(conn)
    return {
        "总资源数": sum(c.values()),
        "flow_skill": c.get("skills", 0),
        "tv_预设": c.get("presets", 0),
        "镜库": c.get("ext", 0),
    }


TOOLS = [
    {
        "name": "search_library",
        "description": "在创作资源工作台的公开资源库里按关键词搜索。库中包含 TV 预设与 Skill 正文，以及外部提示词和 Skill 正文。想知道库里有什么、找某个用途的资源时先调它。",
        "risk": "LOW",
        "need_confirm": False,
        "handler": _t_search,
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "关键词，中文即可，例如「三视图」「换装」「道具」"},
                "platform": {"type": "string", "enum": ["", "flow", "tv", "ext"], "description": "只搜某个平台，留空表示全库"},
                "limit": {"type": "integer", "description": "返回条数，1-12，默认 6"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "open_resource",
        "description": "打开一条资源的正文（截断到 3000 字）和包内文件清单。用户问「这个 skill 怎么用」「那条预设具体写了什么」时调用。",
        "risk": "LOW",
        "need_confirm": False,
        "handler": _t_open,
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "integer", "description": "资源 id，来自 search_library 的返回"}},
            "required": ["id"],
        },
    },
    {
        "name": "list_taxonomy",
        "description": "列出当前资源库中的平台与分类。用户问「有哪些分类」「该传到哪里」时调用。",
        "risk": "LOW",
        "need_confirm": False,
        "handler": _t_taxonomy,
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "library_stats",
        "description": "返回库里各平台的资源数量。",
        "risk": "LOW",
        "need_confirm": False,
        "handler": _t_stats,
        "parameters": {"type": "object", "properties": {}},
    },
]

_BY_NAME = {t["name"]: t for t in TOOLS}

SYSTEM = """你是「创作资源工作台 · Skill 预设库」里的内置 Agent，服务创作者，帮助他们检索和理解 Skill 与预设资源。

你可以调用工具查库。规则：
1. 涉及「库里有什么 / 某条资源写了什么 / 分类怎么分」时，先调工具，不要凭印象回答。
2. 每条结论后面用 [id] 标出它来自哪条资源，例如 [14]；没查到的就直说没找到，不要编。
   引用只能写资源 id，不要给分类或工具编编号（不要出现 [taxonomy] 这种）。
3. 最多 5 步推理，够用就停；同一个工具同样参数不要重复调。
4. 回答用中文、短句、能直接照着做；需要步骤就给编号列表。
5. 你可以说明某条 Skill 的用法、适合场景、和另一条的区别；不确定时给出「怎么验证」的建议。
6. 不要输出 JSON、不要提「我是模型」、不要写和库无关的通用大道理。"""


# ---------------------------------------------------------------- 模型调用
def _chat(cfg, messages, tools):
    """一次 chat/completions 调用，返回 assistant message dict。"""
    url = (cfg.get("base_url") or "https://api.deepseek.com").rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg.get("model") or "deepseek-chat",
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 1200,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + (cfg.get("api_key") or ""),
    })
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise AgentError("模型调用失败：%s" % str(exc)[:160])
    choices = data.get("choices") or []
    if not choices:
        raise AgentError("模型没有返回结果")
    msg = choices[0].get("message") or {}
    usage = data.get("usage") or {}
    return msg, usage


def tool_schemas():
    out = []
    for t in TOOLS:
        out.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            },
        })
    return out


def _short(obj, n=MAX_TOOL_RESULT):
    s = json.dumps(obj, ensure_ascii=False) if not isinstance(obj, str) else obj
    if len(s) <= n:
        return s
    return s[:n] + "…(已截断)"


def _run_tool(conn, user, name, args):
    tool = _BY_NAME.get(name)
    if not tool:
        raise AgentError("没有这个工具：%s" % name)
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise AgentError("参数必须是对象")
    return tool["handler"](conn, args, user)


# ---------------------------------------------------------------- 主循环
def run(conn, user, question, history=None, mock=None):
    """执行一轮 Agent。返回 {ok, mode, answer, steps, sources, tokens, error}"""
    question = (question or "").strip()
    if not question:
        return {"ok": False, "error": "问题不能为空"}
    if len(question) > MAX_QUESTION:
        question = question[:MAX_QUESTION]

    cfg = ai.load_config()
    if not cfg.get("api_key") and not mock:
        return {"ok": False, "mode": "no-key",
                "error": "还没配 DeepSeek Key。Agent 要靠模型决定调哪个工具，配好 Key 才能用（右上角「AI 设置」）。"}

    messages = [{"role": "system", "content": SYSTEM}]
    for h in (history or [])[-6:]:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": question})

    schemas = tool_schemas()
    steps = []
    sources = {}
    seen_calls = set()
    started = time.time()
    tokens = {"prompt": 0, "completion": 0}
    answer = ""

    for step_no in range(1, MAX_STEPS + 1):
        if time.time() - started > DEADLINE:
            answer = "这次查得有点久，先停一下。可以把问题说得更具体些，或者直接用资源库的搜索。"
            break
        try:
            if mock:
                msg, usage = mock(step_no, messages)
            else:
                msg, usage = _chat(cfg, messages, schemas)
        except AgentError as exc:
            return {"ok": False, "mode": "error", "error": str(exc), "steps": steps, "sources": []}
        tokens["prompt"] += int(usage.get("prompt_tokens") or 0)
        tokens["completion"] += int(usage.get("completion_tokens") or 0)

        calls = msg.get("tool_calls") or []
        content = (msg.get("content") or "").strip()

        if not calls:
            answer = content
            break

        messages.append({"role": "assistant", "content": content, "tool_calls": calls})

        if len(steps) + len(calls) > MAX_TOOL_CALLS:
            messages.append({"role": "user", "content": "工具调用次数已到上限，请基于已有信息直接回答。"})
            continue

        for call in calls:
            fn = (call.get("function") or {})
            name = fn.get("name") or ""
            raw_args = fn.get("arguments") or "{}"
            t0 = time.time()
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            except Exception:
                args = None
            key = name + "|" + json.dumps(args, ensure_ascii=False, sort_keys=True)
            if key in seen_calls:
                result = {"error": "重复调用，已跳过。请换参数或直接回答。"}
                entry = {"n": len(steps) + 1, "tool": name, "args": args, "ok": False,
                         "ms": 0, "summary": "重复调用被拦下"}
            else:
                seen_calls.add(key)
                try:
                    result = _run_tool(conn, user, name, args)
                    ok = True
                    summary = _summarize(name, result)
                except AgentError as exc:
                    result = {"error": str(exc)}
                    ok = False
                    summary = "失败：" + str(exc)[:80]
                entry = {"n": len(steps) + 1, "tool": name, "args": args, "ok": ok,
                         "ms": int((time.time() - t0) * 1000), "summary": summary}
            steps.append(entry)
            if ok:
                for item in _collect_sources(name, result):
                    sources[item["id"]] = item
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id") or ("call_%d" % len(steps)),
                "content": _short(result),
            })

    if not answer:
        answer = "这次没查到能回答的内容。换个关键词再问一次，或者直接去资源库搜。"
    answer = answer[:MAX_ANSWER]
    return {
        "ok": True,
        "mode": "agent",
        "answer": answer,
        "steps": steps,
        "sources": list(sources.values()),
        "tokens": tokens,
        "elapsed_ms": int((time.time() - started) * 1000),
    }


def _summarize(name, result):
    if name == "search_library":
        n = result.get("count", 0)
        return "搜到 %d 条" % n
    if name == "open_resource":
        return "打开《%s》%d 字" % (result.get("title", ""), len(result.get("text") or ""))
    if name == "list_taxonomy":
        return "分类：Flow %d 阶段 / TV %d 功能 / 镜库 %d 类" % (
            len(result.get("flow") or {}), len(result.get("tv") or {}), len(result.get("ext_groups") or []))
    if name == "library_stats":
        return "资源总数 %s" % result.get("总资源数", "?")
    return "完成"


def _collect_sources(name, result):
    out = []
    if name == "search_library":
        for it in result.get("items") or []:
            out.append({"id": it["id"], "title": it["title"], "platform": it.get("platform", ""),
                        "grp": it.get("grp", "")})
    elif name == "open_resource":
        out.append({"id": result["id"], "title": result["title"],
                    "platform": result.get("platform", ""), "grp": result.get("grp", "")})
    return out
