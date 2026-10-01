"""Write-only capture path for a chat assistant: log a purchase from a sentence.

The assistant never gets the finance PIN and never reads finance data. It holds a separate
capture key that can only:
  * add an expense (capped per entry and per day, always `pending`, source
    `whatsapp`), and
  * undo one of its own recent, still-pending entries.

Food rule: a coffee or a meal is eaten when it is bought, a grocery is not. So a
purchase also logs food only when ALL of these hold:
  * its category is coffee or eat (Coffee & snacks, Eating out),
  * an item was named ("mocha"), and it is a purchase from today,
  * the item is already in the food library (no invented calories: the food entry
    is the library's own kcal per 100 g times its stored portion),
  * the match is unambiguous, not alcoholic, and not already logged in the last
    half hour.
Anything else is a ledger entry only, and the reply says why. Groceries and
shopping never touch the food log, whatever the wording.

Pure database logic: takes an open sqlite connection so it can be tested against a
throwaway database. Events.json and ingest are handled by api/routers/capture.py.
"""
import datetime
import hashlib
import hmac
import secrets
from typing import Optional

from acta.finance import spend as fs
from acta.tracking import nutrition

FOOD_CATEGORIES = ("coffee", "eat")
MAX_EUR = 200.0            # per entry; larger amounts go through the dashboard
MAX_PER_DAY = 30           # entries captured per day, undone ones included
MAX_AGE_DAYS = 60          # how far back a purchase may be dated
UNDO_WINDOW_H = 24         # an entry can be undone from chat this long
DUP_MIN = 30               # same food twice within this many minutes is one meal
CASH_WORDS = ("cash", "dinheiro", "numerario")
# food_log.note marker for a bare food capture (no purchase attached) — lets
# /api/food/capture/undo find only rows it wrote itself, same idea as the
# "via purchase #<id>" note the purchase-linked food bridge uses.
ASSISTANT_FOOD_NOTE = "via assistant"


# ── the capture key (only its hash is stored server-side) ─────────────────
def new_key() -> str:
    return secrets.token_urlsafe(32)


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def key_ok(key: Optional[str], stored_hash: Optional[str]) -> bool:
    return bool(key) and bool(stored_hash) and hmac.compare_digest(key_hash(key), stored_hash)


# ── what was bought, and where the money came from ────────────────────────
def resolve_account(con, hint: Optional[str], derived_ids) -> tuple:
    """(account_id, None) or (None, why not). No hint means the last-used account;
    "cash" means the cash account; otherwise a name that matches exactly one account."""
    accounts = fs.picker_accounts(con, derived_ids)
    if not accounts:
        return None, "no account can take a purchase"
    if not (hint or "").strip():
        return fs.default_account_id(con, accounts), None
    h = nutrition.norm_key(hint)
    found = [a for a in accounts if nutrition.norm_key(a["name"]) == h]
    if not found and h in CASH_WORDS:
        found = [a for a in accounts if a["kind"] == "cash"]
    if not found and h:
        found = [a for a in accounts if h in nutrition.norm_key(a["name"])]
    if len(found) == 1:
        return found[0]["id"], None
    names = ", ".join(a["name"] for a in accounts)
    why = "matches several accounts" if found else "is not an account"
    return None, f"'{hint}' {why}; the options are: {names}"


def guess_category(con, merchant: str) -> Optional[str]:
    """The category last given to this merchant, so 'Starbucks' needs no category
    said out loud once it has been categorised once."""
    r = con.execute(
        f"SELECT t.category FROM finance_tx t WHERE {fs.LEDGER} AND t.category IS NOT NULL "
        "AND LOWER(TRIM(t.description)) = LOWER(?) ORDER BY t.occurred_on DESC, t.id DESC LIMIT 1",
        ((merchant or "").strip(),)).fetchone()
    return r["category"] if r else None


def captured_today(con, today_iso: str) -> int:
    """Entries captured today, undone ones included: undo must not be a way past the cap."""
    return con.execute(
        "SELECT COUNT(*) FROM finance_tx WHERE source='whatsapp' AND substr(created_at,1,10)=?",
        (today_iso,)).fetchone()[0]


# ── the food library match ────────────────────────────────────────────────
def _words_match(key: str, tokens) -> bool:
    words = key.split()
    return all(any(w == t or (len(t) >= 4 and w.startswith(t)) for w in words) for t in tokens)


def match_food(con, merchant: str, item: str) -> dict:
    """Find the named item in the food library. Only what is already there counts.
    Returns {'status': 'match', 'item': row} | {'status': 'none'} |
    {'status': 'ambiguous', 'names': [...]}."""
    q = nutrition.norm_key(item).split()
    if not q:
        return {"status": "none"}
    m = nutrition.norm_key(merchant).split()
    rows = [nutrition._item_row(r) for r in con.execute("SELECT * FROM food_item")]
    cands = [f for f in rows if _words_match(f["name_key"], q)]
    if len(cands) > 1 and m:                       # "mocha" at Starbucks: the Starbucks one
        brand = [f for f in cands if _words_match(f["name_key"], m)]
        cands = brand or cands
    if len(cands) > 1:
        exact = [f for f in cands if f["name_key"] == nutrition.norm_key(item)]
        cands = exact or cands
    if not cands:
        return {"status": "none"}
    if len(cands) > 1:
        return {"status": "ambiguous", "names": [f["name"] for f in cands[:4]]}
    return {"status": "match", "item": cands[0]}


