# -*- coding: utf-8 -*-
"""把「镜库」抓取数据（catalog.json）导入灵感引擎 Skill 预设库。

用法：
    python import_moke.py                 # 导入默认位置的数据
    python import_moke.py --dry-run       # 只看统计，不写库
    python import_moke.py --catalog D:\\x\\catalog.json --media D:\\x\\public\\media

幂等：按 source_key（镜库原始 key）去重，重复跑只会更新正文，不会产生重复条目。
封面：只复制「本地已下载」的图片到 data/covers/，远程图先跳过（避免外部防盗链）。
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402

DEFAULT_ROOT = Path(r"D:\Projects_ai\dramatv-jingku")
COVERS = store.DATA / "covers"

EMOJI_BY_CAT = {
    "人声·AI配音": "🎙️", "音乐·乐器技法": "🎻", "音乐·曲风": "🎵", "音乐·人声": "🎤",
    "音乐·语言": "🗣️", "音乐·曲式结构": "🎼", "肖像大师": "🧑‍🎨", "艺术风格大师": "🎨",
    "电影风格": "🎬", "电影感组合器": "🎞️", "胶片": "📷", "摄影技法": "📸",
    "动作打斗": "🥋", "微表情": "😐", "光影": "💡", "构图": "🖼️", "相机机型": "📷",
    "剪辑": "✂️", "镜头特性": "🔭", "叙事": "📖", "美学修饰": "✨", "数字人创作": "🧍",
    "视觉特效": "💥", "氛围": "🌫️", "景别": "🔍", "运镜": "🎥", "光线": "🌅",
    "人物表现": "🎭", "风格": "🖌️", "文本": "📝", "场景表现": "🏞️", "美术资产": "🧱",
    "类型与风格": "🎭",
}


def hue_of(text):
    h = 0
    for ch in text or "":
        h = (h * 31 + ord(ch)) % 360
    return h


def emoji_of(cat):
    if cat in EMOJI_BY_CAT:
        return EMOJI_BY_CAT[cat]
    for key, emo in EMOJI_BY_CAT.items():
        if key and key in (cat or ""):
            return emo
    if (cat or "").startswith("Skill"):
        return "🧩"
    return "📄"


def one_line(text, limit=60):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", default=str(DEFAULT_ROOT / "data" / "catalog.json"))
    ap.add_argument("--visuals", default=str(DEFAULT_ROOT / "data" / "visuals.json"))
    ap.add_argument("--media", default=str(DEFAULT_ROOT / "public" / "media"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    catalog_path = Path(args.catalog)
    if not catalog_path.exists():
        print("找不到 catalog.json：%s" % catalog_path)
        return 2
    data = json.loads(catalog_path.read_text(encoding="utf-8"))
    posts = data.get("posts") or []
    if args.limit:
        posts = posts[: args.limit]
    print("读取 %d 条" % len(posts))

    # 封面索引：sourceKey -> 本地文件
    visuals = {}
    vpath = Path(args.visuals)
    if vpath.exists():
        try:
            v = json.loads(vpath.read_text(encoding="utf-8"))
            visuals = v.get("posts") or {}
            print("封面索引 %d 条" % len(visuals))
        except Exception as exc:  # noqa: BLE001
            print("visuals.json 解析失败，跳过封面：%s" % exc)

    media_dir = Path(args.media)
    COVERS.mkdir(parents=True, exist_ok=True)

    items = []
    copied = skipped_cover = 0
    for i, p in enumerate(posts):
        key = p.get("sourceKey") or ("moke:unknown:%d" % i)
        cat = (p.get("category") or "").strip()
        kind = "skill" if p.get("kind") == "skill" else "preset"
        title = (p.get("title") or p.get("originalTitle") or p.get("titleEn") or "").strip()
        if not title:
            title = one_line(p.get("prompt"), 24) or "(无标题)"
        prompt = p.get("prompt") or ""
        summary = (p.get("description") or "").strip() or one_line(prompt, 60)
        cover_url = ""
        vis = visuals.get(key) if isinstance(visuals, dict) else None
        if vis and vis.get("origin") == "local" and vis.get("thumbnail"):
            src = media_dir / Path(vis["thumbnail"]).name
            if src.exists():
                ext = src.suffix.lower() or ".png"
                digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
                dst = COVERS / (digest + ext)
                if not args.dry_run and not dst.exists():
                    shutil.copyfile(src, dst)
                    copied += 1
                cover_url = "/cover/" + digest + ext
            else:
                skipped_cover += 1
        items.append({
            "kind": kind,
            "platform": "ext",
            "grp": cat or "未分类",
            "cat": "",
            "external_key": key,
            "source_key": key,
            "title": title,
            "summary": summary,
            "body": prompt,
            "tags": p.get("tags") or [],
            "source": (p.get("source") or "MOKE AIGC"),
            "source_url": p.get("sourceUrl") or "",
            "cover_url": cover_url,
            "hue": hue_of(cat),
            "emoji": emoji_of(cat),
            "created_at": "2026-10-09 12:00:00",
        })

    print("准备写入 %d 条（本地封面 %d，图片缺失 %d）" % (len(items), copied, skipped_cover))
    if args.dry_run:
        from collections import Counter
        c = Counter(it["grp"] for it in items)
        for g, n in c.most_common(12):
            print("  %5d  %s" % (n, g))
        return 0

    added = updated = 0
    with store.db() as conn:
        store.ensure_dirs()
        store.migrate(conn)
        for start in range(0, len(items), 500):
            chunk = items[start:start + 500]
            a, u = store.import_items(conn, chunk)
            added += a
            updated += u
            print("  %d/%d 新增 %d 更新 %d" % (min(start + 500, len(items)), len(items), added, updated))

    print("完成：新增 %d 条，更新 %d 条，封面文件 %d 个" % (added, updated, copied))
    print("数据目录：%s" % store.DATA)
    return 0


if __name__ == "__main__":
    sys.exit(main())
