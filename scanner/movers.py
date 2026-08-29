"""
F&O Movers — top gainers / losers for the cash segment of F&O stocks.
Shared module used by both the standalone tracker and the dashboard.
"""

import sys
import os
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scanner.fno_universe import FNO_STOCKS_COMPLETE, INDICES, get_symbol_name
from scanner.strategies import _ema, _rsi, _atr
from scanner.rate_limiter import get_limiter

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
QUOTE_BATCH = 40
SIGNAL_EARLY = 1.1
SIGNAL_STRENGTH = 1.5
SIGNAL_STRONG = 2.0
BIG_MOVE_TARGET = 5.0
IST = timezone(timedelta(hours=5, minutes=30))
BENCHMARK = "NSE:NIFTY50-INDEX"

# F&O cash-segment universe (no futures)
SYMBOL_UNIVERSE = list(dict.fromkeys(FNO_STOCKS_COMPLETE + INDICES))

# Fyers quote field-name aliases: futures vs spot/cash responses differ.
_FIELD_ALIASES = {
    "lp": ("lp",),
    "prev_close": ("prev_close", "prev_close_price", "pc"),
    "open": ("open", "open_price"),
    "high": ("high", "high_price"),
    "low": ("low", "low_price"),
    "volume": ("ttq", "volume", "vol"),
}


def _field(q: dict, name: str, default=0.0):
    for key in _FIELD_ALIASES.get(name, (name,)):
        val = q.get(key)
        if val is not None and val != "":
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return default


