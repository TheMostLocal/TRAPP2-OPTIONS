#!/usr/bin/env python3
"""
Fetch live options chains and compute Greeks — TRAPP2-OPTIONS (dedicated repo, z90).

Moved out of the TRAPP2 equity book so chains for 150+ tickers, refreshed
several times a day, don't bloat the equity repo. Same file layout as before
(data/options/<TICKER>.json + manifest.json), plus summary.json.

Source: yfinance (free) for the chain (strike, bid/ask/last, IV, volume, OI).
Greeks: Black-Scholes, computed here in stdlib math (no extra deps) from the
        contract's IV + the spot + a risk-free rate.

Writes one file per ticker:  data/options/<TICKER>.json
    {
      ticker, spot, asOf, riskFreeRate,
      expiries: [
        { expiry: "2026-06-26", dte: 6, isFriday: true,
          calls: [ {strike, last, bid, ask, mid, volume, openInterest, iv,
                    inTheMoney, delta, gamma, theta, vega, rho, intrinsic,
                    extrinsic, breakeven}, ... ],
          puts:  [ ... ] }
      ]
    }

Design notes:
  • Only the nearest N expiries are pulled (most liquidity + relevance), and only
    strikes within ±STRIKE_PCT of spot (near the money) — keeps files small and
    the run inside the Actions time budget.
  • Both calls AND puts.
  • Expired contracts are never written (dte < 0 skipped), so the app naturally
    drops them. The bot keeps whatever it traded in its own data.
  • Stdlib + yfinance only.

Env (optional):
    OPTIONS_TICKERS   comma list to override the universe (else reads
                      data/options_universe.txt, else data/tickers.txt)
    RISK_FREE_RATE    annualized decimal (else read from data/macro/DGS3MO, else 0.04)
    MAX_EXPIRIES      nearest expiries per ticker (default 4)
    STRIKE_PCT        ± strike window around spot (default 0.25 = ±25%)
    TIME_BUDGET_SEC   stop starting new tickers after this many seconds (default 2400)
"""
import json
import math
import os
import sys
import time
import datetime

try:
    import yfinance as yf
except ImportError:
    print("yfinance required: pip install yfinance", file=sys.stderr)
    sys.exit(1)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
OPTIONS_DIR = os.path.join(DATA_DIR, "options")

# Expiry ladder: instead of just "nearest N", we pick a SPREAD of expiries across
# the term structure so the app/bot can trade short-dated, monthly, AND LEAPS.
#   • NEAR weeklies:   the next few expiries (≤ NEAR_MAX_DTE days)
#   • MONTHLIES:       standard monthly expiries out to MONTHLY_MAX_DTE days
#   • LEAPS:           the longest-dated expiries (≥ LEAP_MIN_DTE days), incl. the
#                      furthest available (often 1-2 years out)
# MAX_EXPIRIES caps the TOTAL pulled per ticker so the job stays within budget.
MAX_EXPIRIES     = int(os.environ.get("MAX_EXPIRIES", "10"))      # total cap per ticker
NEAR_COUNT       = int(os.environ.get("NEAR_COUNT", "3"))         # nearest weeklies to always include
NEAR_MAX_DTE     = int(os.environ.get("NEAR_MAX_DTE", "45"))      # "near" window
MONTHLY_MAX_DTE  = int(os.environ.get("MONTHLY_MAX_DTE", "120"))  # monthlies out to ~4 months
LEAP_MIN_DTE     = int(os.environ.get("LEAP_MIN_DTE", "180"))     # LEAPS = 6 months+
LEAP_COUNT       = int(os.environ.get("LEAP_COUNT", "3"))         # how many long-dated expiries to include
STRIKE_PCT = float(os.environ.get("STRIKE_PCT", "0.25"))
TIME_BUDGET_SEC = int(os.environ.get("TIME_BUDGET_SEC", "2400"))
MIN_OI = 1  # skip totally illiquid contracts (open interest 0 AND volume 0)


# ---------- Black-Scholes Greeks (stdlib) ----------
def _norm_cdf(x):
    # standard normal CDF via erf (stdlib)
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def greeks(opt_type, S, K, T, r, sigma):
    """Return (delta, gamma, theta_per_day, vega_per_1pct, rho_per_1pct).
    opt_type: 'call' | 'put'. T in years, sigma = implied vol (decimal)."""
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return (None, None, None, None, None)
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    pdf_d1 = _norm_pdf(d1)
    gamma = pdf_d1 / (S * sigma * sqrtT)
    vega = S * pdf_d1 * sqrtT / 100.0  # per 1% vol move
    if opt_type == "call":
        delta = _norm_cdf(d1)
        theta = (-(S * pdf_d1 * sigma) / (2 * sqrtT) - r * K * math.exp(-r * T) * _norm_cdf(d2)) / 365.0
        rho = K * T * math.exp(-r * T) * _norm_cdf(d2) / 100.0
    else:
        delta = _norm_cdf(d1) - 1.0
        theta = (-(S * pdf_d1 * sigma) / (2 * sqrtT) + r * K * math.exp(-r * T) * _norm_cdf(-d2)) / 365.0
        rho = -K * T * math.exp(-r * T) * _norm_cdf(-d2) / 100.0
    rnd = lambda v: round(v, 4) if v is not None and math.isfinite(v) else None
    return (rnd(delta), rnd(gamma), rnd(theta), rnd(vega), rnd(rho))


