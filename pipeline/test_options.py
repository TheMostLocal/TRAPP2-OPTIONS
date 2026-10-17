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
print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
