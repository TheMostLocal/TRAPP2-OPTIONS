"""Offline tests for pipeline/fetch_options.py (z114). Run: python tests/test_options.py
No network: yfinance is stubbed; chains are synthesized with Black-76.
z208: section 9 - no quotes, no volatility (Yahoo's placeholders, the write guard, the broad window, the surface repair,
IV rank in IV points, the breadth without quote-less chains)."""
import sys, os, json, math, types, tempfile, shutil
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pipeline"))
sys.modules.setdefault("yfinance", types.ModuleType("yfinance"))
import fetch_options as fo

ok = fail = 0
def T(name, cond):
    global ok, fail
    print(("PASS " if cond else "FAIL ") + name); ok += bool(cond); fail += not cond

def synth_chain(S=100.0, r=0.04, q=0.02, sig=0.25, dte=182, skew=0.0, ticker="SYN"):
    """Two-sided quotes priced with Black-76 on F = S e^{(r-q)T}; optional linear skew."""
    T_ = dte / 365.0
    F = S * math.exp((r - q) * T_)
    calls, puts = [], []
    for K in range(80, 121, 5):
        v = sig + skew * (1 - K / S)
        for typ, arr in (("call", calls), ("put", puts)):
            p = fo._b76(typ, F, K, T_, r, v)
            sd = 1 if typ == "call" else -1
            d1 = (math.log(F / K) + 0.5 * v * v * T_) / (v * math.sqrt(T_))
            delta = math.exp(-q * T_) * (fo._norm_cdf(d1) if typ == "call" else fo._norm_cdf(d1) - 1)
            arr.append({"strike": float(K), "bid": round(p * 0.99, 4), "ask": round(p * 1.01, 4), "mid": round(p, 4),
                        "iv": v + (0.1 if typ == "call" else -0.05), "delta": round(delta, 4), "volume": 10, "openInterest": 100})
    return {"ticker": ticker, "spot": S, "asOf": "2026-10-14T20:00:00Z", "riskFreeRate": r,
            "expiries": [{"expiry": "2027-04-14", "dte": dte, "calls": calls, "puts": puts}]}, F

# 1. parity IV recovers the true vol and the dividend carry
d, F = synth_chain()
fo.parity_iv(d)
e = d["expiries"][0]
ivs = [o["ivMid"] for side in ("calls", "puts") for o in e[side] if o.get("ivMid")]
T(f"forward from parity {e['forward']:.3f} ≈ true {F:.3f}", abs(e["forward"] - F) / F < 0.002)
T(f"implied carry {e['impliedCarry']:.4f} ≈ q 0.02", abs(e["impliedCarry"] - 0.02) < 0.004)
T(f"mid IVs recover 25% (range {min(ivs):.4f}–{max(ivs):.4f}) despite Yahoo-style iv errors", all(abs(v - 0.25) < 0.006 for v in ivs))
gap = max(abs(c["ivMid"] - p["ivMid"]) for c, p in zip(e["calls"], e["puts"]) if c.get("ivMid") and p.get("ivMid"))
T(f"put/call IV agree at every strike (max gap {gap*100:.2f} pts)", gap < 0.006)

# 2. summary analytics: skew sign, 30d fields, quote quality, unusual activity
d2, _ = synth_chain(dte=30, skew=0.5)
fo.parity_iv(d2)
d2["expiries"][0]["calls"][4].update(volume=5000, openInterest=300)
sm = fo.summarize_chain(d2)
T(f"25-delta skew positive when puts are richer (skew25={sm.get('skew25')})", (sm.get("skew25") or 0) > 0)
T(f"30-day ATM IV and expected move present ({sm.get('atmIv30')}, {sm.get('expectedMove30Pct')}%)", sm.get("atmIv30") and sm.get("expectedMove30Pct"))
T(f"quote quality counts only clean two-sided quotes ({sm.get('quoteQualityPct')}%: far-OTM strikes priced ~0 have no bid)", 85 <= (sm.get("quoteQualityPct") or 0) < 100)
T(f"unusual activity finds the 5,000-lot (vol/OI {sm['unusual'][0]['volOi'] if sm.get('unusual') else None})", sm.get("unusual") and sm["unusual"][0]["volume"] == 5000)