# ---------- helpers ----------
# ---- universe (TRAPP2-OPTIONS, z90) ------------------------------------------
# OPTIONS_TICKERS (dispatch input)          -> exactly those tickers
# OPTIONS_TIER=core  -> seed list + requests + portfolio holdings + open bot positions
# OPTIONS_TIER=broad -> core + the BROAD_TOP_N largest US-listed equities / ETFs
#                      across the TRAPP2 / TRAPP2-2 / TRAPP2-3 books
OWNER = os.environ.get("GITHUB_REPOSITORY_OWNER") or "TheMostLocal"
RAW = f"https://raw.githubusercontent.com/{OWNER}"
BROAD_TOP_N = int(os.environ.get("BROAD_TOP_N", "150"))


def _get_json(url):
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "TRAPP2-OPTIONS"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"  (universe source unavailable: {url.split('/main/')[-1]} — {type(e).__name__})")
        return None


def _read_list(fname):
    p = os.path.join(DATA_DIR, fname)
    if not os.path.exists(p):
        return []
    with open(p) as f:
        return [ln.strip().upper() for ln in f if ln.strip() and not ln.lstrip().startswith("#")]


def _optionable(t):
    return t and not t.startswith("^") and not t.endswith(("=F", "=X")) and not t.endswith("-USD") and "." not in t


def _read_universe():
    env = os.environ.get("OPTIONS_TICKERS", "").strip()
    if env:
        return [t.strip().upper() for t in env.replace(" ", ",").split(",") if t.strip()]
    seen, out = set(), []

    def add(ts, why):
        n = 0
        for t in ts:
            t = (t or "").strip().upper()
            if _optionable(t) and t not in seen:
                seen.add(t); out.append(t); n += 1
        print(f"  universe +{n:3d} from {why}")

    add(_read_list("options_universe.txt"), "options_universe.txt")
    add(_read_list("requests.txt"), "requests.txt")
    port = _get_json(f"{RAW}/TRAPP2-PORT/main/data/portfolio_data.json") or {}
    add([p.get("ticker") for p in (port.get("portfolio") or [])
         if str(p.get("position", "")).lower() in ("long", "short", "buy to open", "sell to open")], "portfolio holdings")
    bot = _get_json(f"{RAW}/TRAPP2-BOT/main/data/bot_training_data.json") or {}
    add([t.get("ticker") for t in (bot.get("trades") or []) if t.get("status") == "open"], "open bot positions")
    if os.environ.get("OPTIONS_TIER", "core").lower() == "broad":
        rows = []
        for book in ("TRAPP2", "TRAPP2-2", "TRAPP2-3"):
            for r in (_get_json(f"{RAW}/{book}/main/data/master.json") or []):
                if str(r.get("currency") or "USD").upper() != "USD":
                    continue
                if str(r.get("asset_class") or "").lower() not in ("equity", "etf"):
                    continue
                try:
                    mc = float(r.get("marketcap") or 0)
                except (TypeError, ValueError):
                    mc = 0.0
                rows.append((mc, str(r.get("ticker") or "").upper()))
        rows.sort(reverse=True)
        add([t for _, t in rows[:BROAD_TOP_N]], f"top {BROAD_TOP_N} US equities/ETFs by market cap")
    return out


def _risk_free_rate():
    env = os.environ.get("RISK_FREE_RATE", "").strip()
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    # Try the 3-month Treasury from the macro pipeline (DGS3MO is a percent).
    p = os.path.join(DATA_DIR, "macro", "DGS3MO.json")
    try:
        with open(p) as f:
            d = json.load(f)
        obs = d.get("observations") or d.get("data") or []
        if obs:
            last = obs[-1]
            val = last.get("value") if isinstance(last, dict) else last[1]
            return float(val) / 100.0
    except Exception:
        pass
    return 0.04


def _is_friday(date_str):
    try:
        return datetime.date.fromisoformat(date_str).weekday() == 4
    except Exception:
        return False


