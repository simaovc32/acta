"""Unit tests for finance.prices with the network stubbed out.

The live endpoints are an integration concern — they rate-limit, and a test that
depends on them fails for reasons that have nothing to do with this code. What
must be provably right is the logic around them: which source is asked for which
symbol, how a non-EUR quote is converted, and above all that a failed fetch can
never produce a wrong balance.
"""

from acta.finance import holdings as H
from acta.finance import prices as P
from conftest import check
from ledger_fixture import synthetic_ledger


def net_worth(con):
    total = 0.0
    for (aid,) in con.execute("SELECT id FROM finance_account WHERE active=1"):
        r = con.execute("SELECT amount_eur FROM finance_balance WHERE account_id=? "
                        "ORDER BY as_of DESC LIMIT 1", (aid,)).fetchone()
        if r:
            total += r[0]
    return round(total, 2)


class Stub:
    """Records every URL asked for and answers from a fixture table."""

    def __init__(self, responses, fail_hosts=()):
        self.responses = responses
        self.fail_hosts = fail_hosts
        self.calls = []

    def __call__(self, url, *, data=None, headers=None):
        # Accepts the POST signature too: every source shares one seam, so an
        # unstubbed POST must raise here rather than reach the live API.
        self.calls.append(url)
        for host in self.fail_hosts:
            if host in url:
                raise OSError(f"stubbed failure for {host}")
        for frag, payload in self.responses.items():
            if frag in url:
                if isinstance(payload, Exception):
                    raise payload
                return payload
        raise OSError(f"no stub for {url}")


def yahoo_payload(symbol, price, currency):
    return {"chart": {"result": [{"meta": {"symbol": symbol, "currency": currency,
                                           "regularMarketPrice": price}}]}}


def install(stub):
    P._get_json = stub          # the seam this test exists to use
    P._fx_cache.clear()
    return stub


