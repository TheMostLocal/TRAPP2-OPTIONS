"""Offline tests for pipeline/fetch_options.py (z114). Run: python tests/test_options.py
No network: yfinance is stubbed; chains are synthesized with Black-76."""
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
ch = {"ticker": "Q", "spot": 100.0, "expiries": [
    {"expiry": "2026-12-19", "dte": 30, "calls": [{"strike": 100.0, "iv": 0.50}, {"strike": 110.0, "iv": 0.45}],
     "puts": [{"strike": 100.0, "iv": 0.58}, {"strike": 90.0, "iv": 0.65}]},
    {"expiry": "2027-03-19", "dte": 120, "calls": [{"strike": 100.0, "iv": 0.40}], "puts": [{"strike": 100.0, "iv": 0.42}]}]}
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
shutil.rmtree(tmp)
print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