def _safe(v, default=None):
    try:
        if v is None:
            return default
        f = float(v)
        return f if math.isfinite(f) else default
    except Exception:
        return default


def _third_friday(d):
    """True if date d (datetime.date) is the third Friday of its month — the
    standard 'monthly' option expiry."""
    return d.weekday() == 4 and 15 <= d.day <= 21


def _select_expiries(all_exps, today):
    """From every available expiry, pick a spread across the term structure:
    nearest weeklies + monthlies + LEAPS. Returns an ordered, de-duplicated list
    of expiry strings, capped at MAX_EXPIRIES. Falls back gracefully when a name
    has few expiries (e.g. only weeklies)."""
    dated = []
    for e in all_exps:
        try:
            d = datetime.date.fromisoformat(e)
        except Exception:
            continue
        dte = (d - today).days
        if dte < 0:
            continue
        dated.append((e, d, dte))
    if not dated:
        return []
    dated.sort(key=lambda x: x[2])

    picked = []

    # (a) Nearest weeklies — always include the front few for short-dated trades.
    for e, d, dte in dated:
        if dte <= NEAR_MAX_DTE and len(picked) < NEAR_COUNT:
            picked.append(e)

    # (b) Monthlies (third-Friday) out to MONTHLY_MAX_DTE — the liquid sweet spot.
    for e, d, dte in dated:
        if e in picked:
            continue
        if _third_friday(d) and NEAR_MAX_DTE < dte <= MONTHLY_MAX_DTE:
            picked.append(e)

    # (c) Bridge: if no monthly landed in the 45-120d window (some names lack
    #     standard monthlies), grab the nearest expiry in that band anyway.
    if not any((NEAR_MAX_DTE < (datetime.date.fromisoformat(e) - today).days <= MONTHLY_MAX_DTE) for e in picked):
        for e, d, dte in dated:
            if NEAR_MAX_DTE < dte <= MONTHLY_MAX_DTE and e not in picked:
                picked.append(e); break

    # (d) LEAPS — the longest-dated expiries (6 months+). Prefer third-Friday
    #     monthlies (true LEAPS), then fall back to the furthest available.
    leaps = [(e, d, dte) for (e, d, dte) in dated if dte >= LEAP_MIN_DTE]
    leap_monthlies = [t for t in leaps if _third_friday(t[1])]
    leap_pool = leap_monthlies if leap_monthlies else leaps
    # Spread them out: take evenly across the long end (e.g. ~6mo, ~1yr, ~2yr).
    if leap_pool:
        idxs = set()
        n = len(leap_pool)
        for k in range(min(LEAP_COUNT, n)):
            idxs.add(round(k * (n - 1) / max(LEAP_COUNT - 1, 1)))
        for i in sorted(idxs):
            e = leap_pool[i][0]
            if e not in picked:
                picked.append(e)

    # Always include the single furthest-dated expiry (true long LEAP) if room.
    furthest = dated[-1][0]
    if furthest not in picked:
        picked.append(furthest)

    # De-dupe, keep chronological order, cap at MAX_EXPIRIES.
    seen = set()
    ordered = []
    for e, d, dte in dated:
        if e in picked and e not in seen:
            seen.add(e); ordered.append(e)
    return ordered[:MAX_EXPIRIES]


def _row(opt_type, rec, S, T, r):
    strike = _safe(rec.get("strike"))
    if strike is None or strike <= 0:
        return None
    last = _safe(rec.get("lastPrice"), 0.0)
    bid = _safe(rec.get("bid"), 0.0)
    ask = _safe(rec.get("ask"), 0.0)
    mid = round((bid + ask) / 2.0, 4) if (bid or ask) else last
    iv = _safe(rec.get("impliedVolatility"), 0.0)
    vol = int(_safe(rec.get("volume"), 0) or 0)
    oi = int(_safe(rec.get("openInterest"), 0) or 0)
    if vol == 0 and oi < MIN_OI:
        return None  # totally illiquid — skip
    itm = bool(rec.get("inTheMoney", False))
    d, g, th, ve, rho = greeks(opt_type, S, strike, T, r, iv) if iv > 0 else (None, None, None, None, None)
    if opt_type == "call":
        intrinsic = max(0.0, S - strike)
        breakeven = round(strike + (mid or last), 2)
    else:
        intrinsic = max(0.0, strike - S)
        breakeven = round(strike - (mid or last), 2)
    price = mid if mid else last
    extrinsic = round(max(0.0, (price or 0) - intrinsic), 4)
    return {
        "strike": round(strike, 2),
        "last": round(last, 4),
        "bid": round(bid, 4),
        "ask": round(ask, 4),
        "mid": mid,
        "volume": vol,
        "openInterest": oi,
        "iv": round(iv, 4),
        "inTheMoney": itm,
        "delta": d, "gamma": g, "theta": th, "vega": ve, "rho": rho,
        "intrinsic": round(intrinsic, 2),
        "extrinsic": extrinsic,
        "breakeven": breakeven,
    }


