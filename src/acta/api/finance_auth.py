"""PIN gate for the finance screens.

Sized to the real threat (another device on the network reading the numbers):
a PBKDF2-hashed PIN, short-lived in-memory session tokens and a lockout after
repeated failures. A restart just asks for the PIN again.
"""

import datetime
import hashlib
import re
import secrets
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from acta.api.deps import open_db, open_db_rw
from acta.config import TZ

router = APIRouter()


FINANCE_TOKEN_TTL_MS  = 30 * 60 * 1000   # sliding idle expiry


FINANCE_LOCKOUT_MS    = 60 * 1000


FINANCE_MAX_ATTEMPTS  = 5


_PBKDF2_ROUNDS        = 200_000


_finance_tokens: dict = {}      # token -> expires_ms


_finance_attempts: list = []    # failed-attempt timestamps (ms), pruned to last 60s


_finance_lockout_until = 0      # ms; unlock attempts rejected before this


def _finance_pbkdf2(pin: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, _PBKDF2_ROUNDS).hex()


def require_finance_token(x_finance_token: Optional[str] = Header(None)):
    """FastAPI dependency gating every /api/finance/* route except unlock
    and setup-pin. Sliding expiry: any authenticated call extends the session."""
    now_ms = int(datetime.datetime.now(TZ).timestamp() * 1000)
    exp = _finance_tokens.get(x_finance_token or "")
    if not exp or exp < now_ms:
        raise HTTPException(401, "unlock required")
    _finance_tokens[x_finance_token] = now_ms + FINANCE_TOKEN_TTL_MS


class FinancePinIn(BaseModel):
    pin: str


@router.get("/api/finance/status")
def finance_status():
    """Public (no token) — whether a PIN has been configured yet, so the lock
    UI knows to offer 'set a passcode' vs 'enter passcode'."""
    con = open_db()
    try:
        row = con.execute(
            "SELECT 1 FROM finance_config WHERE key='pin_hash'").fetchone()
    finally:
        con.close()
    return {"pin_set": bool(row)}


@router.post("/api/finance/setup-pin")
def finance_setup_pin(body: FinancePinIn):
    """Bootstrap only — refuses once a PIN is already configured. There is no
    change-PIN flow yet (phase 2+); reset by deleting the finance_config rows.
    Exactly 4 digits to match the fixed 4-cell keypad UI."""
    if not re.fullmatch(r"\d{4}", body.pin):
        raise HTTPException(400, "pin must be exactly 4 digits")
    con = open_db_rw()
    try:
        existing = con.execute(
            "SELECT 1 FROM finance_config WHERE key='pin_hash'").fetchone()
        if existing:
            raise HTTPException(403, "PIN already configured")
        salt = secrets.token_bytes(16)
        pin_hash = _finance_pbkdf2(body.pin, salt)
        con.execute("INSERT INTO finance_config(key, value) VALUES('pin_salt', ?)",
                    (salt.hex(),))
        con.execute("INSERT INTO finance_config(key, value) VALUES('pin_hash', ?)",
                    (pin_hash,))
        con.commit()
    finally:
        con.close()
    return {"status": "ok"}


@router.post("/api/finance/unlock")
def finance_unlock(body: FinancePinIn):
    global _finance_lockout_until
    now_ms = int(datetime.datetime.now(TZ).timestamp() * 1000)
    if now_ms < _finance_lockout_until:
        wait_s = int((_finance_lockout_until - now_ms) / 1000) + 1
        raise HTTPException(429, f"too many attempts — wait {wait_s}s")

    con = open_db()
    try:
        salt_row = con.execute(
            "SELECT value FROM finance_config WHERE key='pin_salt'").fetchone()
        hash_row = con.execute(
            "SELECT value FROM finance_config WHERE key='pin_hash'").fetchone()
    finally:
        con.close()
    if not salt_row or not hash_row:
        raise HTTPException(400, "no PIN configured — call /api/finance/setup-pin first")

    # compare_digest, not ==, so the comparison doesn't leak the matching
    # prefix length through timing.
    ok = secrets.compare_digest(
        _finance_pbkdf2(body.pin, bytes.fromhex(salt_row["value"])), hash_row["value"])
    if not ok:
        _finance_attempts.append(now_ms)
        cutoff = now_ms - FINANCE_LOCKOUT_MS
        while _finance_attempts and _finance_attempts[0] < cutoff:
            _finance_attempts.pop(0)
        if len(_finance_attempts) >= FINANCE_MAX_ATTEMPTS:
            _finance_lockout_until = now_ms + FINANCE_LOCKOUT_MS
            _finance_attempts.clear()
        raise HTTPException(401, "wrong passcode")

    _finance_attempts.clear()
    # Drop expired tokens so the dict can't grow without bound across a long
    # uptime (one entry per unlock, never cleaned up otherwise).
    for t in [t for t, exp in _finance_tokens.items() if exp < now_ms]:
        del _finance_tokens[t]
    token = secrets.token_hex(24)
    _finance_tokens[token] = now_ms + FINANCE_TOKEN_TTL_MS
    return {"token": token, "expires_in": FINANCE_TOKEN_TTL_MS // 1000}
