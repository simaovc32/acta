"""Create or rotate the write-only capture key (for a chat assistant that logs purchases/food).

    python tools/capture_key.py rotate

Writes only a SHA-256 hash of the key into finance_config (capture_key_hash) and the
key itself into a file only this user can read ($ACTA_DATA_DIR/capture.key, or
$ACTA_CAPTURE_KEY_FILE). The key is never printed. Rotating invalidates the old key
at once; a client that reads the file on every call needs no restart.
"""
import os
import sqlite3
import sys

from acta import config
from acta.finance import capture as fc

DB = config.ACTA_DB
KEY_FILE = os.environ.get("ACTA_CAPTURE_KEY_FILE", os.path.join(config.DATA_DIR, "capture.key"))


def rotate() -> None:
    key = fc.new_key()
    os.makedirs(os.path.dirname(KEY_FILE), exist_ok=True)
    tmp = KEY_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key + "\n")
    con = sqlite3.connect(DB)
    try:
        con.execute("INSERT INTO finance_config(key, value) VALUES('capture_key_hash', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (fc.key_hash(key),))
        con.commit()
    finally:
        con.close()
    os.replace(tmp, KEY_FILE)
    print(f"capture key rotated: hash stored in {DB}, key file {KEY_FILE} (mode 600)")


if __name__ == "__main__":
    if sys.argv[1:] != ["rotate"]:
        sys.exit(__doc__)
    rotate()