# ---- parity-consistent implied volatility (z107) -----------------------------
# Yahoo's impliedVolatility for a put and a call at the SAME strike often
# disagree badly on long expiries (e.g. AAPL 2Y: puts ~22%, calls ~36%) because
# it ignores dividends/borrow and uses stale "last" prices. Fix, per expiry:
#   1. imply the FORWARD from put-call parity on near-the-money strikes that
#      have clean two-sided quotes:  F = K + e^{rT} (C_mid - P_mid)
#      (median across strikes) - this prices in dividends and borrow cost;
#   2. solve each option's IV from its MID price with Black-76 on that forward.
# By construction a call and a put at the same strike then give (nearly) the
# same IV. Stored as ivMid (Yahoo's iv is kept for reference) + per-expiry
# forward and implied carry. Quotes with no bid/ask or a spread > 50% of mid
# get no ivMid.
def _b76(opt_type, F, K, T, r, sig):
    if sig <= 0 or T <= 0:
        return max(0.0, (F - K) if opt_type == "call" else (K - F)) * math.exp(-r * T)
    sq = sig * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sig * sig * T) / sq
    d2 = d1 - sq
    df = math.exp(-r * T)
    if opt_type == "call":
        return df * (F * _norm_cdf(d1) - K * _norm_cdf(d2))
    return df * (K * _norm_cdf(-d2) - F * _norm_cdf(-d1))


def _b76_iv(opt_type, price, F, K, T, r):
    """Bisection on [0.005, 5.0]; None if the price is outside no-arbitrage bounds."""
    lo_p, hi_p = _b76(opt_type, F, K, T, r, 0.005), _b76(opt_type, F, K, T, r, 5.0)
    if not (lo_p <= price <= hi_p):
        return None
    lo, hi = 0.005, 5.0
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if _b76(opt_type, F, K, T, r, mid) < price:
            lo = mid
        else:
            hi = mid
    return round(0.5 * (lo + hi), 4)


def _clean_mid(o):
    b, a = o.get("bid") or 0, o.get("ask") or 0
    if b <= 0 or a <= 0 or a < b:
        return None
    m = 0.5 * (a + b)
    return m if (a - b) <= 0.5 * m else None


