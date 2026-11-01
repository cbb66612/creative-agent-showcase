# -*- coding: utf-8 -*-
"""把镜库的远程封面图下载到本地（避免外链失效 / 防盗链）。

只下载 visuals.json 里 origin=remote 的 thumbnail，存到 data/covers/<hash>.webp，
并把 resources.cover_url 更新成本地路径。已存在的文件会跳过，可重复运行。
"""
import hashlib
import json
import os
import sqlite3
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402

ROOT = Path(r"D:\Projects_ai\dramatv-jingku")
COVERS = store.DATA / "covers"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"


def fetch_one(item):
    key, url = item
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    dst = COVERS / (digest + ".webp")
    if dst.exists() and dst.stat().st_size > 800:
        return key, "/cover/" + dst.name, "cached"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://www.mokeaigc.ai/"})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = resp.read()
        if len(data) < 800:
            return key, "", "tiny"
        dst.write_bytes(data)
        return key, "/cover/" + dst.name, "ok"
    except Exception as exc:  # noqa: BLE001
        return key, "", "err:%s" % exc


def main():
    vpath = ROOT / "data" / "visuals.json"
    if not vpath.exists():
        print("找不到 visuals.json")
        return 2
    posts = json.loads(vpath.read_text(encoding="utf-8")).get("posts") or {}
    todo = [(k, x["thumbnail"]) for k, x in posts.items()
            if x.get("origin") == "remote" and x.get("thumbnail")]
    print("待下载 %d 张" % len(todo))
    COVERS.mkdir(parents=True, exist_ok=True)

    done = {"ok": 0, "cached": 0, "err": 0}
    results = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fetch_one, it) for it in todo]
        for i, fut in enumerate(as_completed(futures), 1):
            key, path, status = fut.result()
            if path:
                results.append((path, key))
                done["cached" if status == "cached" else "ok"] += 1
            else:
                done["err"] += 1
            if i % 200 == 0:
                print("  %d/%d  ok=%d cached=%d err=%d" % (i, len(todo), done["ok"], done["cached"], done["err"]))

    with store.db() as conn:
        for path, key in results:
            conn.execute("UPDATE resources SET cover_url=? WHERE source_key=?", (path, key))
    print("完成：新增 %d，复用 %d，失败 %d" % (done["ok"], done["cached"], done["err"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