def fetch_quotes(fyers, symbols: list[str]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    if not symbols:
        return result
    limiter = get_limiter()
    for i in range(0, len(symbols), QUOTE_BATCH):
        batch = symbols[i:i + QUOTE_BATCH]
        resp = limiter.retry_call(fyers.quotes, {"symbols": ",".join(batch)})
        if resp and resp.get("s") == "ok":
            for item in resp.get("d", []):
                n = item.get("n", "")
                if n:
                    result[n] = item.get("v", {})
    return result


def _position_efficiency(r: dict, med_vol: float) -> int:
    gainer = r["change_pct"] >= 0
    ltp, o, h, l = r["ltp"], r["open"], r["high"], r["low"]
    rng = (h - l) if h > l else 0.0
    pos = (ltp - l) / rng if rng > 0 else 0.5
    score = 0.0
    score += (pos if gainer else (1 - pos)) * 35
    if gainer:
        score += 15 if ltp >= o else 15 * pos
    else:
        score += 15 if ltp <= o else 15 * (1 - pos)
    if gainer and h > 0:
        below = (h - ltp) / h * 100
        score += 20 if below <= 1 else 15 if below <= 2 else 10 if below <= 4 else 5
    elif not gainer and l > 0:
        above = (ltp - l) / l * 100
        score += 20 if above <= 1 else 15 if above <= 2 else 10 if above <= 4 else 5
    else:
        score += 10
    if (gainer and ltp > r["prev_close"]) or (not gainer and ltp < r["prev_close"]):
        score += 15
    else:
        score += 5
    vol_ratio = (r["volume"] / med_vol) if med_vol > 0 else 1.0
    score += min(15, vol_ratio * 6)
    return round(max(0, min(100, score)))


def _readiness_flag(r: dict, eff: int) -> str:
    c = r["change_pct"]
    if c >= 3 and eff >= 75:
        return "MOMENTUM"
    if c >= 1 and eff >= 65:
        return "HOLDING STRONG"
    if c <= -3 and eff >= 75:
        return "ACCELERATING"
    if c <= -1 and eff >= 65:
        return "UNDER PRESSURE"
    if eff >= 60 and abs(c) >= 0.5:
        return "WATCH"
    return ""


def _signal_stage(change_pct: float, price_above_open: bool, open_gap_pct: float) -> str:
    if price_above_open:
        if open_gap_pct >= SIGNAL_STRONG:
            return "TRACKING BUY"
        if change_pct >= SIGNAL_STRONG:
            return "STRONG BUY"
        if change_pct >= SIGNAL_STRENGTH:
            return "BUY"
        if change_pct >= SIGNAL_EARLY:
            return "EARLY BUY"
        return ""
    if open_gap_pct <= -SIGNAL_STRONG:
        return "TRACKING SELL"
    if change_pct <= -SIGNAL_STRONG:
        return "STRONG SELL"
    if change_pct <= -SIGNAL_STRENGTH:
        return "SELL"
    if change_pct <= -SIGNAL_EARLY:
        return "EARLY SELL"
    return ""


def refresh_movers(fyers) -> list:
    """Return a list of mover rows (sorted by abs change_pct desc)."""
    quotes = fetch_quotes(fyers, SYMBOL_UNIVERSE)
    rows = []
    for symbol in SYMBOL_UNIVERSE:
        q = quotes.get(symbol)
        if not q:
            continue
        lp = _field(q, "lp")
        prev = _field(q, "prev_close")
        if lp <= 0 or prev <= 0:
            continue
        chg_pct = (lp - prev) / prev * 100.0
        open_p = _field(q, "open")
        change_from_open = (lp - open_p) / open_p * 100.0 if open_p > 0 else 0.0
        open_gap_pct = (open_p - prev) / prev * 100.0 if prev > 0 else 0.0
        price_above_open = open_p > 0 and lp >= open_p
        signal = _signal_stage(chg_pct, price_above_open, open_gap_pct)
        rows.append({
            "symbol": symbol,
            "name": get_symbol_name(symbol),
            "ltp": round(lp, 2),
            "prev_close": round(prev, 2),
            "change": round(lp - prev, 2),
            "change_pct": round(chg_pct, 2),
            "open": round(open_p, 2),
            "open_gap_pct": round(open_gap_pct, 2),
            "change_from_open": round(change_from_open, 2),
            "high": round(_field(q, "high"), 2),
            "low": round(_field(q, "low"), 2),
            "volume": int(_field(q, "volume") or 0),
            "efficiency": 0,
            "flag": "",
            "signal": signal,
        })
    vols = [r["volume"] for r in rows if r["volume"] > 0]
    med_vol = sorted(vols)[len(vols) // 2] if vols else 0
    for r in rows:
        r["efficiency"] = _position_efficiency(r, med_vol)
        r["flag"] = _readiness_flag(r, r["efficiency"])
    rows.sort(key=lambda r: r["change_pct"], reverse=True)
    return rows


# ---------------------------------------------------------------------------
# Technical analysis (same as standalone tracker)
# ---------------------------------------------------------------------------
def _fetch_history(fyers, symbol: str, resolution: str = "D", days: int = 320) -> list:
    end = datetime.now()
    start = end - timedelta(days=days + 30)
    data = {
        "symbol": symbol, "resolution": resolution, "date_format": 1,
        "range_from": start.strftime("%Y-%m-%d"),
        "range_to": end.strftime("%Y-%m-%d"),
        "cont_flag": 1,
    }
    try:
        resp = get_limiter().retry_call(fyers.history, data=data)
        if resp and resp.get("s") == "ok":
            return resp.get("candles", [])
    except Exception as e:
        print(f"  [movers/history] {symbol}: {e}")
    return []


def _ema_now(series, period):
    ema = _ema(series, period)
    return ema[-1] if ema and ema[-1] is not None else None


def _returns(candles: list, n: int) -> float | None:
    if len(candles) <= n:
        return None
    c0 = candles[-(n + 1)][4]
    c1 = candles[-1][4]
    if not c0:
        return None
    return (c1 - c0) / c0 * 100.0


def _intraday_analysis(fyers, symbol: str) -> dict:
    end = datetime.now()
    start = end - timedelta(days=3)
    data = {
        "symbol": symbol, "resolution": "15", "date_format": 1,
        "range_from": start.strftime("%Y-%m-%d"),
        "range_to": end.strftime("%Y-%m-%d"),
        "cont_flag": 1,
    }
    try:
        resp = get_limiter().retry_call(fyers.history, data=data)
        candles = resp.get("candles", []) if resp and resp.get("s") == "ok" else []
    except Exception as e:
        print(f"  [movers/intraday] {symbol}: {e}")
        return {}
    if len(candles) < 8:
        return {}
    today = datetime.now(IST).date()
    today_bars = [c for c in candles if datetime.fromtimestamp(c[0], tz=IST).date() == today]
    bars = today_bars if len(today_bars) >= 4 else candles[-16:]
    closes = [c[4] for c in bars]
    highs = [c[2] for c in bars]
    lows = [c[3] for c in bars]
    vols = [c[5] for c in bars]
    ltp = closes[-1]
    tv = cum_v = 0.0
    for i in range(len(bars)):
        tp = (highs[i] + lows[i] + closes[i]) / 3
        tv += tp * vols[i]
        cum_v += vols[i]
    vwap = (tv / cum_v) if cum_v > 0 else ltp
    hi = max(highs)
    lo = min(lows)
    range_pos = (ltp - lo) / (hi - lo) * 100 if hi > lo else 50.0
    hh_hl = 0
    for i in range(max(1, len(bars) - 6), len(bars)):
        if highs[i] > highs[i - 1] and lows[i] > lows[i - 1]:
            hh_hl += 1
        elif highs[i] < highs[i - 1] and lows[i] < lows[i - 1]:
            hh_hl -= 1
    o = bars[-1][1]
    c = bars[-1][4]
    body = abs(c - o)
    rng_candle = (highs[-1] - lows[-1]) or 1.0
    if c > o and body / rng_candle > 0.5:
        pattern = "STRONG BULLISH"
    elif c < o and body / rng_candle > 0.5:
        pattern = "STRONG BEARISH"
    elif c > o:
        pattern = "BULLISH"
    elif c < o:
        pattern = "BEARISH"
    else:
        pattern = "NEUTRAL"
    return {
        "vwap": round(vwap, 2),
        "price_vs_vwap": round((ltp - vwap) / vwap * 100, 2) if vwap else None,
        "range_pos_pct": round(range_pos, 1),
        "hh_hl_streak": hh_hl,
        "candle": pattern,
        "session_bars": len(bars),
    }


def analyze_future(fyers, symbol: str) -> dict:
    result = {"symbol": symbol, "error": None}
    candles = _fetch_history(fyers, symbol, "D", 320)
    if len(candles) < 50:
        result["error"] = "Insufficient history."
        return result
    closes = [c[4] for c in candles]
    highs = [c[2] for c in candles]
    lows = [c[3] for c in candles]
    vols = [c[5] for c in candles]
    last = closes[-1]
    ema9 = _ema_now(closes, 9)
    ema21 = _ema_now(closes, 21)
    ema50 = _ema_now(closes, 50)
    rsi14 = _rsi(closes, 14)[-1]
    atr14 = _atr(highs, lows, closes, 14)[-1]
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    macd_line = [(a - b) if a is not None and b is not None else None for a, b in zip(ema12, ema26)]
    macd_vals = [m for m in macd_line if m is not None]
    signal = None
    if len(macd_vals) >= 9:
        signal = sum(macd_vals[-9:]) / 9.0
    macd_now = macd_line[-1]
    high_52w = max(highs[-252:]) if len(highs) >= 50 else max(highs)
    low_52w = min(lows[-252:]) if len(lows) >= 50 else min(lows)
    support = min(lows[-20:])
    resistance = max(highs[-20:])
    if len(closes) >= 55:
        slope = (ema50 - closes[-6]) / closes[-6] * 100 if closes[-6] else 0.0
    else:
        slope = 0.0
    above_ema50 = ema50 is not None and last > ema50
    above_ema21 = ema21 is not None and last > ema21
    if ema50 is not None and slope > 0.05 and above_ema50:
        trend = "UPTREND"
    elif ema50 is not None and slope < -0.05 and not above_ema50:
        trend = "DOWNTREND"
    elif ema50 is not None and above_ema50:
        trend = "BULLISH (above EMA50)"
    elif ema50 is not None:
        trend = "BEARISH (below EMA50)"
    else:
        trend = "NEUTRAL"
    bench = _fetch_history(fyers, BENCHMARK, "D", 60)
    rs_5d = rs_20d = None
    if len(bench) > 21:
        stock_5 = _returns(candles, 5)
        stock_20 = _returns(candles, 20)
        nifty_5 = _returns(bench, 5)
        nifty_20 = _returns(bench, 20)
        if None not in (stock_5, nifty_5):
            rs_5d = round(stock_5 - nifty_5, 2)
        if None not in (stock_20, nifty_20):
            rs_20d = round(stock_20 - nifty_20, 2)
    pct_to_res = round((resistance - last) / resistance * 100, 2) if resistance > 0 else None
    pct_from_sup = round((last - support) / last * 100, 2) if last > 0 else None
    intra = _intraday_analysis(fyers, symbol)
    score = 50.0
    if above_ema21:
        score += 7
    else:
        score -= 7
    if above_ema50:
        score += 8
    else:
        score -= 8
    if rsi14 is not None:
        if 55 <= rsi14 <= 75:
            score += 8
        elif rsi14 > 75:
            score += 3
        elif 45 <= rsi14 < 55:
            score += 0
        else:
            score -= 8
    if macd_now is not None and signal is not None:
        if macd_now > signal:
            score += 6
        else:
            score -= 6
    if rs_20d is not None:
        if rs_20d > 0:
            score += 8
        else:
            score -= 8
    score = max(0, min(100, round(score)))
    if score >= 70:
        strength_label = "VERY STRONG"
    elif score >= 55:
        strength_label = "STRONG"
    elif score >= 45:
        strength_label = "NEUTRAL"
    elif score >= 30:
        strength_label = "WEAK"
    else:
        strength_label = "VERY WEAK"
    result.update({
        "last_close": round(last, 2),
        "ema9": round(ema9, 2) if ema9 else None,
        "ema21": round(ema21, 2) if ema21 else None,
        "ema50": round(ema50, 2) if ema50 else None,
        "rsi14": round(rsi14, 2) if rsi14 is not None else None,
        "atr14": round(atr14, 2) if atr14 else None,
        "macd": round(macd_now, 2) if macd_now is not None else None,
        "macd_signal": round(signal, 2) if signal is not None else None,
        "high_52w": round(high_52w, 2),
        "low_52w": round(low_52w, 2),
        "support": round(support, 2),
        "resistance": round(resistance, 2),
        "trend": trend,
        "ema50_slope_pct": round(slope, 3),
        "rs_5d": rs_5d,
        "rs_20d": rs_20d,
        "strength_score": score,
        "strength_label": strength_label,
        "momentum_5d": round(_returns(candles, 5), 2) if _returns(candles, 5) is not None else None,
        "momentum_20d": round(_returns(candles, 20), 2) if _returns(candles, 20) is not None else None,
        "pct_to_res": pct_to_res,
        "pct_from_sup": pct_from_sup,
        "intraday": intra,
    })
    return result