# 3. retries: transient errors retried then raised; no options -> None
fo.time.sleep = lambda s: None
calls_n = {"n": 0}
def flaky(t, r):
    calls_n["n"] += 1
    if calls_n["n"] < 3: raise fo.FetchError("rate limited")
    return {"ok": True}
fo.fetch_ticker = flaky
T("transient error retried until it succeeds (3 tries)", fo.fetch_with_retry("X", 0.04) == {"ok": True} and calls_n["n"] == 3)
fo.fetch_ticker = lambda t, r: (_ for _ in ()).throw(fo.FetchError("down"))
try:
    fo.fetch_with_retry("X", 0.04); T("persistent error raises FetchError", False)
except fo.FetchError:
    T("persistent error raises FetchError", True)

# 4. main(): errors KEEP the previous chain, no-options removes it, oldest first, report written
tmp = tempfile.mkdtemp()
fo.DATA_DIR, fo.OPTIONS_DIR, fo.SURF_DIR = tmp, os.path.join(tmp, "options"), os.path.join(tmp, "surfaces")
os.makedirs(fo.OPTIONS_DIR)
for t, asof in (("OLD", "2026-10-01T00:00:00Z"), ("ERR", "2026-10-10T00:00:00Z"), ("GONE", "2026-10-12T00:00:00Z")):
    json.dump({"ticker": t, "asOf": asof, "spot": 1, "expiries": []}, open(os.path.join(fo.OPTIONS_DIR, f"{t}.json"), "w"))
order = []
def fake(t, r, tries=3):
    order.append(t)
    if t == "ERR": raise fo.FetchError("HTTP 429")
    if t == "GONE": return None
    dd, _ = synth_chain(ticker=t); return fo.parity_iv(dd)
fo.fetch_with_retry = fake
fo._read_universe = lambda: ["GONE", "ERR", "NEW", "OLD"]
fo._risk_free_rate = lambda: 0.04
os.environ.pop("OPTIONS_TICKERS", None)
fo.main()
rep = json.load(open(os.path.join(fo.OPTIONS_DIR, "run_report.json")))
T(f"never-fetched and oldest first: {order}", order[:2] == ["NEW", "OLD"])
T("error keeps the previous chain file (ERR still on file)", os.path.exists(os.path.join(fo.OPTIONS_DIR, "ERR.json")) and rep["keptStale"][0]["ticker"] == "ERR")
T("no listed options removes the file (GONE)", not os.path.exists(os.path.join(fo.OPTIONS_DIR, "GONE.json")) and rep["noOptions"] == ["GONE"])
summ = json.load(open(os.path.join(fo.OPTIONS_DIR, "summary.json")))["tickers"]
T("summary has the new fields for written chains", "skew25" in (summ.get("NEW") or {}) and "ivRank" in summ["NEW"])
shutil.rmtree(tmp)

# 5. IV rank from surface history
tmp = tempfile.mkdtemp(); fo.SURF_DIR = tmp
snaps = [{"date": f"2026-0{1 + i // 28}-{1 + i % 28:02d}", "iv": [[None] * 9 for _ in fo.SURF_TENORS]} for i in range(30)]
for i, sn in enumerate(snaps): sn["iv"][fo.SURF_TENORS.index(30)][fo.SURF_M.index(1.0)] = 20 + i * 0.5      # 20 .. 34.5
json.dump({"tenorsDays": fo.SURF_TENORS, "moneyness": fo.SURF_M, "snapshots": snaps}, open(os.path.join(tmp, "Z.json"), "w"))
rk = fo.iv_rank("Z", 30.0)
T(f"IV rank of 30 in a 20–34.5 history = {rk.get('rank')} (expect 69.0), percentile {rk.get('percentile')}", rk.get("rank") == 69.0)
T("fewer than 20 days → no rank", "rank" not in fo.iv_rank("missing", 30.0) if fo.iv_rank("missing", 30.0) else True)
shutil.rmtree(tmp)

