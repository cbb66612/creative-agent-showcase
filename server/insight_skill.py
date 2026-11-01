# -*- coding: utf-8 -*-
"""为 Skill 生成「一句话就能看懂」的结构化解读，缓存进数据库。

用法：
  runtime\\python.exe insight_skill.py            # 只补没有解读的
  runtime\\python.exe insight_skill.py --all      # 全部重算
  runtime\\python.exe insight_skill.py --id 14    # 只算某一条
"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ai  # noqa: E402
import store  # noqa: E402

PROMPT = """你在整理一个影视制作团队内部 Skill 库的目录信息。
下面是一个 Skill 包的正文（Markdown）。请**只依据正文**，输出严格 JSON（不要代码块、不要解释），字段如下：

{
  "purpose": "它是干什么的，1-2 句话，动词开头",
  "inputs": ["需要用户提供什么，最多 4 条，每条 15 字内"],
  "outputs": ["它产出什么，最多 4 条，每条 15 字内"],
  "audience": ["给谁用/用在哪，最多 3 条，每条 12 字内"],
  "when": "什么时候该用它，1 句话",
  "not_for": "什么时候不该用它，1 句话；没有依据就写空串"
}

要求：
- 全部用中文，具体、可执行，不要复述标题，不要说「本 skill」。
- 拿不准的字段给空数组或空串，不要编造。
"""


def ask(cfg, text):
    import urllib.request

    body = json.dumps({
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": "你只输出合法 JSON，不写任何多余文字。"},
            {"role": "user", "content": PROMPT + "\n\n---\n" + text[:12000]},
        ],
        "temperature": 0.2,
    }).encode("utf-8")
    req = urllib.request.Request(
        cfg["base_url"].rstrip("/") + "/chat/completions",
        data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + cfg["api_key"]},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read().decode("utf-8"))
    out = data["choices"][0]["message"]["content"].strip()
    if out.startswith("```"):
        out = out.strip("`")
        out = out[out.find("{"):]
    out = out[out.rfind("}") + 1:] if out.rfind("}") < out.find("{") else out
    start, end = out.find("{"), out.rfind("}")
    return json.loads(out[start:end + 1])


def main():
    args = sys.argv[1:]
    only_all = "--all" in args
    only_id = None
    if "--id" in args:
        only_id = int(args[args.index("--id") + 1])

    cfg = ai.load_config()
    if not cfg.get("api_key"):
        print("没有配置 DeepSeek Key，先到 /admin/settings 配好")
        return 1

    with store.db() as conn:
        store.migrate(conn)
        store.ensure_insights(conn)
        sql = ("SELECT r.id, r.title, v.id AS vid FROM resources r "
               "JOIN versions v ON v.id = r.current_version_id "
               "WHERE r.kind='skill' AND r.platform='flow'")
        rows = conn.execute(sql).fetchall()
        if only_id:
            rows = [r for r in rows if r["id"] == only_id]
        print("待处理", len(rows), "个 Skill")
        done = 0
        for r in rows:
            if not only_all and not only_id:
                has = conn.execute(
                    "SELECT 1 FROM skill_insights WHERE version_id=?", (r["vid"],)).fetchone()
                if has:
                    continue
            files = conn.execute(
                "SELECT path FROM files WHERE version_id=?", (r["vid"],)).fetchall()
            main = None
            for f in files:
                if f["path"].lower() in ("skill.md", "readme.md"):
                    main = f["path"]
                    break
            if not main:
                print("  跳过（没有 SKILL.md）", r["title"])
                continue
            ver = conn.execute("SELECT * FROM versions WHERE id=?", (r["vid"],)).fetchone()
            text = store.read_version_file(ver, main)
            try:
                info = ask(cfg, text)
            except Exception as e:
                print("  失败", r["title"], e)
                continue
            conn.execute(
                "INSERT OR REPLACE INTO skill_insights(resource_id, version_id, json, created_at) "
                "VALUES(?,?,?,datetime('now','localtime'))",
                (r["id"], r["vid"], json.dumps(info, ensure_ascii=False)),
            )
            conn.commit()
            done += 1
            print("  ok", r["title"], "→", info.get("purpose", "")[:40])
        print("完成", done, "条")


if __name__ == "__main__":
    sys.exit(main())