def test_price_feed(monkeypatch):
    # the web-search fallback is only tried when a key exists; the network itself is stubbed
    monkeypatch.setenv("TAVILY_API_KEY", "stub")
    monkeypatch.delenv("TWELVEDATA_API_KEY", raising=False)
    orig_get, orig_pause = P._get_json, P.PAUSE
    P.PAUSE = 0                                     # no sleeping in tests
    try:
        # ── EUR stock: no FX, one call ────────────────────────────────────────
        install(Stub({"VWCE.DE": yahoo_payload("VWCE.DE", 168.38, "EUR")}))
        q = P.quote_eur("VWCE.DE", "VWCE", kind="stocks")
        check("EUR quote needs no conversion",
              q["price_eur"] == 168.38 and q["fx_rate"] == 1.0, str(q))
        check("EUR quote records its source", q["source"] == "yahoo", str(q))

        # ── USD stock: converted via ECB, native figures kept ────────────────
        install(Stub({"AAPL": yahoo_payload("AAPL", 313.33, "USD"),
                      "USDEUR": yahoo_payload("USDEUR=X", 0.86693, "EUR"),
                      "frankfurter": {"rates": {"EUR": 0.86693}}}))
        q = P.quote_eur("AAPL", "AAPL", kind="stocks")
        check("USD quote is converted to EUR",
              q["price_eur"] == round(313.33 * 0.86693, 6), str(q["price_eur"]))
        check("native price and currency are kept for audit",
              q["native_price"] == 313.33 and q["native_currency"] == "USD", str(q))
        check("the rate used is recorded", q["fx_rate"] == 0.86693, str(q))

        # FX is looked up once per currency per run, not once per symbol.
        stub = install(Stub({"AAPL": yahoo_payload("AAPL", 313.33, "USD"),
                             "MSFT": yahoo_payload("MSFT", 500.0, "USD"),
                             "USDEUR": yahoo_payload("USDEUR=X", 0.86693, "EUR"),
                      "frankfurter": {"rates": {"EUR": 0.86693}}}))
        P.quote_eur("AAPL", "AAPL")
        P.quote_eur("MSFT", "MSFT")
        check("the FX rate is fetched once and cached",
              sum("USDEUR" in u for u in stub.calls) == 1,
              str([u for u in stub.calls if "USDEUR" in u]))

        # ── host rotation ────────────────────────────────────────────────────
        stub = install(Stub({"AAPL": yahoo_payload("AAPL", 313.33, "USD"),
                             "USDEUR": yahoo_payload("USDEUR=X", 0.86693, "EUR"),
                      "frankfurter": {"rates": {"EUR": 0.86693}}},
                            fail_hosts=("query1",)))
        q = P.quote_eur("AAPL", "AAPL", kind="stocks")
        check("query1 failing falls through to query2", q["price_eur"] > 0)
        check("both hosts were tried in order",
              any("query1" in u for u in stub.calls)
              and any("query2" in u for u in stub.calls), str(stub.calls))

        # ── crypto asks CoinGecko first, with the bare ticker ─────────────────
        stub = install(Stub({"coingecko": {"bitcoin": {"eur": 56629.0}}}))
        q = P.quote_eur("BTC-EUR", "BTC", kind="crypto")
        check("crypto is priced by CoinGecko first",
              q["source"] == "coingecko" and q["price_eur"] == 56629.0, str(q))
        check("CoinGecko is asked for the bare ticker, not the feed symbol",
              "bitcoin" in stub.calls[0] and "BTC-EUR" not in stub.calls[0],
              stub.calls[0])
        check("Yahoo is not called when CoinGecko answers",
              not any("yahoo" in u for u in stub.calls), str(stub.calls))

        # This is the bug the first live run found: the fallback was handed
        # "BTC-EUR", which is not a CoinGecko id, so crypto could not fall back.
        stub = install(Stub({"BTC-EUR": yahoo_payload("BTC-EUR", 56413.11, "EUR")}))
        q = P.quote_eur("BTC-EUR", "BTC", kind="crypto")
        check("crypto falls back to Yahoo using the feed symbol",
              q["source"] == "yahoo" and q["price_eur"] == 56413.11, str(q))

        # ── unusable quotes are refused, not stored ──────────────────────────
        # Asserted on yahoo_quote directly: quote_eur would legitimately fall
        # through to the next source, which is a different behaviour to test.
        install(Stub({"ZERO": yahoo_payload("ZERO", 0, "EUR")}))
        try:
            P.yahoo_quote("ZERO")
            check("a zero price is rejected", False, "accepted")
        except ValueError:
            check("a zero price is rejected", True)
        install(Stub({"NOCUR": yahoo_payload("NOCUR", 10, "")}))
        try:
            P.yahoo_quote("NOCUR")
            check("a quote with no currency is rejected", False, "accepted")
        except ValueError:
            check("a quote with no currency is rejected", True)

        # An unusable structured quote must fall through, not abort the symbol.
        stub = install(Stub({"ZERO": yahoo_payload("ZERO", 0, "EUR"),
                             "tavily": {"answer": "ZERO trades at 42.50 EUR"}}))
        q = P.quote_eur("ZERO", "ZERO", kind="stocks")
        check("an unusable Yahoo quote falls through to web search",
              q["source"] == "tavily" and q["price_eur"] == 42.50, str(q))

        # Nothing may reach the network unstubbed — the POST path included.
        install(Stub({"ONLYYAHOO": yahoo_payload("ONLYYAHOO", 5.0, "EUR")}))
        stub_calls_before = len(P._get_json.calls)      # noqa: SLF001
        try:
            P.quote_eur("UNKNOWN-SYM", "UNKNOWN-SYM", kind="stocks")
            check("an unstubbed source raises instead of calling out", False,
                  "it returned a quote")
        except ValueError:
            check("an unstubbed source raises instead of calling out",
                  len(P._get_json.calls) > stub_calls_before)   # noqa: SLF001

        # ── prose parsing (Tavily) ───────────────────────────────────────────
        # Every string here is real Tavily output captured from a live call.
        for label, text, price, cur in [
            ("currency named away from the number",
             "The current share price of the VWCE ETF in EUR is 168.38 as of "
             "August 7, 2026. The fund has a 0.32% increase for the day.", 168.38, "EUR"),
            ("currency in a header, number below",
             "XETRA - Delayed Quote • EUR\n\n# Vanguard FTSE All-World\n\n168.38  +0.54",
             168.38, "EUR"),
            ("sign adjacent to the number",
             "As of today, Apple AAPL stock price is $313.33, up by $0.92 or 0.29%.",
             313.33, "USD"),
            ("thousands separator",
             "BTC is trading at €56,630.00 today", 56630.0, "EUR"),
            ("a percentage does not win by being first",
             "AAPL rose 0.29% today; in USD the price is 313.33", 313.33, "USD"),
        ]:
            q = P.parse_price(text)
            check(f"parses: {label}",
                  q["currency"] == cur and abs(q["price"] - price) < 0.01, str(q))

        for label, text in [
            ("a bare percentage", "AAPL is up 5% today"),
            ("a year", "The stock rose in 2026"),
            ("share volume", "Volume was 49,155,600 shares"),
            ("two currencies in one block",
             "Listed in EUR on XETRA and USD on Nasdaq, around 168.38"),
        ]:
            try:
                P.parse_price(text)
                check(f"refuses: {label}", False, "parsed something")
            except ValueError:
                check(f"refuses: {label}", True)

        # ── resynthetic_ledger(): the invariant that matters ────────────────────────────
        con = synthetic_ledger()
        before = net_worth(con)
        stocks = con.execute("SELECT id FROM finance_account WHERE kind='stocks'").fetchone()[0]
        crypto  = con.execute("SELECT id FROM finance_account WHERE kind='crypto'").fetchone()[0]
        old_xtb = con.execute("SELECT amount_eur FROM finance_balance WHERE account_id=? "
                              "ORDER BY as_of DESC LIMIT 1", (stocks,)).fetchone()[0]
        H.upsert_holding(con, stocks, "VWCE", 40, feed_symbol="VWCE.DE")
        H.upsert_holding(con, stocks, "AAPL", 5)
        H.upsert_holding(con, crypto, "BTC", 0.03, feed_symbol="BTC-EUR")
        con.commit()

        # AAPL cannot be priced -> XTB must keep its old balance, untouched.
        install(Stub({"VWCE.DE": yahoo_payload("VWCE.DE", 168.38, "EUR"),
                      "coingecko": {"bitcoin": {"eur": 56629.0}}}))
        out = P.refresh(con)
        check("a partly-priced account is reported as failed",
              any(k.startswith("account:") for k in out["failed"]), str(out["failed"]))
        check("the partly-priced account keeps its entered balance",
              con.execute("SELECT amount_eur FROM finance_balance WHERE account_id=? "
                          "ORDER BY as_of DESC LIMIT 1", (stocks,)).fetchone()[0] == old_xtb)
        check("the fully-priced account still updates",
              round(0.03 * 56629.0, 2) ==
              con.execute("SELECT amount_eur FROM finance_balance WHERE account_id=? "
                          "ORDER BY as_of DESC LIMIT 1", (crypto,)).fetchone()[0])
        check("one account failing does not block the other",
              net_worth(con) != before and "VWCE" in out["priced"])

        # A price that did arrive is still stored, so tomorrow carries it forward.
        check("prices that succeeded are persisted despite the failure",
              H.price_at(con, "VWCE") is not None)

        # ── dry run writes nothing ───────────────────────────────────────────
        nw_now = net_worth(con)
        install(Stub({"VWCE.DE": yahoo_payload("VWCE.DE", 999.0, "EUR"),
                      "AAPL": yahoo_payload("AAPL", 999.0, "EUR"),
                      "coingecko": {"bitcoin": {"eur": 99999.0}}}))
        out = P.refresh(con, dry_run=True)
        check("dry run reports prices", len(out["priced"]) == 3, str(out["priced"].keys()))
        check("dry run writes no price rows",
              H.price_at(con, "VWCE")[0] == 168.38, str(H.price_at(con, "VWCE")))
        check("dry run does not move net worth", net_worth(con) == nw_now)

        # ── FX prefers a market rate over the ECB reference ──────────────────
        # Measured against the broker, the ECB reference rate put every USD
        # position ~0.75% high. Market rate first, reference as fallback.
        stub = install(Stub({"AAPL": yahoo_payload("AAPL", 100.0, "USD"),
                             "USDEUR": yahoo_payload("USDEUR=X", 0.8605, "EUR"),
                             "frankfurter": {"rates": {"EUR": 0.86693}}}))
        q = P.quote_eur("AAPL", "AAPL", kind="stocks")
        check("a market FX rate is preferred over the ECB reference",
              q["fx_rate"] == 0.8605 and q["fx_source"] == "yahoo", str(q))
        check("the market rate is the one applied", q["price_eur"] == 86.05, str(q))

        # If the market rate is unavailable the reference rate still works.
        stub = install(Stub({"AAPL": yahoo_payload("AAPL", 100.0, "USD"),
                             "frankfurter": {"rates": {"EUR": 0.86693}}}))
        q = P.quote_eur("AAPL", "AAPL", kind="stocks")
        check("falls back to the ECB reference rate",
              q["fx_source"] == "ecb" and q["fx_rate"] == 0.86693, str(q))

        # An absurd rate is a wrong ticker, not news.
        stub = install(Stub({"AAPL": yahoo_payload("AAPL", 100.0, "USD"),
                             "USDEUR": yahoo_payload("USDEUR=X", 5000.0, "EUR"),
                             "frankfurter": {"rates": {"EUR": 0.86693}}}))
        q = P.quote_eur("AAPL", "AAPL", kind="stocks")
        check("an implausible FX rate is rejected in favour of the reference",
              q["fx_source"] == "ecb", str(q))

        # EUR quotes must not be converted at all.
        install(Stub({"VWCE.DE": yahoo_payload("VWCE.DE", 168.38, "EUR")}))
        q = P.quote_eur("VWCE.DE", "VWCE", kind="stocks")
        check("a EUR quote needs no FX lookup at all",
              q["fx_rate"] == 1.0 and q["fx_source"] == "none", str(q))

        # ── the Tavily fallback is gated on plausibility ─────────────────────
        con2 = synthetic_ledger()
        stocks2 = con2.execute("SELECT id FROM finance_account WHERE kind='stocks'"
                            ).fetchone()[0]
        H.upsert_holding(con2, stocks2, "VWCE", 10, feed_symbol="VWCE.DE")
        con2.commit()
        tavily_body = {"answer": "The VWCE ETF is 168.38 EUR as of today."}

        # No earlier price to check a parsed figure against -> refused outright.
        install(Stub({"tavily": tavily_body}, fail_hosts=("query1", "query2")))
        out = P.refresh(con2)
        check("a prose price with no prior is refused",
              "VWCE" in out["failed"] and "no earlier price" in out["failed"]["VWCE"],
              str(out["failed"]))
        check("nothing was stored for the refused symbol",
              H.price_at(con2, "VWCE") is None)

        # Give it a prior price, then the same figure is accepted.
        H.set_price(con2, "VWCE", 165.00, source="manual")
        con2.commit()
        install(Stub({"tavily": tavily_body}, fail_hosts=("query1", "query2")))
        out = P.refresh(con2)
        check("a prose price close to the last known is accepted",
              out["priced"].get("VWCE", {}).get("source") == "tavily",
              str(out["failed"]))
        check("the accepted prose price is marked as web-search sourced",
              con2.execute("SELECT source FROM finance_holding_price WHERE symbol='VWCE' "
                           "ORDER BY as_of DESC, created_at DESC LIMIT 1").fetchone()[0]
              == "tavily")

        # A wildly different figure reads as a bad parse, not as news.
        H.set_price(con2, "VWCE", 20.00, source="manual")
        con2.commit()
        install(Stub({"tavily": tavily_body}, fail_hosts=("query1", "query2")))
        out = P.refresh(con2)
        check("a prose price far from the last known is refused",
              "VWCE" in out["failed"] and "bad parse" in out["failed"]["VWCE"],
              str(out["failed"]))
        check("the implausible figure never reached the price table",
              H.price_at(con2, "VWCE")[0] == 20.00,
              str(H.price_at(con2, "VWCE")))

        # Structured sources are never gated — only prose is.
        install(Stub({"VWCE.DE": yahoo_payload("VWCE.DE", 168.38, "EUR")}))
        out = P.refresh(con2)
        check("a structured price is not plausibility-gated",
              out["priced"].get("VWCE", {}).get("source") == "yahoo"
              and H.price_at(con2, "VWCE")[0] == 168.38,
              str(out))
        con2.close()

        # ── each distinct symbol is fetched once, however many accounts ───────
        H.upsert_holding(con, crypto, "ETH", 1.0)
        con.commit()
        stub = install(Stub({"VWCE.DE": yahoo_payload("VWCE.DE", 168.38, "EUR"),
                             "AAPL": yahoo_payload("AAPL", 313.33, "EUR"),
                             "coingecko": {"bitcoin": {"eur": 1.0},
                                           "ethereum": {"eur": 2.0}}}))
        P.refresh(con, dry_run=True)
        check("no symbol is fetched twice in one run",
              len(stub.calls) == len(set(stub.calls)), str(stub.calls))
        con.close()
    finally:
        P._get_json, P.PAUSE = orig_get, orig_pause
