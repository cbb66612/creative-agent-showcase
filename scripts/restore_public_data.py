"""Restore the published, Flow-free snapshot into a local working data directory."""
from pathlib import Path
import shutil
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
EXPORT = ROOT / "data-export"
DATA = ROOT / "server" / "data"


def main():
    if not (EXPORT / "aikb.db").is_file():
        raise SystemExit("Missing data-export/aikb.db")
    if (DATA / "aikb.db").exists():
        raise SystemExit("server/data/aikb.db already exists; refusing to overwrite live data")
    DATA.mkdir(parents=True, exist_ok=True)
    shutil.copy2(EXPORT / "aikb.db", DATA / "aikb.db")
    shutil.copytree(EXPORT / "covers", DATA / "covers", dirs_exist_ok=True)
    with sqlite3.connect(DATA / "aikb.db") as db:
        count = db.execute("SELECT count(*) FROM resources").fetchone()[0]
        flow = db.execute("SELECT count(*) FROM resources WHERE platform='flow'").fetchone()[0]
        if count != 6176 or flow:
            raise SystemExit("Public data check failed")
    print(f"Restored {count} public resources; Flow Skill records: {flow}")


if __name__ == "__main__":
    main()
