"""
Daily price feed for holdings. Free, no API key, no account.

Sources, in the order they are tried:

  1. Yahoo Finance chart endpoint — one code path for equities, ETFs and crypto.
     Returns the quote currency alongside the price, which is what lets the
     EUR-only rule be enforced instead of assumed.
  2. CoinGecko — crypto only, quoted directly in EUR. A documented public API,
     used as the fallback precisely because Yahoo's endpoint is not.
  3. Frankfurter (ECB rates) — converts a non-EUR quote to EUR.

Why daily rather than live: `finance_holding_price` is keyed (symbol, as_of) with
latest-at-or-before carry-forward, the same grammar as finance_balance. Net worth
is a slow figure and intraday noise carries no information for it — the same
reason the weight panel leads with a 7-day mean. Polling would also burn the
free tiers for nothing. One run touches well under ten URLs.

A failure is never allowed to become a wrong number: a symbol that cannot be
priced is reported and skipped, carry-forward keeps yesterday's price, and
finance_holdings.is_stale() surfaces the age (crypto 2 days, stocks 14). The
balance is only re-derived for accounts whose every holding priced cleanly,
because finance_holdings.sync_balance() refuses partial totals.

    python -m acta.finance.prices            # refresh every derived account
    python -m acta.finance.prices --dry-run  # fetch and print, write nothing
"""
import argparse
import datetime
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from acta import config
from acta.finance import holdings as H

# saas_ideas project uses, so there is one place to rotate it.
ENV_PATH = config.ENV_FILE

# A price parsed out of prose is not the same class of fact as one read from a
# JSON field with its own currency attribute, so it is never allowed to move a
# balance by more than this against the last known price. A genuine 30% daily
# move on an index ETF is far less likely than a bad parse.
TAVILY_MAX_DRIFT = 0.25


