"""在专用窗口里探测 TV 接口的真实路径。"""

import json
import sys
import os

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from tv_dump import evaluate  # noqa: E402

JS = r"""
(async () => {
  const cands = [
    '/api-data/api/v1/auth/session',
    '/api-data/api/v1/features',
    '/api-data/api/features',
    '/api-data/features',
    '/admin/api/features',
    '/api-data/api/v1/preset-categories',
    '/api-data/api/v1/presets?page=1&pageSize=2',
    '/api-data/api/v1/skills?page=1&pageSize=2',
    '/api-data/api/v1/config/features',
    '/api-data/api/v1/preset/features'
  ];
  const out = [];
  for (const p of cands) {
    try {
      const r = await fetch(p, {headers: {'Accept': 'application/json'}});
      const t = await r.text();
      out.push({p, status: r.status, head: t.slice(0, 160)});
    } catch (e) { out.push({p, status: 'ERR', head: String(e).slice(0, 120)}); }
  }
  return out;
})()
"""

if __name__ == "__main__":
    data = evaluate(JS, timeout=90)
    for row in data or []:
        print("%-45s %-5s %s" % (row["p"], row["status"], row["head"].replace("\n", " ")))