def parity_iv(d):
    """Adds forward / impliedCarry per expiry and ivMid per option (in place)."""
    S, r = d.get("spot"), d.get("riskFreeRate") or 0.0
    if not S:
        return d
    for e in d.get("expiries") or []:
        T = max(e.get("dte") or 0, 0.5) / 365.0
        calls = {o["strike"]: o for o in e.get("calls") or []}
        puts = {o["strike"]: o for o in e.get("puts") or []}
        fs = []
        for K in set(calls) & set(puts):
            if abs(K / S - 1) > 0.05:
                continue
            cm, pm = _clean_mid(calls[K]), _clean_mid(puts[K])
            if cm is not None and pm is not None:
                fs.append(K + math.exp(r * T) * (cm - pm))
        if fs:
            fs.sort()
            F = fs[len(fs) // 2]
            e["forwardSource"] = f"parity ({len(fs)} strike{'s' if len(fs) != 1 else ''})"
        else:
            F = S * math.exp(r * T)
            e["forwardSource"] = "spot x e^rT (no clean pairs)"
        e["forward"] = round(F, 4)
        e["impliedCarry"] = round(r - math.log(F / S) / T, 4) if F > 0 else None   # ≈ dividend yield + borrow
        for side, typ in (("calls", "call"), ("puts", "put")):
            for o in e.get(side) or []:
                m = _clean_mid(o)
                o["ivMid"] = _b76_iv(typ, m, F, o["strike"], T, r) if m is not None else None
    return d


def _iv_of(o):
    """Preferred IV for surfaces: parity-consistent mid IV, else Yahoo's."""
    v = o.get("ivMid")
    return v if v else o.get("iv")


class FetchError(Exception):
    """Transient / data error - keep the previous file instead of deleting it."""


def fetch_with_retry(ticker, r, tries=3):
    """z114: Yahoo rate-limits and hiccups. Retry with backoff; a ticker that
    genuinely has no options returns None; repeated errors raise FetchError."""
    last = None
    for k in range(tries):
        try:
            return fetch_ticker(ticker, r)
        except FetchError as e:
            last = e
        except Exception as e:                # yfinance internals
            last = FetchError(f"{type(e).__name__}: {str(e)[:80]}")
        time.sleep(1.5 * (2 ** k))
    raise last


def fetch_ticker(ticker, r):
    tk = yf.Ticker(ticker)
    # Spot price.
    spot = None
    try:
        fi = getattr(tk, "fast_info", None)
        if fi:
            spot = _safe(fi.get("last_price") if hasattr(fi, "get") else getattr(fi, "last_price", None))
    except Exception:
        pass
    if spot is None:
        try:
            h = tk.history(period="1d")
            if not h.empty:
                spot = float(h["Close"].iloc[-1])
        except Exception:
            pass
    if not spot or spot <= 0:
        raise FetchError("no spot price")
    try:
        expiries = list(tk.options or [])
    except Exception as e:
        raise FetchError(f"expiry list failed: {type(e).__name__}")
    if not expiries:
        return None                      # genuinely no listed options
    today = datetime.date.today()
    chosen = _select_expiries(expiries, today)
    out_expiries = []
    for exp in chosen:
        try:
            dte = (datetime.date.fromisoformat(exp) - today).days
        except Exception:
            continue
        if dte < 0:
            continue  # expired — skip
        T = max(dte, 0) / 365.0
        if T <= 0:
            T = 0.5 / 365.0  # same-day: tiny but nonzero so Greeks don't div0
        try:
            chain = tk.option_chain(exp)
        except Exception:
            continue
        lo, hi = spot * (1 - STRIKE_PCT), spot * (1 + STRIKE_PCT)
        calls, puts = [], []
        for _, rec in chain.calls.iterrows():
            s = _safe(rec.get("strike"))
            if s is None or s < lo or s > hi:
                continue
            row = _row("call", rec, spot, T, r)
            if row:
                calls.append(row)
        for _, rec in chain.puts.iterrows():
            s = _safe(rec.get("strike"))
            if s is None or s < lo or s > hi:
                continue
            row = _row("put", rec, spot, T, r)
            if row:
                puts.append(row)
        if calls or puts:
            calls.sort(key=lambda x: x["strike"])
            puts.sort(key=lambda x: x["strike"])
            _bucket = ("near" if dte <= NEAR_MAX_DTE
                       else "monthly" if dte <= MONTHLY_MAX_DTE
                       else "leap")
            out_expiries.append({
                "expiry": exp, "dte": dte, "isFriday": _is_friday(exp),
                "bucket": _bucket, "isLeap": dte >= LEAP_MIN_DTE,
                "calls": calls, "puts": puts,
            })
    if not out_expiries:
        return None
    return parity_iv({
        "ticker": ticker,
        "spot": round(spot, 4),
        "asOf": datetime.datetime.utcnow().isoformat() + "Z",
        "riskFreeRate": round(r, 4),
        "expiries": out_expiries,
    })


# ---- IV surface history (Data tab, z96) -------------------------------------
# One CONSTANT-MATURITY snapshot per ticker per day, so surfaces are comparable
# across time even though listed expiries roll: for each moneyness (strike/spot,
# OTM side: puts below spot, calls at/above), IV is read off each expiry's smile
# by linear interpolation, then interpolated across expiries in TOTAL VARIANCE
# (w = iv^2 * t) to fixed tenors. Never extrapolated - gaps stay null.
SURF_DIR = os.path.join(DATA_DIR, "surfaces")
SURF_M = [0.80, 0.85, 0.90, 0.95, 1.00, 1.05, 1.10, 1.15, 1.20]
SURF_TENORS = [7, 14, 30, 60, 90, 180, 365, 730]
SURF_KEEP = 260                     # ~1 year of trading days


def _smile_at(points, m):
    if not points or m < points[0][0] or m > points[-1][0]:
        return None
    for k in range(1, len(points)):
        if m <= points[k][0]:
            (m0, v0), (m1, v1) = points[k - 1], points[k]
            return v0 + (v1 - v0) * (0 if m1 == m0 else (m - m0) / (m1 - m0))
    return None


ATM_BLEND = 0.025       # within ±2.5% of spot, average put and call IV (z97)


def smile_points(e, S):
    """[(moneyness, iv)] for one expiry: OTM puts below spot, OTM calls above,
    and the put/call AVERAGE near the money - removes the step where the smile
    switches sides (put and call IVs at the same strike rarely match exactly)."""
    ok = lambda o: 0.01 < (_iv_of(o) or 0) < 5
    puts = {o["strike"]: _iv_of(o) for o in (e.get("puts") or []) if ok(o)}
    calls = {o["strike"]: _iv_of(o) for o in (e.get("calls") or []) if ok(o)}
    pts = []
    for K in sorted(set(puts) | set(calls)):
        m = K / S
        if abs(m - 1) <= ATM_BLEND and K in puts and K in calls:
            pts.append((m, (puts[K] + calls[K]) / 2))
        elif K < S and K in puts:
            pts.append((m, puts[K]))
        elif K >= S and K in calls:
            pts.append((m, calls[K]))
    return pts


def constant_maturity_surface(d):
    """-> [[iv% for m in SURF_M] for tenor in SURF_TENORS] (None where uncovered)."""
    S = d.get("spot")
    if not S:
        return None
    exps = []
    for e in sorted(d.get("expiries") or [], key=lambda e: e.get("dte") or 0):
        dte = e.get("dte") or 0
        if dte < 1:
            continue
        pts = smile_points(e, S)
        if len(pts) >= 3:
            exps.append((dte, pts))
    if len(exps) < 2:
        return None
    grid = []
    for T in SURF_TENORS:
        row = []
        for m in SURF_M:
            val = None
            for k in range(1, len(exps)):
                (t0, p0), (t1, p1) = exps[k - 1], exps[k]
                if t0 <= T <= t1:
                    a, b = _smile_at(p0, m), _smile_at(p1, m)
                    if a is not None and b is not None:
                        w0, w1 = a * a * t0, b * b * t1
                        w = w0 + (w1 - w0) * ((T - t0) / (t1 - t0) if t1 != t0 else 0)
                        val = round(math.sqrt(max(w, 0) / T) * 100, 2) if w > 0 else None
                    break
            row.append(val)
        grid.append(row)
    return grid if any(v is not None for r in grid for v in r) else None


def update_surface_history(ticker, d):
    grid = constant_maturity_surface(d)
    if not grid:
        return False
    os.makedirs(SURF_DIR, exist_ok=True)
    p = os.path.join(SURF_DIR, f"{ticker}.json")
    try:
        with open(p) as f:
            h = json.load(f)
    except Exception:
        h = {}
    day = str(d.get("asOf") or datetime.datetime.utcnow().isoformat())[:10]
    snaps = [s for s in (h.get("snapshots") or []) if s.get("date") != day]   # one per day (latest wins)
    snaps.append({"date": day, "spot": d.get("spot"), "iv": grid})
    snaps.sort(key=lambda s: s["date"])
    out = {"ticker": ticker, "moneyness": SURF_M, "tenorsDays": SURF_TENORS,
           "method": "constant-maturity; OTM smile (put/call average within ±2.5% of spot), total-variance across expiries; no extrapolation",
           "snapshots": snaps[-SURF_KEEP:]}
    with open(p, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    return True


# ---- summary analytics (z114) -------------------------------------------------
def _near_dte(d, target):
    exps = [e for e in (d.get("expiries") or []) if (e.get("dte") or 0) >= 1]
    return min(exps, key=lambda e: abs((e.get("dte") or 0) - target)) if exps else None


def _atm_iv(e, S):
    rows = [o for o in (e.get("calls") or []) + (e.get("puts") or []) if _iv_of(o)]
    if not rows:
        return None
    k = min(rows, key=lambda o: abs(o["strike"] - S))["strike"]
    ivs = [_iv_of(o) for o in rows if o["strike"] == k]
    return sum(ivs) / len(ivs) if ivs else None


def _iv_at_delta(rows, target):
    """IV of the contract whose delta is closest to `target` (e.g. -0.25 put)."""
    rows = [o for o in rows if o.get("delta") is not None and _iv_of(o)]
    if not rows:
        return None, None
    o = min(rows, key=lambda x: abs(x["delta"] - target))
    return (_iv_of(o), o["delta"]) if abs(o["delta"] - target) <= 0.12 else (None, None)


def iv_rank(ticker, atm30):
    """IV rank / percentile of today's 30-day ATM IV within the stored surface
    history (data/surfaces/<T>.json, 30d tenor at 100% moneyness). Needs 20+ days."""
    try:
        with open(os.path.join(SURF_DIR, f"{ticker}.json")) as f:
            h = json.load(f)
        i30, i100 = h["tenorsDays"].index(30), h["moneyness"].index(1.00)
        hist = [sn["iv"][i30][i100] for sn in h.get("snapshots") or [] if sn.get("iv") and sn["iv"][i30][i100] is not None]
    except Exception:
        return None
    if atm30 is None or len(hist) < 20:
        return {"n": len(hist)}
    lo, hi = min(hist), max(hist)
    return {"n": len(hist), "rank": round((atm30 - lo) / (hi - lo) * 100, 1) if hi > lo else None,
            "percentile": round(sum(1 for x in hist if x <= atm30) / len(hist) * 100, 1), "low": lo, "high": hi}


def unusual_activity(d, top=5):
    """Contracts trading far above their open interest (volume >= 500 and
    volume >= 2x OI) - fresh positioning rather than churn in old contracts."""
    out = []
    for e in d.get("expiries") or []:
        for side in ("calls", "puts"):
            for o in e.get(side) or []:
                v, oi = o.get("volume") or 0, o.get("openInterest") or 0
                if v >= 500 and v >= 2 * max(oi, 1):
                    out.append({"expiry": e.get("expiry"), "type": side[:-1], "strike": o["strike"], "volume": v,
                                "openInterest": oi, "volOi": round(v / max(oi, 1), 1), "iv": _iv_of(o)})
    out.sort(key=lambda x: (-x["volume"], -x["volOi"]))
    return out[:top]


def quote_quality(d):
    rows = [o for e in d.get("expiries") or [] for side in ("calls", "puts") for o in e.get(side) or []]
    if not rows:
        return None
    clean = sum(1 for o in rows if _clean_mid(o) is not None)
    return round(clean / len(rows) * 100, 1)


def summarize_chain(d):
    """Nearest-expiry ATM IV, expected move (1 sd to that expiry), put/call
    open-interest ratio across the file, and max pain for the nearest expiry."""
    S = d.get("spot")
    exps = d.get("expiries") or []
    if not S or not exps:
        return None
    e0 = exps[0]
    rows = (e0.get("calls") or []) + (e0.get("puts") or [])
    atm = min(rows, key=lambda r: abs(r["strike"] - S)) if rows else None
    iv = _iv_of(atm) if atm else None
    dte = max(1, e0.get("dte") or 1)
    em = (iv * math.sqrt(dte / 365.0) * 100) if iv else None
    coi = sum(r.get("openInterest") or 0 for e in exps for r in (e.get("calls") or []))
    poi = sum(r.get("openInterest") or 0 for e in exps for r in (e.get("puts") or []))
    strikes = sorted({r["strike"] for r in rows})
    pain = None
    if strikes:
        def payout(k):
            return (sum(max(0, k - c["strike"]) * (c.get("openInterest") or 0) for c in (e0.get("calls") or [])) +
                    sum(max(0, p["strike"] - k) * (p.get("openInterest") or 0) for p in (e0.get("puts") or [])))
        pain = min(strikes, key=payout)
    out = {"spot": S, "asOf": d.get("asOf"), "expiries": len(exps), "nearestExpiry": e0.get("expiry"),
           "atmIv": round(iv, 4) if iv else None, "expectedMovePct": round(em, 2) if em else None,
           "putCallOI": round(poi / coi, 3) if coi else None, "maxPain": pain}
    # z114: the read a trader actually wants
    e30, e90 = _near_dte(d, 30), _near_dte(d, 90)
    a30 = _atm_iv(e30, S) if e30 else None
    a90 = _atm_iv(e90, S) if e90 else None
    if e30:
        p25, pd = _iv_at_delta(e30.get("puts") or [], -0.25)
        c25, cd = _iv_at_delta(e30.get("calls") or [], 0.25)
        out["skew25"] = round(p25 - c25, 4) if (p25 and c25) else None          # put wing over call wing (+ = downside fear)
        out["expectedMove30Pct"] = round(a30 * math.sqrt(max(e30.get("dte") or 30, 1) / 365.0) * 100, 2) if a30 else None
        out["atmIv30"] = round(a30, 4) if a30 else None
        out["dte30"] = e30.get("dte")
    if a30 and a90 and e90 is not e30:
        out["termSlope"] = round(a90 - a30, 4)                                  # + = contango (normal), − = inverted (stress)
    out["ivRank"] = iv_rank(d.get("ticker"), a30)
    out["unusual"] = unusual_activity(d)
    out["quoteQualityPct"] = quote_quality(d)
    return out


def main():
    universe = _read_universe()
    if not universe:
        print("No options universe found (set OPTIONS_TICKERS or add data/options_universe.txt / data/tickers.txt).")
        return 0
    r = _risk_free_rate()
    os.makedirs(OPTIONS_DIR, exist_ok=True)
    # z114: oldest chains first, so a run that hits the time budget still
    # rotates through the whole universe across runs (the tail used to starve).
    def _age_key(t):
        try:
            with open(os.path.join(OPTIONS_DIR, f"{t}.json")) as f:
                return str(json.load(f).get("asOf") or "")
        except Exception:
            return ""                           # never fetched -> first
    pinned = [t for t in universe if os.environ.get("OPTIONS_TICKERS")]      # dispatch: keep given order
    universe = pinned or sorted(universe, key=_age_key)
    print(f"Fetching options for {len(universe)} tickers · r={r:.3f} · ≤{MAX_EXPIRIES} expiries (near+monthly+LEAPS) · ±{STRIKE_PCT*100:.0f}% strikes")
    start = time.time()
    written = 0
    skipped = 0
    report = {"tier": os.environ.get("OPTIONS_TIER", "core"), "attempted": 0, "written": [], "noOptions": [],
              "keptStale": [], "notReached": []}
    for i, ticker in enumerate(universe):
        if time.time() - start > TIME_BUDGET_SEC:
            report["notReached"] = universe[i:]
            print(f"Time budget hit at {i}/{len(universe)} — stopping (the oldest chains go first next run).")
            break
        report["attempted"] += 1
        try:
            data = fetch_with_retry(ticker, r)
        except FetchError as e:
            # z114: a transient failure KEEPS the previous chain (it used to be
            # deleted, so one Yahoo hiccup wiped good data until the next run)
            report["keptStale"].append({"ticker": ticker, "error": str(e)})
            print(f"  {ticker}: kept previous chain ({e})", file=sys.stderr)
            continue
        if not data:
            skipped += 1
            report["noOptions"].append(ticker)
            # genuinely no listed options: remove the file so the app doesn't show dead options
            stale = os.path.join(OPTIONS_DIR, f"{ticker}.json")
            if os.path.exists(stale):
                try:
                    os.remove(stale)
                except Exception:
                    pass
            continue
        report["written"].append(ticker)
        with open(os.path.join(OPTIONS_DIR, f"{ticker}.json"), "w") as f:
            json.dump(data, f, separators=(",", ":"))
        try:
            update_surface_history(ticker, data)
        except Exception as e:
            print(f"  surface history failed for {ticker}: {e}")
        written += 1
        if written % 25 == 0:
            print(f"  …{written} written ({i+1}/{len(universe)})")
    # Manifest so the frontend knows which tickers have options without 404-probing,
    # plus a per-ticker summary (spot, ATM IV, expected move, put/call OI, max pain)
    # the app and the bot can read without loading every chain.
    have = sorted([f[:-5] for f in os.listdir(OPTIONS_DIR) if f.endswith(".json") and f not in ("manifest.json", "summary.json")])
    with open(os.path.join(OPTIONS_DIR, "manifest.json"), "w") as f:
        json.dump({"asOf": datetime.datetime.utcnow().isoformat() + "Z", "tickers": have, "count": len(have)}, f)
    summ = {}
    for t in have:
        try:
            with open(os.path.join(OPTIONS_DIR, f"{t}.json")) as f:
                summ[t] = summarize_chain(json.load(f))
        except Exception:
            pass
    with open(os.path.join(OPTIONS_DIR, "summary.json"), "w") as f:
        json.dump({"asOf": datetime.datetime.utcnow().isoformat() + "Z", "tickers": summ}, f, separators=(",", ":"))
    report.update({"asOf": datetime.datetime.utcnow().isoformat() + "Z", "seconds": round(time.time() - start),
                   "chainsOnFile": len(have)})
    with open(os.path.join(OPTIONS_DIR, "run_report.json"), "w") as f:
        json.dump(report, f, separators=(",", ":"))
    _step_summary(report, summ)
    print(f"Done — {len(report['written'])} written, {len(report['noOptions'])} without options, "
          f"{len(report['keptStale'])} kept from the previous run (errors), {len(report['notReached'])} not reached, "
          f"{len(have)} total with chains · {report['seconds']}s")
    return 0


def _step_summary(report, summ):
    """Markdown table in the Actions run page (GITHUB_STEP_SUMMARY)."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [f"### Options chains · tier `{report['tier']}` · {report['seconds']}s", "",
             "| Written | No options | Kept previous (errors) | Not reached | Chains on file |", "|---|---|---|---|---|",
             f"| {len(report['written'])} | {len(report['noOptions'])} | {len(report['keptStale'])} | {len(report['notReached'])} | {report['chainsOnFile']} |", ""]
    if report["keptStale"]:
        lines += ["**Kept previous chain:** " + ", ".join(f"{x['ticker']} ({x['error']})" for x in report["keptStale"][:20]), ""]
    ua = [(t, u) for t, s in summ.items() if s for u in (s.get("unusual") or [])[:1]]
    ua.sort(key=lambda x: -x[1]["volume"])
    if ua:
        lines += ["**Unusual activity (vol ≥ 500 and ≥ 2× OI):**", "", "| Ticker | Contract | Volume | OI | Vol/OI |", "|---|---|---|---|---|"]
        lines += [f"| {t} | {u['expiry']} {u['strike']} {u['type'][0].upper()} | {u['volume']:,} | {u['openInterest']:,} | {u['volOi']} |" for t, u in ua[:10]]
    try:
        with open(path, "a") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