def _load_env() -> None:
    if os.environ.get("TAVILY_API_KEY") or not os.path.exists(ENV_PATH):
        return
    with open(ENV_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

# Yahoo rejects the default urllib agent.
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"}
TIMEOUT = 12
# Yahoo rate-limits per IP and returns 429 on even a light burst; once a day
# for a handful of symbols there is no reason to hurry.
PAUSE   = 1.2
RETRIES = 3

# Only the majors — anything else should carry an explicit feed_symbol.
COINGECKO_IDS = {
    "BTC": "bitcoin",   "ETH": "ethereum",  "SOL": "solana",
    "ADA": "cardano",   "XRP": "ripple",    "DOGE": "dogecoin",
    "DOT": "polkadot",  "MATIC": "matic-network", "LTC": "litecoin",
    "LINK": "chainlink", "AVAX": "avalanche-2", "USDC": "usd-coin",
    "USDT": "tether",
}


def _get_json(url: str, *, data: bytes = None, headers: dict = None) -> dict:
    """The single HTTP seam, with exponential backoff on throttling. 429/503 are
    the normal way a free endpoint says "slow down", not a reason to fail the
    whole run. POST bodies go through here too so every source shares the
    retry behaviour — and so tests can stub one function instead of three."""
    delay = 1.5
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(url, data=data,
                                         headers={**UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < RETRIES - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise


# ── sources ───────────────────────────────────────────────────────────────────

# Yahoo throttles per host, so rotating hosts buys a second chance before backoff.
YAHOO_HOSTS = ("https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com")


def yahoo_quote(symbol: str) -> dict:
    """Price + quote currency for one ticker. Raises on anything unusable."""
    path = ("/v8/finance/chart/" + urllib.parse.quote(symbol)
            + "?interval=1d&range=5d")
    errors, data = [], None
    for host in YAHOO_HOSTS:
        try:
            data = _get_json(host + path)
            break
        except Exception as e:                       # noqa: BLE001 - try next host
            errors.append(f"{host.split('//')[1].split('.')[0]}: {type(e).__name__}")
    if data is None:
        raise ValueError("; ".join(errors))
    meta = (data.get("chart") or {}).get("result")
    if not meta:
        raise ValueError(f"no result for {symbol}")
    m = meta[0].get("meta") or {}
    price = m.get("regularMarketPrice")
    cur   = (m.get("currency") or "").upper()
    if price is None or not cur:
        raise ValueError(f"no price/currency for {symbol}")
    if float(price) <= 0:
        raise ValueError(f"implausible price {price} for {symbol}")
    return {"price": float(price), "currency": cur, "source": "yahoo"}


# Twelve Data's free tier allows 8 requests/minute. A nightly run of ~23 symbols
# fits comfortably inside the 800/day cap, so the only thing to respect is the
# per-minute rate — enforced here rather than at the call site so no caller can
# forget it and trip a 429 mid-run.
TD_MIN_INTERVAL = 8.0
_td_last_call = 0.0


def twelvedata_quote(symbol: str) -> dict:
    """Price + currency for one ticker from Twelve Data.

    Preferred over Yahoo because Yahoo returns 429 to this host permanently
    (datacenter IPs are blocked outright, not throttled), which is what silently
    pushed every stock onto web-search parsing. A key turns the same request
    into a structured answer with its own currency field.
    """
    global _td_last_call
    _load_env()
    key = os.environ.get("TWELVEDATA_API_KEY")
    if not key:
        raise ValueError("TWELVEDATA_API_KEY not set")

    wait = TD_MIN_INTERVAL - (time.time() - _td_last_call)
    if wait > 0:
        time.sleep(wait)
    _td_last_call = time.time()

    url = ("https://api.twelvedata.com/quote?symbol="
           + urllib.parse.quote(symbol) + "&apikey=" + urllib.parse.quote(key))
    d = _get_json(url)
    # Twelve Data reports failures in-band with HTTP 200 as often as not.
    if str(d.get("status", "")).lower() == "error":
        raise ValueError(d.get("message") or "twelvedata error")
    price = d.get("close") or d.get("price")
    cur = (d.get("currency") or "").upper()
    if price is None or not cur:
        raise ValueError(f"no price/currency for {symbol}")
    price = float(price)
    if price <= 0:
        raise ValueError(f"implausible price {price} for {symbol}")
    return {"price": price, "currency": cur, "source": "twelvedata"}


def twelvedata_fx(currency: str) -> float:
    """Market rate for `currency`->EUR.

    A dedicated endpoint rather than /quote: forex pairs come back there as an
    OHLC bar, whereas this returns the current rate as a single field with no
    ambiguity about which of open/close is meant.
    """
    global _td_last_call
    _load_env()
    key = os.environ.get("TWELVEDATA_API_KEY")
    if not key:
        raise ValueError("TWELVEDATA_API_KEY not set")

    wait = TD_MIN_INTERVAL - (time.time() - _td_last_call)
    if wait > 0:
        time.sleep(wait)
    _td_last_call = time.time()

    url = ("https://api.twelvedata.com/exchange_rate?symbol="
           + urllib.parse.quote(f"{currency}/EUR")
           + "&apikey=" + urllib.parse.quote(key))
    d = _get_json(url)
    if str(d.get("status", "")).lower() == "error":
        raise ValueError(d.get("message") or "twelvedata fx error")
    rate = d.get("rate")
    if rate is None:
        raise ValueError(f"no rate for {currency}/EUR")
    return float(rate)


def justetf_quote(isin: str) -> dict:
    """EUR price for an ETF by ISIN.

    Keyless and, unlike Yahoo and Stooq, not IP-blocked from this host — which
    makes it the only structured source available for the ETFs that make up most
    of the portfolio's value. It answers in the currency asked for, so no FX
    conversion is applied on top.
    """
    code = (isin or "").strip().upper()
    if not code:
        raise ValueError("isin is required")
    url = ("https://www.justetf.com/api/etfs/" + urllib.parse.quote(code)
           + "/quote?locale=en&currency=EUR")
    d = _get_json(url)
    q = (d.get("latestQuote") or {}).get("raw")
    if q is None:
        raise ValueError(f"no quote for {code}")
    q = float(q)
    if q <= 0:
        raise ValueError(f"implausible price {q} for {code}")
    return {"price": q, "currency": "EUR", "source": "justetf"}


def coingecko_quote(symbol: str) -> dict:
    """EUR price for a major coin, by its CoinGecko id."""
    cid = COINGECKO_IDS.get(symbol.upper())
    if not cid:
        raise ValueError(f"no CoinGecko id known for {symbol}")
    url = ("https://api.coingecko.com/api/v3/simple/price?ids="
           + cid + "&vs_currencies=eur")
    price = (_get_json(url).get(cid) or {}).get("eur")
    if price is None or float(price) <= 0:
        raise ValueError(f"no EUR price for {symbol}")
    return {"price": float(price), "currency": "EUR", "source": "coingecko"}


CURRENCY_SIGNS = {"€": "EUR", "$": "USD", "£": "GBP"}
# Deliberately strict: a decimal point is required. "up 5%" and "2026" are
# integers in the same sentence as the price, and accepting them would let a
# year become a share price.
_PRICE_RE = re.compile(
    r"(?P<sign>[€$£])?\s*(?P<code>EUR|USD|GBP)?\s*"
    r"(?P<num>\d{1,3}(?:,\d{3})+\.\d+|\d+\.\d{2,})"
    r"\s*(?P<after>EUR|USD|GBP)?", re.I)


def parse_price(text: str) -> dict:
    """Pull one price and its currency out of prose.

    Two passes, strongest evidence first:

      1. A number with a currency attached to it ("$313.33", "EUR 168.38").
      2. A number in a block that mentions exactly ONE currency anywhere
         ("...price of the VWCE ETF in EUR is 168.38"). Requiring the currency
         to be unique in the block is what makes this safe — it cannot label a
         EUR figure as USD, because a block naming both never reaches here.

    Percentages are skipped in the second pass: "up 0.29%" sits in the same
    sentence as the price and would otherwise win by being first.
    """
    text = text or ""
    for m in _PRICE_RE.finditer(text):
        cur = (CURRENCY_SIGNS.get(m.group("sign") or "")
               or (m.group("code") or m.group("after") or "").upper())
        if not cur:
            continue
        try:
            val = float(m.group("num").replace(",", ""))
        except ValueError:
            continue
        if val > 0:
            return {"price": val, "currency": cur}

    mentioned = {CURRENCY_SIGNS[s] for s in CURRENCY_SIGNS if s in text}
    mentioned |= {c.upper() for c in re.findall(r"\b(EUR|USD|GBP)\b", text, re.I)}
    if len(mentioned) != 1:
        raise ValueError("no currency-qualified price found in the text")
    cur = mentioned.pop()
    for m in _PRICE_RE.finditer(text):
        if text[m.end("num"):m.end("num") + 1] == "%":
            continue
        try:
            val = float(m.group("num").replace(",", ""))
        except ValueError:
            continue
        if val > 0:
            return {"price": val, "currency": cur}
    raise ValueError("no currency-qualified price found in the text")


def tavily_quote(symbol: str) -> dict:
    """Last-resort price via web search.

    This is a search API, not a market data feed: it answers in prose, the
    figure can be a day or two old, and the same ticker may be quoted on
    several exchanges in different currencies. It exists here so a Yahoo
    outage does not stop the feed entirely — never as the preferred source,
    and every figure it returns is plausibility-gated in refresh() before it
    is allowed to touch a balance.
    """
    _load_env()
    key = os.environ.get("TAVILY_API_KEY")
    if not key:
        raise ValueError("TAVILY_API_KEY not set")
    body = json.dumps({"query": f"{symbol} current share price",
                       "search_depth": "basic", "include_answer": True,
                       "max_results": 4}).encode()
    d = _get_json("https://api.tavily.com/search", data=body,
                  headers={"Content-Type": "application/json",
                           "Authorization": f"Bearer {key}"})
    # The answer field first: it is the summarised current price, where the raw
    # results are as likely to lead with a historical table row.
    for text in [d.get("answer") or ""] + [
            (x.get("content") or "") for x in (d.get("results") or [])]:
        try:
            q = parse_price(text)
        except ValueError:
            continue
        return {**q, "source": "tavily"}
    raise ValueError(f"no parsable price for {symbol}")


_fx_cache: dict = {}


def fx_to_eur(currency: str) -> tuple[float, str]:
    """Rate to convert `currency` into EUR, plus where it came from.

    A market rate first, ECB's daily reference second: the reference rate is
    fixed at 16:00 CET while US markets close at 22:00, so it prices USD
    positions measurably off; a market rate closes most of that gap.
    """
    cur = (currency or "EUR").upper()
    if cur == "EUR":
        return 1.0, "none"
    if cur in _fx_cache:
        return _fx_cache[cur]

    errors = []
    # Twelve Data quotes the pair directly and is reachable from here; Yahoo
    # (the original market-rate source) is 429-blocked, which is why every run
    # since has silently fallen through to the ECB reference below.
    try:
        rate = twelvedata_fx(cur)
        if 0 < rate < 1000:
            _fx_cache[cur] = (rate, "twelvedata")
            return _fx_cache[cur]
        errors.append(f"twelvedata: implausible rate {rate}")
    except Exception as e:                           # noqa: BLE001
        errors.append(f"twelvedata: {type(e).__name__}")

    # Yahoo quotes the pair directly, e.g. USDEUR=X.
    try:
        q = yahoo_quote(f"{cur}EUR=X")
        rate = float(q["price"])
        if 0 < rate < 1000:
            _fx_cache[cur] = (rate, "yahoo")
            return _fx_cache[cur]
        errors.append(f"yahoo: implausible rate {rate}")
    except Exception as e:                           # noqa: BLE001
        errors.append(f"yahoo: {type(e).__name__}")

    try:
        d = _get_json("https://api.frankfurter.app/latest?from="
                      + urllib.parse.quote(cur) + "&to=EUR")
        rate = (d.get("rates") or {}).get("EUR")
        if rate:
            _fx_cache[cur] = (float(rate), "ecb")
            return _fx_cache[cur]
        errors.append("ecb: no rate in response")
    except Exception as e:                           # noqa: BLE001
        errors.append(f"ecb: {type(e).__name__}")
    raise ValueError(f"no {cur}->EUR rate ({'; '.join(errors)})")


def quote_eur(feed_symbol: str, store_symbol: str = None, *,
              kind: str = "stocks", isin: str = None) -> dict:
    """One holding's price in EUR, whatever it takes.

    The symbols are not interchangeable and each source needs its own: Twelve
    Data and Yahoo want the exchange-qualified ticker, CoinGecko wants the bare
    coin ("BTC"), justETF wants the ISIN.

    Source order is by how strong a fact each answer is, not by convenience:

      crypto  CoinGecko (EUR-native, documented) -> Twelve Data -> Yahoo -> web
      ETF     justETF (EUR-native, by ISIN)      -> Twelve Data -> Yahoo -> web
      stock   Twelve Data (keyed, structured)    -> Yahoo -> web

    Yahoo sits second-to-last because it returns 429 to this host; it is kept in
    case that lifts. Web search is last everywhere: a figure parsed out of prose
    has no currency field, so a USD quote can be mislabelled as EUR.
    """
    store_symbol = store_symbol or feed_symbol
    attempts = []
    if kind == "crypto":
        attempts.append((coingecko_quote, store_symbol))
    elif isin:
        attempts.append((justetf_quote, isin))
    attempts.append((twelvedata_quote, feed_symbol))
    attempts.append((yahoo_quote, feed_symbol))
    attempts.append((tavily_quote, feed_symbol))
    errors = []
    for fn, sym in attempts:
        try:
            q = fn(sym)
        except Exception as e:                       # noqa: BLE001 - report, try next
            errors.append(f"{fn.__name__}({sym}): {type(e).__name__}: {e}")
            continue
        rate, fx_src = fx_to_eur(q["currency"])
        return {"price_eur": round(q["price"] * rate, 6),
                "native_price": q["price"], "native_currency": q["currency"],
                "fx_rate": rate, "fx_source": fx_src, "source": q["source"]}
    raise ValueError("; ".join(errors))


# ── refresh ───────────────────────────────────────────────────────────────────

def refresh(con, *, as_of: str = None, dry_run: bool = False,
            allow_unverified: bool = False) -> dict:
    """Price every holding of every derived account, then re-derive balances.

    `allow_unverified` permits a web-search price for a symbol that has no
    earlier price to be checked against. It exists for exactly one situation:
    the first ever run, where there is no history yet and a human is reading the
    output. The drift check still applies wherever a prior price does exist, so
    this loosens the first write only — never an ongoing one.
    """
    as_of = as_of or datetime.date.today().isoformat()
    account_ids = H.holdings_accounts(con)
    priced, failed, synced = {}, {}, []

    # One fetch per distinct feed symbol, not per holding: the same ticker in two
    # accounts is one price, and the price table is keyed on symbol for that
    # reason.
    wanted: dict = {}
    for aid in account_ids:
        kind = con.execute("SELECT kind FROM finance_account WHERE id=?",
                           (aid,)).fetchone()[0]
        for h in H.holdings(con, aid):
            wanted.setdefault(H.feed_symbol(h), (h["symbol"], kind, H.isin(h)))

    for feed_sym, (store_sym, kind, isin) in sorted(wanted.items()):
        try:
            q = quote_eur(feed_sym, store_sym, kind=kind, isin=isin)
        except Exception as e:                       # noqa: BLE001
            failed[store_sym] = str(e)
            continue
        # A prose-parsed price must agree with what we already knew, or it is
        # treated as a bad parse rather than as news. Without a prior price
        # there is nothing to check it against, so it is refused outright —
        # a stale balance beats a confidently wrong one.
        if q["source"] == "tavily":
            prior = H.price_at(con, store_sym, as_of)
            if prior is None and not allow_unverified:
                failed[store_sym] = ("web-search price refused: no earlier price "
                                     "to sanity-check it against")
                continue
            if prior is None:
                q = {**q, "unverified": True}
                priced[store_sym] = q
                if not dry_run:
                    H.set_price(con, store_sym, q["price_eur"], as_of,
                                native_price=q["native_price"],
                                native_currency=q["native_currency"],
                                fx_rate=q["fx_rate"], fx_source=q.get("fx_source"),
                                source=q["source"])
                    con.commit()
                time.sleep(PAUSE)
                continue
            drift = abs(q["price_eur"] - prior[0]) / prior[0] if prior[0] else 1.0
            if drift > TAVILY_MAX_DRIFT:
                failed[store_sym] = (
                    f"web-search price refused: {q['price_eur']:.4f} is "
                    f"{drift:.0%} from the last known {prior[0]:.4f} "
                    f"({prior[1]}) — looks like a bad parse")
                continue
        priced[store_sym] = q
        if not dry_run:
            H.set_price(con, store_sym, q["price_eur"], as_of,
                        native_price=q["native_price"],
                        native_currency=q["native_currency"],
                        fx_rate=q["fx_rate"], fx_source=q.get("fx_source"),
                        source=q["source"])
            # Commit per symbol, not once at the end: prices are independent, and this
            # loop runs for minutes under Twelve Data's pacing, so one end-of-loop commit
            # would hold the write lock long enough to collide with ingest ("database is locked").
            con.commit()
        time.sleep(PAUSE)

    if not dry_run:
        for aid in account_ids:
            # sync_balance refuses a partial total, so an account with any
            # unpriced holding keeps its previous balance rather than dropping.
            try:
                r = H.sync_balance(con, aid, as_of)
            except ValueError as e:
                failed[f"account:{aid}"] = str(e)
                continue
            if r:
                synced.append(r)
        con.commit()

    return {"as_of": as_of, "priced": priced, "failed": failed,
            "synced": synced, "dry_run": dry_run}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and print without writing")
    ap.add_argument("--trust-web", action="store_true",
                    help="accept a web-search price for a symbol with no prior "
                         "price (first run only — read the output before trusting it)")
    ap.add_argument("--as-of", default=None, help="YYYY-MM-DD (default today)")
    ap.add_argument("--db", default=config.ACTA_DB)
    args = ap.parse_args()

    # timeout=30 (vs sqlite3's 5s default) as defense-in-depth alongside the
    # per-symbol commits in refresh() — see the comment there for the actual
    # fix; this just widens the window for whatever brief overlap remains.
    con = sqlite3.connect(args.db, timeout=30)
    con.row_factory = sqlite3.Row
    H.ensure(con)
    try:
        out = refresh(con, as_of=args.as_of, dry_run=args.dry_run,
                      allow_unverified=args.trust_web)
    finally:
        con.close()

    print(f"as_of {out['as_of']}" + ("  (dry run)" if out["dry_run"] else ""))
    if not out["priced"] and not out["failed"]:
        print("  no holdings to price")
    for sym, q in sorted(out["priced"].items()):
        native = ("" if q["native_currency"] == "EUR" else
                  f"   <- {q['native_price']} {q['native_currency']}"
                  f" @ {q['fx_rate']} [{q.get('fx_source','?')}]")
        flag = "  UNVERIFIED" if q.get("unverified") else ""
        print(f"  {sym:10} EUR {q['price_eur']:>12,.4f}  [{q['source']}]{native}{flag}")
    for acct in out["synced"]:
        print(f"  account {acct['account_id']} -> EUR {acct['amount_eur']:,.2f}")
    for sym, err in sorted(out["failed"].items()):
        print(f"  FAILED {sym}: {err}", file=sys.stderr)
    return 1 if out["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