# 6. z196 - lite ladder for the long tail; full ladder unchanged
import datetime as _dt
today = _dt.date(2026, 11, 19)
exps = [(today + _dt.timedelta(days=d)).isoformat() for d in (1, 8, 15, 22, 29, 36, 43, 50)]
d0 = _dt.date(2026, 12, 1)
for k in range(26):                                   # third Fridays for two years
    m = (d0.month - 1 + k) % 12 + 1; y = d0.year + (d0.month - 1 + k) // 12
    f = _dt.date(y, m, 15); f += _dt.timedelta(days=(4 - f.weekday()) % 7)
    exps.append(f.isoformat())
exps = sorted(set(exps))
lite = fo._select_expiries(exps, today, lite=True)
dtes = [(_dt.date.fromisoformat(e) - today).days for e in lite]
T(f"lite ladder: <= {fo.TAIL_MAX_EXPIRIES} expiries {dtes}", 0 < len(lite) <= fo.TAIL_MAX_EXPIRIES)
T("lite ladder keeps the nearest expiry and one near 30 days", dtes[0] == 1 and any(14 <= x <= 45 and abs(x - 30) <= 7 for x in dtes))
T("lite ladder has a ~1-year LEAP", any(300 <= x <= 430 for x in dtes))
T("lite ladder: at most two monthlies in 45-120 days", sum(1 for x in dtes if 45 < x <= 120) <= 2)
full = fo._select_expiries(exps, today)
fd = [(_dt.date.fromisoformat(e) - today).days for e in full]
T(f"full ladder unchanged: <= {fo.MAX_EXPIRIES}, furthest kept ({fd[-1]} d)", len(full) <= fo.MAX_EXPIRIES and full[-1] == exps[-1])

# 7. z196 - app signal = the app's extractOptionsSignal arithmetic
_q = {"bid": 4.0, "ask": 4.2}                                     # Yahoo rows carry their quote (z208: no quote, no IV)
ch = {"ticker": "Q", "spot": 100.0, "expiries": [
    {"expiry": "2026-12-19", "dte": 30, "calls": [dict(_q, strike=100.0, iv=0.50), dict(_q, strike=110.0, iv=0.45)],
     "puts": [dict(_q, strike=100.0, iv=0.58), dict(_q, strike=90.0, iv=0.65)]},
    {"expiry": "2027-03-19", "dte": 120, "calls": [dict(_q, strike=100.0, iv=0.40)], "puts": [dict(_q, strike=100.0, iv=0.42)]}]}
a = fo.app_signal(ch)
T(f"app signal iv 0.54 / skew 0.08 / term 0.14 / ivSignal -0.51: {a}", a and a["iv"] == 0.54 and a["skew"] == 0.08 and a["termSlope"] == 0.14 and a["ivSignal"] == -0.51)
mr = fo.market_read([{"ticker": "SPY", "iv": 0.30, "skew": 0.02, "termSlope": 0.0, "ivSignal": 0, "isIndex": True},
                     {"ticker": "AAA", "iv": 0.40, "skew": 0.02, "termSlope": 0.0, "ivSignal": 0, "isIndex": False}])
T(f"market read: index IV 30% = stressed, trend -0.375 ({mr['volRegime']}, {mr['marketTrend']})", mr["volRegime"] == "stressed" and mr["marketTrend"] == -0.375 and mr["indexIV"] == 0.3)