def pick_portion(food: dict, size: Optional[str]) -> Optional[tuple]:
    """(grams, label, assumed). A named size that the library knows wins; otherwise
    the stored typical portion, flagged as assumed. None when no portion is stored."""
    s = nutrition.norm_key(size or "")
    if s:
        for z in food.get("sizes") or []:
            if nutrition.norm_key(z["label"]) == s:
                return float(z["grams"]), z["label"], False
    if food.get("portion_g"):
        return float(food["portion_g"]), (food.get("portion_label") or "one serving"), True
    return None


def recent_duplicate(con, food_id: int, ts: datetime.datetime, minutes: int = DUP_MIN) -> bool:
    lo = (ts - datetime.timedelta(minutes=minutes)).isoformat(timespec="seconds")
    hi = (ts + datetime.timedelta(minutes=minutes)).isoformat(timespec="seconds")
    return con.execute("SELECT 1 FROM food_log WHERE food_id=? AND ts BETWEEN ? AND ? LIMIT 1",
                       (food_id, lo, hi)).fetchone() is not None


def _match_and_portion(con, *, merchant: Optional[str], item: str, size: Optional[str],
                       ts: datetime.datetime, is_alcoholic) -> dict:
    """The part 'can this named item become a food_log row' — library match, alcohol
    guard, portion lookup, the 30-minute dedupe. Shared by plan_food's purchase-linked
    food bridge and plan_food_direct's bare (no-purchase) food log.
    {'action': 'skip', 'reason': str} | {'action': 'log', 'item': row, 'grams', 'label', 'assumed'}"""
    m = match_food(con, merchant or "", item)
    if m["status"] == "none":
        return {"action": "skip", "reason": f"'{item.strip()}' is not in your food list (add it in the Food tab first)"}
    if m["status"] == "ambiguous":
        return {"action": "skip", "reason": f"'{item.strip()}' matches several foods ({', '.join(m['names'])}), say which one"}
    food = m["item"]
    if (food.get("abv_pct") or 0) > 0 or is_alcoholic(food):
        return {"action": "skip", "reason": "alcoholic drinks are logged by hand"}
    portion = pick_portion(food, size)
    if portion is None:
        return {"action": "skip", "reason": f"{food['name']} has no portion size stored"}
    if recent_duplicate(con, food["id"], ts):
        return {"action": "skip", "reason": f"{food['name']} was already logged in the last {DUP_MIN} minutes"}
    grams, label, assumed = portion
    return {"action": "log", "item": food, "grams": grams, "label": label, "assumed": assumed}


def plan_food(con, *, merchant: str, item: Optional[str], size: Optional[str],
              category: Optional[str], is_today: bool, ts: datetime.datetime,
              is_alcoholic) -> dict:
    """Decide whether this purchase also logs food, without writing anything.
    {'action': 'none'}                        no item was named: nothing to say
    {'action': 'skip', 'reason': str}         an item was named but it is not logged
    {'action': 'log', 'item': row, 'grams', 'label', 'assumed'}"""
    if not (item or "").strip():
        return {"action": "none"}
    if category not in FOOD_CATEGORIES:
        return {"action": "skip", "reason": (
            "only coffee and eating-out purchases are added to food"
            if category else "it has no category, so it is not added to food")}
    if not is_today:
        return {"action": "skip", "reason": "it is not a purchase from today"}
    return _match_and_portion(con, merchant=merchant, item=item, size=size, ts=ts, is_alcoholic=is_alcoholic)


def plan_food_direct(con, *, merchant: Optional[str], item: str, size: Optional[str],
                     ts: datetime.datetime, is_alcoholic) -> dict:
    """Decide whether a bare food log — no purchase, no price — can be written. Same
    library-match/alcohol/portion/dedupe rules as plan_food's food bridge, minus the
    purchase-only gates (category, "is it from today"). item is required.
    {'action': 'skip', 'reason': str} | {'action': 'log', 'item': row, 'grams', 'label', 'assumed'}"""
    if not (item or "").strip():
        return {"action": "skip", "reason": "item is required"}
    return _match_and_portion(con, merchant=merchant, item=item, size=size, ts=ts, is_alcoholic=is_alcoholic)


def captured_food_today(con, today_iso: str) -> int:
    """Bare food captures (no purchase) logged today. Counts current rows via the
    ASSISTANT_FOOD_NOTE marker, so — unlike captured_today's purchase cap, which counts
    finance_tx rows that undo only marks dismissed, never deletes — an undo here does
    free a slot: a bare food log carries no money risk, so this is a spam guard
    against a runaway caller, not a hard cap that must survive undo."""
    return con.execute(
        "SELECT COUNT(*) FROM food_log WHERE note=? AND measured_on=?",
        (ASSISTANT_FOOD_NOTE, today_iso)).fetchone()[0]
