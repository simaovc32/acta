"""Nightly local backup of acta.db.

- Uses SQLite's online backup API (safe while the API and ingest are running).
- Runs integrity_check on the copy before keeping it.
- Gzips to $ACTA_DATA_DIR/backups/acta-YYYYMMDD.db.gz, prunes to the newest 14.

    acta backup          (cron: 30 2 * * *)
"""
import datetime
import gzip
import shutil
import sqlite3
import sys
from pathlib import Path

from acta import config

DB = Path(config.ACTA_DB)
BACKUP_DIR = Path(config.BACKUP_DIR)
KEEP = 14


def main() -> int:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today().strftime("%Y%m%d")
    tmp = BACKUP_DIR / f"acta-{today}.db.tmp"
    out = BACKUP_DIR / f"acta-{today}.db.gz"

    # Online backup — consistent snapshot even mid-write.
    src = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    dst = sqlite3.connect(tmp)
    with dst:
        src.backup(dst)
    src.close()

    # Verify the copy before trusting it.
    ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
    dst.close()
    if ok != "ok":
        tmp.unlink(missing_ok=True)
        print(f"ERROR: integrity_check failed: {ok}", file=sys.stderr)
        return 1

    with open(tmp, "rb") as f_in, gzip.open(out, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    tmp.unlink()

    # Prune oldest beyond KEEP.
    backups = sorted(BACKUP_DIR.glob("acta-*.db.gz"))
    for old in backups[:-KEEP]:
        old.unlink()

    print(f"ok: {out.name} ({out.stat().st_size // 1024} KB), {min(len(backups), KEEP)} backups kept")
    return 0


if __name__ == "__main__":
    sys.exit(main())