# 8. z196 - main(): meta files never listed as tickers; no-options cache; broad skips fresh chains; state file
tmp = tempfile.mkdtemp()
fo.DATA_DIR, fo.OPTIONS_DIR, fo.SURF_DIR = tmp, os.path.join(tmp, "options"), os.path.join(tmp, "surfaces")
os.makedirs(fo.OPTIONS_DIR)
nowz = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
json.dump({"ticker": "FRESH", "asOf": nowz, "spot": 100, "expiries": []}, open(os.path.join(fo.OPTIONS_DIR, "FRESH.json"), "w"))
json.dump({"ticker": "DEADX", "asOf": "2026-01-01T00:00:00Z", "spot": 1, "expiries": []}, open(os.path.join(fo.OPTIONS_DIR, "DEADX.json"), "w"))
recentz = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
json.dump({"ticker": "HICCUP", "asOf": recentz, "spot": 1, "expiries": []}, open(os.path.join(fo.OPTIONS_DIR, "HICCUP.json"), "w"))
json.dump({"ticker": "ORPHAN", "asOf": "2026-09-01T00:00:00Z", "spot": 1, "expiries": []}, open(os.path.join(fo.OPTIONS_DIR, "ORPHAN.json"), "w"))
for meta in ("run_report.json", "_state.json", "_sectors.json"):
    json.dump({}, open(os.path.join(fo.OPTIONS_DIR, meta), "w"))
calls2 = []
def fake2(t, r, tries=3, lite=False):
    calls2.append((t, lite))
    if t.startswith("PAD"): return None
    if t == "NOOPT": return None
    if t in ("NOPX", "DEADX", "HICCUP"): raise fo.FetchError("no spot price")
    dd, _ = synth_chain(ticker=t, dte=30); dd["ladder"] = "lite" if lite else "full"
    dd["asOf"] = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"); return fo.parity_iv(dd)
fo.fetch_with_retry = fake2
fo._read_universe = lambda: ["FRESH", "TAIL", "NOOPT", "NOPX", "DEADX", "HICCUP", "SPY"] + [f"PAD{i}" for i in range(200)]
_pad_skip = {f"PAD{i}" for i in range(200)}
fo.TIER_OF.clear(); fo.TIER_OF.update({"TAIL": "lite"})
os.environ["OPTIONS_TIER"] = "broad"
os.environ["OPTIONS_BROAD_ANYTIME"] = "1"                         # z208: these broad passes run at any test time
fo.main()
man = json.load(open(os.path.join(fo.OPTIONS_DIR, "manifest.json")))
T(f"manifest lists tickers only: {man['tickers']}", set(man["tickers"]) == {"FRESH", "TAIL", "SPY", "HICCUP"})
T("an orphan chain (in no list, weeks old) is pruned", not os.path.exists(os.path.join(fo.OPTIONS_DIR, "ORPHAN.json")))
T("broad pass skips a chain fetched this evening (FRESH not fetched)", "FRESH" not in [c[0] for c in calls2])
T("tail name fetched with the lite ladder", ("TAIL", True) in calls2 and ("SPY", False) in calls2)
nocache = json.load(open(os.path.join(fo.OPTIONS_DIR, "_nooptions.json")))["tickers"]
T(f"no-options cache: NOOPT (no listed options), NOPX (never had a chain, no price), DEADX (weeks-old chain, no price): {sorted(set(nocache) - _pad_skip)}",
  set(nocache) - _pad_skip == {"NOOPT", "NOPX", "DEADX"} and not os.path.exists(os.path.join(fo.OPTIONS_DIR, "DEADX.json")))
rep2 = json.load(open(os.path.join(fo.OPTIONS_DIR, "run_report.json")))
T("a recent chain and a price hiccup keeps the chain (HICCUP kept, not cached)", os.path.exists(os.path.join(fo.OPTIONS_DIR, "HICCUP.json")) and any(k["ticker"] == "HICCUP" for k in rep2["keptStale"]))
st = json.load(open(os.path.join(fo.OPTIONS_DIR, "_state.json")))["broad"]
T(f"_state.json: tonight's broad pass complete ({st})", st["complete"] is True and st["date"] == _dt.datetime.now(fo.ET).date().isoformat())
mk = json.load(open(os.path.join(fo.OPTIONS_DIR, "market.json")))
T("market.json: app read (index-led), breadth, one history row for today", mk.get("appSignal") and mk["appSignal"]["indexes"] == ["SPY"] and mk.get("breadth") and len(mk["history"]) == 1)
sg = json.load(open(os.path.join(fo.OPTIONS_DIR, "signals.json")))
T("signals.json: compact per-ticker app signal", sg["fields"][:4] == ["iv", "skew", "termSlope", "ivSignal"] and len(sg["tickers"]["TAIL"]) == 6)
calls2.clear()
fo.main()
rep3 = json.load(open(os.path.join(fo.OPTIONS_DIR, "run_report.json")))
T(f"second pass: cached names skipped ({rep3['skippedNoOptions']}), fresh names skipped ({rep3['skippedFresh']})",
  rep3["skippedNoOptions"] == 203 and "NOOPT" not in [c[0] for c in calls2] and rep3["skippedFresh"] >= 3)
T("market.json history stays one row per day", len(json.load(open(os.path.join(fo.OPTIONS_DIR, "market.json")))["history"]) == 1)
fo.TIME_BUDGET_SEC = -1
os.remove(os.path.join(fo.OPTIONS_DIR, "_nooptions.json"))
fo.main()
st2 = json.load(open(os.path.join(fo.OPTIONS_DIR, "_state.json")))["broad"]
T("time budget hit -> tonight's broad pass marked incomplete (the loop runs another)", st2["complete"] is False and st2["notReached"] > 0)
fo.TIME_BUDGET_SEC = 2400
# Yahoo refusing most requests -> stop early, incomplete
fo.fetch_with_retry = lambda t, r, tries=3, lite=False: (_ for _ in ()).throw(fo.FetchError("HTTP 429"))
fo._read_universe = lambda: [f"BLK{i}" for i in range(150)]
os.remove(os.path.join(fo.OPTIONS_DIR, "_state.json"))
fo.main()
rep4 = json.load(open(os.path.join(fo.OPTIONS_DIR, "run_report.json")))
st3 = json.load(open(os.path.join(fo.OPTIONS_DIR, "_state.json")))["broad"]
T(f"error storm: stopped after {rep4['attempted']} attempts ({rep4.get('aborted')}), pass incomplete", rep4["attempted"] == 60 and rep4.get("aborted") and st3["complete"] is False)
# lite names keep fewer surface snapshots
fo.SURF_KEEP_TAIL = 3
snaps = [{"date": f"2026-10-0{i}", "spot": 1, "iv": [[None] * 9 for _ in fo.SURF_TENORS]} for i in range(1, 6)]
os.makedirs(fo.SURF_DIR, exist_ok=True)
json.dump({"ticker": "TAIL", "snapshots": snaps}, open(os.path.join(fo.SURF_DIR, "TAIL.json"), "w"))
dd, _ = synth_chain(ticker="TAIL", dte=30); dd["ladder"] = "lite"; dd["expiries"].append(dict(synth_chain(dte=90)[0]["expiries"][0], dte=90))
fo.parity_iv(dd); fo.update_surface_history("TAIL", dd)
T("lite-ladder surface history capped at SURF_KEEP_TAIL", len(json.load(open(os.path.join(fo.SURF_DIR, "TAIL.json")))["snapshots"]) == 3)
os.environ.pop("OPTIONS_TIER", None)
os.environ.pop("OPTIONS_BROAD_ANYTIME", None)
shutil.rmtree(tmp)

# 9. z208 - no quotes, no volatility
yiv = fo.yahoo_iv
T("Yahoo IV kept with a quote (0.25, bid/ask)", yiv({"iv": 0.25, "bid": 1.0, "ask": 1.1}) == 0.25)
T("... and with a one-sided quote (ask only)", yiv({"iv": 0.31, "bid": 0, "ask": 0.05}) == 0.31)
T("no quote at all -> no IV (0.25, bid = ask = 0)", yiv({"iv": 0.25, "bid": 0, "ask": 0}) is None)
T("placeholders -> no IV: 1/64 raw, 0.0156 stored, 1e-05, 0.0625", all(yiv({"iv": v, "bid": 1, "ask": 1.1}) is None for v in (0.015625, 0.0156, 1e-05, 0.0625, 0.03125)))
T("a real low IV survives (HYG-like 0.0576)", yiv({"iv": 0.0576, "bid": 0.1, "ask": 0.12}) == 0.0576)
row = fo._row("call", {"strike": 100, "lastPrice": 2.5, "bid": 0, "ask": 0, "impliedVolatility": 0.015625, "volume": 0, "openInterest": 500}, 100.0, 30 / 365, 0.04)
T(f"_row: a no-quote contract stores no IV and no Greeks ({row['iv']}, {row['delta']})", row["iv"] is None and row["delta"] is None)
row2 = fo._row("call", {"strike": 100, "lastPrice": 2.5, "bid": 2.4, "ask": 2.6, "impliedVolatility": 0.2471, "volume": 5, "openInterest": 500}, 100.0, 30 / 365, 0.04)
T(f"_row: a quoted contract keeps its IV and delta ({row2['iv']}, {row2['delta']})", row2["iv"] == 0.2471 and row2["delta"] is not None)

def dead_chain(ticker="DEAD", asof="2026-11-24T08:05:48Z"):
    """The 0327 ET 11-24 shape: open interest and last prices, bid = ask = 0, Yahoo's placeholder IVs, no ivMid."""
    dd, _ = synth_chain(ticker=ticker, dte=30)
    for o in dd["expiries"][0]["calls"] + dd["expiries"][0]["puts"]:
        o.update(bid=0.0, ask=0.0, iv=0.0156, ivMid=None)
    dd["asOf"] = asof
    return dd
dc = dead_chain()
sm = fo.summarize_chain(dc)
T(f"a quote-less chain: noQuotes, no ATM IV, no skew ({sm.get('noQuotes')}, {sm.get('atmIv30')}, {sm.get('skew25')})", sm.get("noQuotes") and sm.get("atmIv30") is None and sm.get("skew25") is None)
T("... and no app signal (it was +0.5 'calm IV' for 1,435 names)", fo.app_signal(dc) is None)
T("... and no surface grid", fo.constant_maturity_surface(dc) is None)

tmp = tempfile.mkdtemp()
fo.DATA_DIR, fo.OPTIONS_DIR, fo.SURF_DIR = tmp, os.path.join(tmp, "options"), os.path.join(tmp, "surfaces")
os.makedirs(fo.OPTIONS_DIR); os.makedirs(fo.SURF_DIR)
# IV rank in IV points: a fraction is converted; summarize passes the constant-maturity 30-day ATM
snaps = [{"date": f"2026-0{1 + i // 28}-{1 + i % 28:02d}", "iv": [[None] * 9 for _ in fo.SURF_TENORS]} for i in range(30)]
for i, sn in enumerate(snaps): sn["iv"][fo.SURF_TENORS.index(30)][fo.SURF_M.index(1.0)] = 20 + i * 0.5      # 20 .. 34.5
json.dump({"tenorsDays": fo.SURF_TENORS, "moneyness": fo.SURF_M, "snapshots": snaps}, open(os.path.join(fo.SURF_DIR, "Z.json"), "w"))
T("IV rank: a fraction (0.30) is read as 30 IV points -> 69.0 (it was -6,734)", fo.iv_rank("Z", 0.30).get("rank") == 69.0 and fo.iv_rank("Z", 30.0).get("rank") == 69.0)
good, _ = synth_chain(ticker="Z", dte=30, sig=0.27)
good["expiries"].append(dict(synth_chain(dte=90, sig=0.27)[0]["expiries"][0], dte=90))
fo.parity_iv(good)
cm = fo.constant_maturity_surface(good)[fo.SURF_TENORS.index(30)][fo.SURF_M.index(1.0)]
rk = fo.summarize_chain(good)["ivRank"]
T(f"summary ranks today's constant-maturity 30-day ATM ({cm} pts) -> rank {rk.get('rank')}", rk.get("rank") == round((cm - 20) / 14.5 * 100, 1))

# the write guard: a good chain on file is not replaced by a quote-less one
nowu = _dt.datetime(2026, 11, 24, 8, 27, tzinfo=_dt.timezone.utc)
g2, _ = synth_chain(ticker="KEEP", dte=30); g2["asOf"] = "2026-11-23T21:10:00Z"; fo.parity_iv(g2)
json.dump(g2, open(os.path.join(fo.OPTIONS_DIR, "KEEP.json"), "w"))
why = fo.keep_previous("KEEP", dead_chain("KEEP"), nowu)
T(f"keep_previous: {why}", why and why.startswith("no live quotes (0% two-sided at 03:27 ET)") and "kept" in why)
T("keep_previous: a live chain is always written", fo.keep_previous("KEEP", g2, nowu) is None)
g3 = dict(g2, asOf="2026-11-10T21:10:00Z"); json.dump(g3, open(os.path.join(fo.OPTIONS_DIR, "OLDG.json"), "w"))
T("keep_previous: a chain on file older than 5 days is not kept", fo.keep_previous("OLDG", dead_chain("OLDG"), nowu) is None)
json.dump(dead_chain("BAD"), open(os.path.join(fo.OPTIONS_DIR, "BAD.json"), "w"))
T("keep_previous: nothing better on file -> written", fo.keep_previous("BAD", dead_chain("BAD"), nowu) is None)
calls9 = []
def fake9(t, r, tries=3, lite=False):
    calls9.append(t); return dead_chain(t, asof=_dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
fo.fetch_with_retry = fake9
fo._read_universe = lambda: ["KEEP", "NEWD"]
g2["asOf"] = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"); json.dump(g2, open(os.path.join(fo.OPTIONS_DIR, "KEEP.json"), "w"))
before = open(os.path.join(fo.OPTIONS_DIR, "KEEP.json")).read()
fo.main()
rep9 = json.load(open(os.path.join(fo.OPTIONS_DIR, "run_report.json")))
T("main: KEEP's live chain stays on file; reported", open(os.path.join(fo.OPTIONS_DIR, "KEEP.json")).read() == before and any(k["ticker"] == "KEEP" and "no live quotes" in k["error"] for k in rep9["keptStale"]))
nd = json.load(open(os.path.join(fo.OPTIONS_DIR, "NEWD.json")))
T("main: NEWD (nothing on file) written, flagged noQuotes, no surface snapshot", nd.get("noQuotes") is True and not os.path.exists(os.path.join(fo.SURF_DIR, "NEWD.json")) and rep9.get("noQuotes") == 2)
mk9 = json.load(open(os.path.join(fo.OPTIONS_DIR, "market.json")))
T(f"market.json: quote-less chains counted and kept out of the breadth (noQuotes {mk9.get('noQuotes')}, breadth n {(mk9.get('breadth') or {}).get('n')})",
  mk9.get("noQuotes") >= 1 and (mk9.get("breadth") or {}).get("n") == 1)

# the broad window: weekdays 1600-2000 ET; refused outside it without the override
T("broad window: Tue 17:00 ET yes, Tue 03:27 ET no, Sat 17:00 ET no",
  fo.broad_window_ok(_dt.datetime(2026, 11, 24, 22, 0, tzinfo=_dt.timezone.utc)) and not fo.broad_window_ok(nowu)
  and not fo.broad_window_ok(_dt.datetime(2026, 11, 28, 22, 0, tzinfo=_dt.timezone.utc)))
real_win = fo.broad_window_ok
fo.broad_window_ok = lambda now: False
os.environ["OPTIONS_TIER"] = "broad"
calls9.clear()
st_path = os.path.join(fo.OPTIONS_DIR, "_state.json")
if os.path.exists(st_path): os.remove(st_path)
fo.main()
T("a broad pass outside the window fetches nothing and leaves tonight's state alone", calls9 == [] and not os.path.exists(st_path))
fo.broad_window_ok = real_win
os.environ.pop("OPTIONS_TIER", None)

# the surface repair
def sn(d, lvl):
    g = [[None] * 9 for _ in fo.SURF_TENORS]
    for k in range(len(fo.SURF_TENORS)): g[k][fo.SURF_M.index(1.0)] = lvl
    return {"date": d, "spot": 1, "iv": g}
hist = [sn("2026-11-19", 49.7), sn("2026-11-20", 55.0), sn("2026-11-23", 50.2), sn("2026-11-24", 1.87), sn("2026-11-25", 56.0)]
kept, dropped = fo.clean_snapshots(hist)
T(f"clean: AA's 11-24 snapshot (1.87 vs ~50) dropped: {dropped}", [d for d, _ in dropped] == ["2026-11-24"] and len(kept) == 4)
kept, dropped = fo.clean_snapshots([sn("2026-11-10", 30.0), sn("2026-11-11", 6.0), sn("2026-11-12", 31.0)])
T("clean: a one-day collapse that reverses is dropped (any date)", [d for d, _ in dropped] == ["2026-11-11"])
takeover = [sn("2026-11-10", 40.0), sn("2026-11-11", 41.0), sn("2026-11-12", 4.0), sn("2026-11-13", 3.8), sn("2026-11-14", 3.9)]
T("clean: a collapse that persists (a cash takeover) is kept", fo.clean_snapshots(takeover)[1] == [])
T("clean: the latest snapshot is never dropped as a dip", fo.clean_snapshots([sn("2026-11-20", 40.0), sn("2026-11-23", 41.0), sn("2026-11-25", 3.0)])[1] == [])
T("clean: a real low-vol name on the bad day stays (HYG 6.1 vs ~6.5)", fo.clean_snapshots([sn("2026-11-20", 7.2), sn("2026-11-23", 6.97), sn("2026-11-24", 6.09)])[1] == [])
json.dump({"ticker": "AA", "snapshots": hist}, open(os.path.join(fo.SURF_DIR, "AA.json"), "w"))
fixed = fo.sweep_surfaces()
T(f"sweep: the file repaired once ({fixed})", fixed == {"2026-11-24": 1} and len(json.load(open(os.path.join(fo.SURF_DIR, "AA.json")))["snapshots"]) == 4 and fo.sweep_surfaces() == {})
# market.json history: a row from a quote-less pass is dropped
json.dump({"history": [{"d": "2026-11-23", "br": 0.5147, "ivr": 0.0, "n": 1976}, {"d": "2026-11-24", "br": 0.0156, "n": 1971}]}, open(os.path.join(fo.OPTIONS_DIR, "market.json"), "w"))
_, _, mk10 = fo.build_outputs(fo.chain_files())
T(f"market.json history: the 11-24 row (median 30-day IV 1.6 %) dropped: {[h['d'] for h in mk10['history']]}", "2026-11-24" not in [h["d"] for h in mk10["history"]] and "2026-11-23" in [h["d"] for h in mk10["history"]])
T("market.json history: an old row's IV-rank share (computed in the wrong units) blanked; today's row marked v2",
  all(h.get("ivr") is None for h in mk10["history"] if h["d"] == "2026-11-23") and mk10["history"][-1].get("v") == 2)
sg10 = json.load(open(os.path.join(fo.OPTIONS_DIR, "signals.json"))) if os.path.exists(os.path.join(fo.OPTIONS_DIR, "signals.json")) else {"tickers": {}}
_, sig10, _ = fo.build_outputs(fo.chain_files())
T(f"signals.json: no row for a quote-less chain (NEWD), the live one kept (KEEP): {sorted(sig10['tickers'])}", "NEWD" not in sig10["tickers"] and "KEEP" in sig10["tickers"])
shutil.rmtree(tmp)
print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
