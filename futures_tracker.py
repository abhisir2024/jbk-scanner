"""
F&O Top Gainers & Losers Tracker — Web Dashboard
==================================================
Live tracker for the **underlying cash segment** of all F&O stocks (NSE):

- Uses the FNO stock universe (208 F&O stocks + indices) from scanner.fno_universe.
- Polls live quotes every `--interval` seconds (default 120, i.e. 2 min).
- Ranks by % change from previous close and shows TOP 10 GAINERS and TOP 10 LOSERS.
- TRACKING MODE: if a stock opens >= 2% above the previous close, it enters
  tracking mode (buy).  It stays highlighted while the price holds ABOVE the
  open.  The moment the price falls below the open the signal CLOSES (no
  highlight).  Losers mirror on the downside.
- Progressive strength stages from the previous close:
    >= 1.1%  -> EARLY BUY    (early phase)
    >= 1.5%  -> BUY          (showing strength)
    >= 2.0%  -> STRONG BUY   (more strength — big-move candidate)
    goal     -> 5%+ move for the day
- Auto-refreshes the page every 30s (JS) while the server polls quotes.
- Runs only during market hours (09:15 - 15:40 IST, Mon-Fri); outside those
  hours it keeps the last snapshot and shows "Market Closed".
- DOUBLE-CLICK any row -> modal with SHARE STRENGTH + TECHNICAL ANALYSIS:
    * EMA9/21/50, RSI14, ATR14, MACD, 52-week high/low
    * Support / Resistance, trend classification
    * Relative strength vs NIFTY50 (5D & 20D)
    * Overall strength score (0-100)
    * Intraday 15-min VWAP, range position, candle pattern

Usage:
    python futures_tracker.py                      # http://127.0.0.1:5002
    python futures_tracker.py --port 9000          # custom port
    python futures_tracker.py --interval 60        # refresh every 1 min
    python futures_tracker.py --interval 300       # refresh every 5 min
"""

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(__file__))

from auth.login import get_fyers_client, load_env
from scanner.strategies import _ema, _rsi, _atr
from scanner.fno_universe import FNO_STOCKS_COMPLETE, INDICES, get_symbol_name

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
QUOTE_BATCH = 40          # Fyers quotes limit is 50 symbols / request
DEFAULT_INTERVAL = 120    # seconds between quote refreshes
DEFAULT_PORT = 5002

# Signal stages — all measured from PREVIOUS CLOSE only (not from open).
# Concept: catch stocks in the EARLY phase that are building toward a big
# (>5%) move for the day.
#
# TRACKING MODE: a stock that OPENS >= SIGNAL_STRONG% (2%) above the previous
# close is in tracking mode (buy).  It stays highlighted while price holds
# ABOVE the open.  The moment price falls below the open the signal CLOSES
# (no highlight).  Losers mirror (open <= -2%, held below the open).
#
# For non-gap movers the stage is still shown from the current change:
#     >= 1.1%  -> EARLY BUY    (early phase — first strength)
#     >= 1.5%  -> BUY          (showing strength)
#     >= 2.0%  -> STRONG BUY   (more strength — big-move candidate)
SIGNAL_EARLY = 1.1
SIGNAL_STRENGTH = 1.5
SIGNAL_STRONG = 2.0
BIG_MOVE_TARGET = 5.0

IST = timezone(timedelta(hours=5, minutes=30))

OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "futures_top.json")
BENCHMARK = "NSE:NIFTY50-INDEX"

# Universe: all F&O cash-segment stocks + indices (no futures contracts)
SYMBOL_UNIVERSE = list(dict.fromkeys(FNO_STOCKS_COMPLETE + INDICES))


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------
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
    """Read a quote field using its alias names."""
    for key in _FIELD_ALIASES.get(name, (name,)):
        val = q.get(key)
        if val is not None and val != "":
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return default


def fetch_quotes(fyers, symbols: list[str]) -> dict[str, dict]:
    """Fetch live quotes in batches. Returns {symbol: quote_v}."""
    result: dict[str, dict] = {}
    if not symbols:
        return result
    for i in range(0, len(symbols), QUOTE_BATCH):
        batch = symbols[i:i + QUOTE_BATCH]
        try:
            resp = fyers.quotes({"symbols": ",".join(batch)})
            if resp.get("s") != "ok":
                continue
            for item in resp.get("d", []):
                n = item.get("n", "")
                if n:
                    result[n] = item.get("v", {})
        except Exception as e:
            print(f"  [quotes] batch error: {e}")
        time.sleep(0.2)
    return result


# ---------------------------------------------------------------------------
# Position efficiency + big-move readiness
# ---------------------------------------------------------------------------
def _position_efficiency(r: dict, med_vol: float) -> int:
    """
    Score 0-100 how efficiently a mover is holding its position.

    For a GAINER: price near the day high, above open, above prev close,
    high relative volume = efficient (still has fuel).
    For a LOSER:  mirror (near day low, below open, below prev close).
    """
    gainer = r["change_pct"] >= 0
    ltp, o, h, l = r["ltp"], r["open"], r["high"], r["low"]
    rng = (h - l) if h > l else 0.0
    pos = (ltp - l) / rng if rng > 0 else 0.5

    score = 0.0

    # 1. Range retention (35) — where LTP sits inside the day's range
    score += (pos if gainer else (1 - pos)) * 35

    # 2. Open hold (15) — holding above (gainer) / below (loser) the open
    if gainer:
        score += 15 if ltp >= o else 15 * pos
    else:
        score += 15 if ltp <= o else 15 * (1 - pos)

    # 3. High/low retention (20) — distance from day high (gainer) / low (loser)
    if gainer and h > 0:
        below = (h - ltp) / h * 100
        if below <= 1:
            score += 20
        elif below <= 2:
            score += 15
        elif below <= 4:
            score += 10
        else:
            score += 5
    elif not gainer and l > 0:
        above = (ltp - l) / l * 100
        if above <= 1:
            score += 20
        elif above <= 2:
            score += 15
        elif above <= 4:
            score += 10
        else:
            score += 5
    else:
        score += 10

    # 4. Prev-close confirmation (15)
    if (gainer and ltp > r["prev_close"]) or (not gainer and ltp < r["prev_close"]):
        score += 15
    else:
        score += 5

    # 5. Relative volume (15) — conviction
    vol_ratio = (r["volume"] / med_vol) if med_vol > 0 else 1.0
    score += min(15, vol_ratio * 6)

    return round(max(0, min(100, score)))


def _readiness_flag(r: dict, eff: int) -> str:
    """Flag contracts that look ready for a bigger move."""
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
    """
    Progressive strength stage measured from the previous close.

    TRACKING MODE (buy): the stock OPENED >= SIGNAL_STRONG% (2%) above the
    previous close.  While it keeps trading ABOVE its open it stays
    highlighted as a tracked candidate.  If the price falls BELOW the open
    the signal CLOSES (returns "" — no highlight), because the intraday
    strength since market open is broken.

    Non-gap movers still get a stage based on current change from prev close:
      >= SIGNAL_STRONG   -> STRONG BUY   (big-move candidate)
      >= SIGNAL_STRENGTH -> BUY          (showing strength)
      >= SIGNAL_EARLY    -> EARLY BUY    (early phase — first signal)

    Losers mirror on the downside (open <= -2%, must stay below the open).
    """
    # Buy side — price must hold ABOVE the open, else signal closes
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
    # Sell side — price must stay BELOW the open, else signal closes
    if open_gap_pct <= -SIGNAL_STRONG:
        return "TRACKING SELL"
    if change_pct <= -SIGNAL_STRONG:
        return "STRONG SELL"
    if change_pct <= -SIGNAL_STRENGTH:
        return "SELL"
    if change_pct <= -SIGNAL_EARLY:
        return "EARLY SELL"
    return ""


# ---------------------------------------------------------------------------
# Tracker state
# ---------------------------------------------------------------------------
class FuturesTracker:
    def __init__(self, interval: int = DEFAULT_INTERVAL):
        load_env()
        self.interval = interval
        self.fyers = get_fyers_client()
        self.lock = threading.Lock()
        self.state = {
            "gainers": [],
            "losers": [],
            "updated": None,
            "market_open": False,
            "total_contracts": 0,
            "error": None,
            "signal_buys": 0,
            "signal_sells": 0,
        }
        self._stop = False

    # ------------------------------------------------------------------
    def _market_open(self) -> bool:
        now = datetime.now(IST)
        if now.weekday() >= 5:  # Sat / Sun
            return False
        t = now.hour * 60 + now.minute
        return 9 * 60 + 15 <= t <= 15 * 60 + 40

    # ------------------------------------------------------------------
    def refresh(self, force: bool = False):
        try:
            if not force and not self._market_open():
                with self.lock:
                    self.state["market_open"] = False
                    self.state["error"] = None
                self._write_output()
                return

            if not SYMBOL_UNIVERSE:
                with self.lock:
                    self.state["error"] = "Empty FNO universe."
                return

            # Single quote sweep for the F&O cash segment
            quotes = fetch_quotes(self.fyers, SYMBOL_UNIVERSE)

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

            # Relative volume baseline across the snapshot
            vols = [r["volume"] for r in rows if r["volume"] > 0]
            med_vol = sorted(vols)[len(vols) // 2] if vols else 0

            for r in rows:
                eff = _position_efficiency(r, med_vol)
                r["efficiency"] = eff
                r["flag"] = _readiness_flag(r, eff)

            rows.sort(key=lambda r: r["change_pct"], reverse=True)
            gainers = rows[:10]
            losers = rows[-10:][::-1]

            signal_buys = sum(1 for r in rows if r["signal"] and r["signal"].endswith("BUY"))
            signal_sells = sum(1 for r in rows if r["signal"] and r["signal"].endswith("SELL"))

            with self.lock:
                self.state["gainers"] = gainers
                self.state["losers"] = losers
                self.state["updated"] = datetime.now(IST).isoformat(timespec="seconds")
                self.state["market_open"] = self._market_open()
                self.state["total_contracts"] = len(rows)
                self.state["signal_buys"] = signal_buys
                self.state["signal_sells"] = signal_sells
                self.state["error"] = None
            self._write_output()
            print(f"  [refresh] {len(rows)} symbols | {datetime.now(IST).strftime('%H:%M:%S')} IST")
        except Exception as e:
            print(f"  [refresh] error: {e}")
            with self.lock:
                self.state["error"] = str(e)

    # ------------------------------------------------------------------
    def _write_output(self):
        try:
            with self.lock:
                snap = {
                    "updated": self.state["updated"],
                    "market_open": self.state["market_open"],
                    "interval_sec": self.interval,
                    "gainers": self.state["gainers"],
                    "losers": self.state["losers"],
                }
            with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
                json.dump(snap, f, indent=2)
        except Exception as e:
            print(f"  [output] error: {e}")

    # ------------------------------------------------------------------
    def loop(self):
        self.refresh(force=True)
        # Retry first refresh if it returned 0 (API contention at startup)
        if self.state["total_contracts"] == 0 and not self.state["error"]:
            time.sleep(5)
            self.refresh(force=True)
        while not self._stop:
            time.sleep(self.interval)
            self.refresh()

    def stop(self):
        self._stop = True


# ---------------------------------------------------------------------------
# Technical analysis (double-click)
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
        resp = fyers.history(data=data)
        if resp.get("s") == "ok":
            return resp.get("candles", [])
    except Exception as e:
        print(f"  [history] {symbol}: {e}")
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


def _intraday_analysis(fyers, spot_symbol: str) -> dict:
    """15-min analysis of the underlying cash segment (session VWAP, holding, pattern)."""
    end = datetime.now()
    start = end - timedelta(days=3)
    data = {
        "symbol": spot_symbol, "resolution": "15", "date_format": 1,
        "range_from": start.strftime("%Y-%m-%d"),
        "range_to": end.strftime("%Y-%m-%d"),
        "cont_flag": 1,
    }
    try:
        resp = fyers.history(data=data)
        candles = resp.get("candles", []) if resp.get("s") == "ok" else []
    except Exception as e:
        print(f"  [intraday] {spot_symbol}: {e}")
        return {}

    if len(candles) < 8:
        return {}

    # Today's session bars only
    today = datetime.now(IST).date()
    today_bars = [c for c in candles if datetime.fromtimestamp(c[0], tz=IST).date() == today]
    bars = today_bars if len(today_bars) >= 4 else candles[-16:]

    closes = [c[4] for c in bars]
    highs = [c[2] for c in bars]
    lows = [c[3] for c in bars]
    vols = [c[5] for c in bars]
    ltp = closes[-1]

    # Session VWAP
    tv = cum_v = 0.0
    for i in range(len(bars)):
        tp = (highs[i] + lows[i] + closes[i]) / 3
        tv += tp * vols[i]
        cum_v += vols[i]
    vwap = (tv / cum_v) if cum_v > 0 else ltp

    hi = max(highs)
    lo = min(lows)
    range_pos = (ltp - lo) / (hi - lo) * 100 if hi > lo else 50.0

    # Higher-high / higher-low streak
    hh_hl = 0
    for i in range(max(1, len(bars) - 6), len(bars)):
        if highs[i] > highs[i - 1] and lows[i] > lows[i - 1]:
            hh_hl += 1
        elif highs[i] < highs[i - 1] and lows[i] < lows[i - 1]:
            hh_hl -= 1

    # Last-candle pattern
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
        "bars": [[round(x, 2) for x in b] for b in bars[-20:]],
    }


def analyze_future(fyers, symbol: str) -> dict:
    """Share strength + technical analysis for a cash-segment stock from the F&O universe."""
    result = {"symbol": symbol, "error": None}

    candles = _fetch_history(fyers, symbol, "D", 320)
    if len(candles) < 50:
        result["error"] = "Insufficient history for spot symbol."
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

    # MACD (12,26,9)
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    macd_line = [ (a - b) if a is not None and b is not None else None
                  for a, b in zip(ema12, ema26) ]
    macd_vals = [m for m in macd_line if m is not None]
    signal = None
    if len(macd_vals) >= 9:
        signal = sum(macd_vals[-9:]) / 9.0
    macd_now = macd_line[-1]

    high_52w = max(highs[-252:]) if len(highs) >= 50 else max(highs)
    low_52w = min(lows[-252:]) if len(lows) >= 50 else min(lows)

    support = min(lows[-20:])
    resistance = max(highs[-20:])

    # Trend from EMA50 slope
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

    # Relative strength vs NIFTY50
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

    # Breakout proximity (% to resistance, % from support)
    pct_to_res = round((resistance - last) / resistance * 100, 2) if resistance > 0 else None
    pct_from_sup = round((last - support) / last * 100, 2) if last > 0 else None

    # Intraday analysis (15-min bars of the underlying cash segment)
    intra = _intraday_analysis(fyers, symbol)

    # Strength score 0-100
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


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
_tracker: FuturesTracker | None = None
_analysis_cache: dict = {}
_analysis_lock = threading.Lock()


def _json_response(handler, obj, code=200):
    body = json.dumps(obj).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class TrackerHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self._serve_page()
        elif path == "/api/status":
            with _tracker.lock:
                self._json({
                    "updated": _tracker.state["updated"],
                    "market_open": _tracker.state["market_open"],
                    "interval_sec": _tracker.interval,
                    "total_contracts": _tracker.state["total_contracts"],
                    "signal_buys": _tracker.state["signal_buys"],
                    "signal_sells": _tracker.state["signal_sells"],
                    "error": _tracker.state["error"],
                })
        elif path == "/api/top":
            with _tracker.lock:
                self._json({
                    "gainers": _tracker.state["gainers"],
                    "losers": _tracker.state["losers"],
                    "updated": _tracker.state["updated"],
                    "market_open": _tracker.state["market_open"],
                    "error": _tracker.state["error"],
                })
        elif path == "/api/analysis":
            self._serve_analysis(params)
        else:
            self.send_error(404)

    def _json(self, obj):
        _json_response(self, obj)

    def _serve_page(self):
        html = _get_html()
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_analysis(self, params):
        symbol = params.get("symbol", [""])[0]
        if not symbol:
            _json_response(self, {"error": "symbol required"}, 400)
            return
        with _analysis_lock:
            cached = _analysis_cache.get(symbol)
        if cached and time.time() - cached[1] < 300:
            _json_response(self, cached[0])
            return
        try:
            if symbol not in SYMBOL_UNIVERSE:
                _json_response(self, {"error": "symbol not in F&O universe", "symbol": symbol}, 404)
                return
            analysis = analyze_future(_tracker.fyers, symbol)
            with _analysis_lock:
                _analysis_cache[symbol] = (analysis, time.time())
            _json_response(self, analysis)
        except Exception as e:
            _json_response(self, {"error": str(e), "symbol": symbol})

    def log_message(self, format, *args):
        pass


# ---------------------------------------------------------------------------
# HTML / JS
# ---------------------------------------------------------------------------
def _get_html() -> str:
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>F&O Movers — Gainers & Losers</title>
<style>
:root {
  --bg: #0a0e1a; --bg2: #111827; --card: #1a1f35; --row: #0f1424;
  --border: #1e293b; --text: #f1f5f9; --muted: #94a3b8; --dim: #64748b;
  --green: #22c55e; --red: #ef4444; --blue: #3b82f6; --yellow: #eab308;
}
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:'Inter',-apple-system,'Segoe UI',system-ui,sans-serif;
  background:var(--bg); color:var(--text); line-height:1.5; }
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:#334155;border-radius:3px}
.header{background:linear-gradient(135deg,#111827,#1a1f35);padding:14px 24px;
  display:flex;justify-content:space-between;align-items:center;
  border-bottom:1px solid var(--border);position:sticky;top:0;z-index:50;}
.logo{font-size:22px;font-weight:800;
  background:linear-gradient(135deg,#3b82f6,#06b6d4);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;}
.sub{font-size:12px;color:var(--dim);letter-spacing:1px;text-transform:uppercase;}
.hdr-right{display:flex;align-items:center;gap:14px;font-size:13px;color:var(--muted);}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px;}
.dot.open{background:var(--green);box-shadow:0 0 8px var(--green);}
.dot.closed{background:var(--yellow);}
.container{max-width:1300px;margin:0 auto;padding:20px 24px;}
.cards{display:grid;grid-template-columns:1fr 1fr;gap:20px;}
@media(max-width:900px){.cards{grid-template-columns:1fr;}}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;overflow:hidden;}
.card h2{padding:12px 16px;font-size:15px;font-weight:700;letter-spacing:0.3px;
  border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px;}
.card.gain h2{background:linear-gradient(90deg,rgba(34,197,94,0.14),transparent);color:#4ade80;}
.card.lose h2{background:linear-gradient(90deg,rgba(239,68,68,0.14),transparent);color:#f87171;}
table{width:100%;border-collapse:collapse;font-size:13px;}
thead th{background:var(--bg2);color:var(--muted);font-size:11px;text-transform:uppercase;
  letter-spacing:0.5px;padding:9px 10px;text-align:right;position:sticky;top:0;}
thead th:first-child, td:first-child{text-align:left;}
tbody td{padding:9px 10px;border-top:1px solid var(--border);text-align:right;white-space:nowrap;}
tbody tr{cursor:pointer;transition:background 0.15s;}
tbody tr:hover{background:#1a2040;}
tbody tr:nth-child(1){background:rgba(34,197,94,0.07);}
tbody tr:nth-child(1):hover{background:rgba(34,197,94,0.13);}
tbody tr:nth-child(2){background:rgba(34,197,94,0.04);}
.sym{font-weight:700;font-size:13px;}
.muted{color:var(--muted);} .dim{color:var(--dim);}
.up{color:var(--green);} .down{color:var(--red);}
.chg-badge{display:inline-block;min-width:72px;padding:2px 8px;border-radius:6px;
  font-weight:700;font-size:12px;text-align:center;}
.chg-badge.up{background:rgba(34,197,94,0.15);color:#4ade80;}
.chg-badge.down{background:rgba(239,68,68,0.15);color:#f87171;}
.rank{display:inline-flex;align-items:center;justify-content:center;width:20px;height:20px;
  border-radius:6px;font-size:11px;font-weight:700;background:var(--bg2);color:var(--muted);margin-right:8px;}
.foot{text-align:center;color:var(--dim);font-size:12px;padding:14px;border-top:1px solid var(--border);}
.status-line{text-align:center;color:var(--muted);font-size:13px;padding:14px 0 0;}
.spin{display:inline-block;width:12px;height:12px;border:2px solid var(--dim);
  border-top-color:var(--blue);border-radius:50%;animation:sp 0.8s linear infinite;vertical-align:-2px;}
@keyframes sp{to{transform:rotate(360deg);}}

/* Modal */
.modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.65);
  z-index:200;align-items:center;justify-content:center;padding:20px;}
.modal.open{display:flex;}
.modal-box{background:var(--card);border:1px solid var(--border);border-radius:14px;
  width:100%;max-width:680px;max-height:90vh;overflow:auto;padding:0;}
.modal-head{padding:16px 20px;border-bottom:1px solid var(--border);
  display:flex;justify-content:space-between;align-items:center;
  background:linear-gradient(135deg,#111827,#1a1f35);position:sticky;top:0;border-radius:14px 14px 0 0;}
.modal-head h3{font-size:17px;}
.modal-head .exp{font-size:12px;color:var(--muted);}
.close{cursor:pointer;background:none;border:none;color:var(--muted);font-size:24px;line-height:1;}
.close:hover{color:var(--text);}
.modal-body{padding:18px 20px;}
.metric-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:16px;}
.metric{background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:10px 12px;}
.metric .k{font-size:10px;text-transform:uppercase;letter-spacing:0.5px;color:var(--dim);}
.metric .v{font-size:16px;font-weight:700;margin-top:2px;}
.metric .v.up{color:#4ade80;} .metric .v.down{color:#f87171;}
.section-title{font-size:13px;font-weight:700;color:var(--muted);text-transform:uppercase;
  letter-spacing:0.5px;margin:16px 0 8px;}
.tag{display:inline-block;padding:4px 12px;border-radius:20px;font-size:13px;font-weight:700;}
.tag.bull{background:rgba(34,197,94,0.15);color:#4ade80;}
.tag.bear{background:rgba(239,68,68,0.15);color:#f87171;}
.tag.neutral{background:rgba(234,179,8,0.15);color:#eab308;}
.rs-table{width:100%;font-size:13px;}
.rs-table td{padding:6px 8px;border-top:1px solid var(--border);text-align:right;}
.rs-table td:first-child{text-align:left;color:var(--muted);}
.hint{text-align:center;color:var(--dim);font-size:12px;padding:6px;}
.ldg{display:flex;align-items:center;justify-content:center;height:80px;color:var(--muted);}
.ldg .spin{margin-right:8px;}
.score-bar{background:var(--bg2);height:8px;border-radius:4px;margin-top:4px;overflow:hidden;}
.score-fill{height:100%;border-radius:4px;transition:width 0.4s;}
.flag{display:inline-block;padding:2px 8px;border-radius:6px;font-size:11px;font-weight:700;letter-spacing:0.3px;}
.flag-bull{background:rgba(34,197,94,0.16);color:#4ade80;}
.flag-bear{background:rgba(239,68,68,0.16);color:#f87171;}
.sig{display:inline-block;padding:3px 10px;border-radius:6px;font-size:11px;font-weight:800;
  letter-spacing:0.4px;white-space:nowrap;animation:pulse 2s infinite;}
.sig-buy{background:rgba(34,197,94,0.22);color:#4ade80;border:1px solid rgba(34,197,94,0.45);
  box-shadow:0 0 10px rgba(34,197,94,0.25);}
.sig-sell{background:rgba(239,68,68,0.22);color:#f87171;border:1px solid rgba(239,68,68,0.45);
  box-shadow:0 0 10px rgba(239,68,68,0.25);}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.65}}
.trk{display:inline-block;padding:1px 6px;border-radius:4px;font-size:10px;font-weight:700;
  margin-left:6px;letter-spacing:0.3px;}
.trk-buy{background:rgba(34,197,94,0.12);color:#4ade80;}
.trk-sell{background:rgba(239,68,68,0.12);color:#f87171;}
.sig-count{font-size:11px;font-weight:700;padding:2px 8px;border-radius:6px;margin-left:6px;}
.sig-count.buy{background:rgba(34,197,94,0.15);color:#4ade80;}
.sig-count.sell{background:rgba(239,68,68,0.15);color:#f87171;}
tr.signal-row{outline:1px solid rgba(34,197,94,0.35);outline-offset:-1px;}
tr.signal-row.sell-row{outline-color:rgba(239,68,68,0.35);}
.effbar{display:inline-block;vertical-align:middle;margin-left:4px;background:var(--bg2);
  border-radius:3px;height:5px;overflow:hidden;position:relative;}
.effbar i{display:block;height:100%;background:var(--accent-green,#22c55e);border-radius:3px;}
</style>
</head>
<body>
<div class="header">
  <div>
    <div class="logo">F&O MOVERS</div>
    <div class="sub">Top 10 Gainers & Losers · F&O Cash Segment</div>
  </div>
  <div class="hdr-right">
    <span id="marketTag"><span class="dot closed"></span>Checking...</span>
    <span id="signalInfo"></span>
    <span id="updated">—</span>
    <span id="count"></span>
  </div>
</div>

<div class="container">
  <div class="cards">
    <div class="card gain">
      <h2>&#x1F7E2; TOP 10 GAINERS <span class="sig-count" id="gainSigCount"></span></h2>
      <table>
        <thead><tr>
          <th>Stock</th><th>LTP</th><th>Chg</th><th>% Chg</th>
          <th>From Open</th><th>High</th><th>Low</th><th>Eff</th><th>Volume</th><th>Signal</th>
        </tr></thead>
        <tbody id="gainBody"><tr><td colspan="10" class="hint">Loading...</td></tr></tbody>
      </table>
    </div>
    <div class="card lose">
      <h2>&#x1F534; TOP 10 LOSERS <span class="sig-count" id="loseSigCount"></span></h2>
      <table>
        <thead><tr>
          <th>Stock</th><th>LTP</th><th>Chg</th><th>% Chg</th>
          <th>From Open</th><th>High</th><th>Low</th><th>Eff</th><th>Volume</th><th>Signal</th>
        </tr></thead>
        <tbody id="loseBody"><tr><td colspan="10" class="hint">Loading...</td></tr></tbody>
      </table>
    </div>
  </div>
  <div class="status-line" id="statusLine"></div>
  <div class="foot">Auto-refresh every 30s &middot; Double-click a row for share strength &amp; technical analysis<br>
  <span style="font-size:11px">TRACKING MODE: opened &ge;2% vs prev close &mdash; highlighted while price holds above the open &nbsp;|&nbsp; Signal closes / not highlighted when price drops below the open &nbsp;|&nbsp; Stages: EARLY 1.1% &rarr; BUY 1.5% &rarr; STRONG 2% (goal 5%+)</span></div>
</div>

<div class="modal" id="modal">
  <div class="modal-box">
    <div class="modal-head">
      <div>
        <h3 id="mSym">—</h3>
        <div class="exp" id="mExp"></div>
      </div>
      <button class="close" onclick="closeModal()">&times;</button>
    </div>
    <div class="modal-body" id="mBody"><div class="ldg"><span class="spin"></span>Analyzing...</div></div>
  </div>
</div>

<script>
let last = null;

async function fetchJSON(u) {
  const r = await fetch(u);
  return r.json();
}

function fmt(n, d=2) {
  if (n === null || n === undefined || isNaN(n)) return '—';
  return Number(n).toLocaleString('en-IN', {minimumFractionDigits:d, maximumFractionDigits:d});
}
function fmtInt(n) {
  if (n === null || n === undefined || isNaN(n)) return '—';
  return Number(n).toLocaleString('en-IN');
}

function rowHTML(r, rank) {
  const up = r.change_pct >= 0;
  const cls = up ? 'up' : 'down';
  const badge = up ? 'up' : 'down';
  const arrow = up ? '▲' : '▼';
  const effCls = r.efficiency >= 65 ? 'up' : r.efficiency >= 45 ? 'muted' : 'down';
  const effBar = r.efficiency >= 0 ? ` <span class="effbar" style="width:${Math.min(100, r.efficiency)}px"><i style="width:${r.efficiency}%"></i></span>` : '';
  const openCls = r.change_from_open >= 0 ? 'up' : 'down';
  const sigIsBuy = r.signal && r.signal.includes('BUY');
  const sigCls = sigIsBuy ? 'sig-buy' : 'sig-sell';
  const sig = r.signal ? `<span class="sig ${sigCls}">${r.signal}</span>` : '—';
  const sigRowCls = sigIsBuy ? 'signal-row' : r.signal ? 'signal-row sell-row' : '';
  const trk = r.open_gap_pct != null && Math.abs(r.open_gap_pct) >= 2
    ? `<span class="trk ${r.open_gap_pct >= 0 ? 'trk-buy' : 'trk-sell'}" title="Opened ${fmt(r.open_gap_pct)}% vs prev close — tracking mode">TRK ${fmt(r.open_gap_pct)}%</span>`
    : '';
  return `<tr class="${sigRowCls}" ondblclick="openAnalysis('${r.symbol}')" title="Double-click for analysis">
    <td><span class="rank">${rank}</span><span class="sym">${r.name}</span>${trk}</td>
    <td>${fmt(r.ltp)}</td>
    <td class="${cls}">${arrow} ${fmt(r.change)}</td>
    <td><span class="chg-badge ${badge}">${fmt(r.change_pct)}%</span></td>
    <td class="${openCls}">${fmt(r.change_from_open)}%</td>
    <td>${fmt(r.high)}</td>
    <td>${fmt(r.low)}</td>
    <td class="${effCls}" title="Position efficiency 0-100">${fmt(r.efficiency,0)}${effBar}</td>
    <td>${fmtInt(r.volume)}</td>
    <td>${sig}</td>
  </tr>`;
}

function render(data) {
  last = data;
  const g = data.gainers || [], l = data.losers || [];
  const gBody = document.getElementById('gainBody');
  const lBody = document.getElementById('loseBody');
  gBody.innerHTML = g.length ? g.map((r,i)=>rowHTML(r,i+1)).join('') :
    '<tr><td colspan="10" class="hint">No data yet</td></tr>';
  lBody.innerHTML = l.length ? l.map((r,i)=>rowHTML(r,i+1)).join('') :
    '<tr><td colspan="10" class="hint">No data yet</td></tr>';

  const gSig = g.filter(r=>r.signal && r.signal.includes('BUY')).length;
  const lSig = l.filter(r=>r.signal && r.signal.includes('SELL')).length;
  document.getElementById('gainSigCount').innerHTML =
    gSig ? `<span class="sig-count buy">${gSig} BUY</span>` : '';
  document.getElementById('loseSigCount').innerHTML =
    lSig ? `<span class="sig-count sell">${lSig} SELL</span>` : '';
  document.getElementById('signalInfo').innerHTML =
    (data.signal_buys || data.signal_sells)
      ? `<span class="sig-count buy">${data.signal_buys||0} BUY</span> <span class="sig-count sell">${data.signal_sells||0} SELL</span>`
      : '';

  const tag = document.getElementById('marketTag');
  if (data.market_open) {
    tag.innerHTML = '<span class="dot open"></span>Market Open';
  } else {
    tag.innerHTML = '<span class="dot closed"></span>Market Closed';
  }
  document.getElementById('updated').textContent =
    data.updated ? 'Updated ' + new Date(data.updated + '+05:30').toLocaleTimeString('en-IN') : '—';
  document.getElementById('count').textContent =
    data.total_contracts ? data.total_contracts + ' stocks' : '';
  document.getElementById('statusLine').textContent =
    data.error ? '⚠ ' + data.error :
    data.market_open ? 'Scanning F&O cash segment…' : 'Market closed — showing last snapshot';
}

async function poll() {
  try {
    const data = await fetchJSON('/api/top');
    render(data);
  } catch (e) {
    document.getElementById('statusLine').textContent = '⚠ ' + e;
  }
}

async function openAnalysis(symbol) {
  const modal = document.getElementById('modal');
  const body = document.getElementById('mBody');
  const sym = document.getElementById('mSym');
  const exp = document.getElementById('mExp');
  modal.classList.add('open');
  sym.textContent = symbol.replace('NSE:','').replace('-EQ','').replace('-INDEX','');
  exp.textContent = symbol;
  body.innerHTML = '<div class="ldg"><span class="spin"></span>Analyzing...</div>';
  try {
    const a = await fetchJSON('/api/analysis?symbol=' + encodeURIComponent(symbol));
    renderAnalysis(a, body);
  } catch (e) {
    body.innerHTML = '<div class="hint">Failed to load: ' + e + '</div>';
  }
}

function renderAnalysis(a, body) {
  if (a.error) {
    body.innerHTML = '<div class="hint">' + a.error + '</div>';
    return;
  }
  const t = a.trend || 'NEUTRAL';
  const trendTag = t.startsWith('UP') || t.startsWith('BULL') ? 'bull' :
                   t.startsWith('DOWN') || t.startsWith('BEAR') ? 'bear' : 'neutral';
  const rs20 = a.rs_20d;
  const score = a.strength_score;
  const scoreColor = score >= 55 ? '#22c55e' : score >= 45 ? '#eab308' : '#ef4444';

  const trendRow = a.ema50_slope_pct !== undefined
    ? ` EMA50 slope ${fmt(a.ema50_slope_pct)}%` : '';

  body.innerHTML = `
    <div class="metric-grid">
      <div class="metric"><div class="k">Last Close</div><div class="v">${fmt(a.last_close)}</div></div>
      <div class="metric"><div class="k">Trend</div><div class="v"><span class="tag ${trendTag}">${t}${trendRow}</span></div></div>
      <div class="metric"><div class="k">Strength</div><div class="v ${score>=55?'up':score>=45?'':'down'}">${score}/100</div>
        <div class="score-bar"><div class="score-fill" style="width:${score}%;background:${scoreColor}"></div></div></div>
      <div class="metric"><div class="k">Label</div><div class="v">${a.strength_label}</div></div>
    </div>

    <div class="section-title">Indicators</div>
    <div class="metric-grid">
      <div class="metric"><div class="k">EMA 9 / 21 / 50</div>
        <div class="v" style="font-size:13px">${fmt(a.ema9)} / ${fmt(a.ema21)} / ${fmt(a.ema50)}</div></div>
      <div class="metric"><div class="k">RSI 14</div>
        <div class="v ${a.rsi14>=55?'up':a.rsi14<=45?'down':''}">${fmt(a.rsi14)}</div></div>
      <div class="metric"><div class="k">ATR 14</div><div class="v">${fmt(a.atr14)}</div></div>
      <div class="metric"><div class="k">MACD / Signal</div>
        <div class="v" style="font-size:13px">${fmt(a.macd)} / ${fmt(a.macd_signal)}</div></div>
    </div>

    <div class="section-title">Key Levels</div>
    <div class="metric-grid">
      <div class="metric"><div class="k">Support (20D)</div><div class="v">${fmt(a.support)}</div></div>
      <div class="metric"><div class="k">Resistance (20D)</div><div class="v">${fmt(a.resistance)}</div></div>
      <div class="metric"><div class="k">52W High</div><div class="v up">${fmt(a.high_52w)}</div></div>
      <div class="metric"><div class="k">52W Low</div><div class="v down">${fmt(a.low_52w)}</div></div>
      <div class="metric"><div class="k">% to Resistance</div><div class="v">${a.pct_to_res != null ? fmt(a.pct_to_res) + '%' : '—'}</div></div>
      <div class="metric"><div class="k">% from Support</div><div class="v">${a.pct_from_sup != null ? fmt(a.pct_from_sup) + '%' : '—'}</div></div>
    </div>

    ${a.intraday && a.intraday.vwap != null ? `
    <div class="section-title">Intraday — Underlying Cash (15-min)</div>
    <div class="metric-grid">
      <div class="metric"><div class="k">Session VWAP</div><div class="v">${fmt(a.intraday.vwap)}</div></div>
      <div class="metric"><div class="k">Price vs VWAP</div><div class="v ${a.intraday.price_vs_vwap >= 0 ? 'up' : 'down'}">${fmt(a.intraday.price_vs_vwap)}%</div></div>
      <div class="metric"><div class="k">Range Position</div><div class="v">${fmt(a.intraday.range_pos_pct,1)}%</div></div>
      <div class="metric"><div class="k">HH/HL Streak</div><div class="v">${a.intraday.hh_hl_streak}</div></div>
      <div class="metric"><div class="k">Last Candle</div><div class="v ${a.intraday.candle.includes('BULL') ? 'up' : a.intraday.candle.includes('BEAR') ? 'down' : 'muted'}">${a.intraday.candle}</div></div>
      <div class="metric"><div class="k">Session Bars</div><div class="v">${a.intraday.session_bars}</div></div>
    </div>` : ''}

    <div class="section-title">Relative Strength vs NIFTY50</div>
    <table class="rs-table">
      <tr><td>RS 5-day (stock − NIFTY)</td><td class="${rs5(a.rs_5d)}">${fmt(a.rs_5d)}%</td></tr>
      <tr><td>RS 20-day (stock − NIFTY)</td><td class="${rs5(a.rs_20d)}">${fmt(a.rs_20d)}%</td></tr>
      <tr><td>Momentum 5-day</td><td class="${rs5(a.momentum_5d)}">${fmt(a.momentum_5d)}%</td></tr>
      <tr><td>Momentum 20-day</td><td class="${rs5(a.momentum_20d)}">${fmt(a.momentum_20d)}%</td></tr>
    </table>
    <div class="hint">Double-click another row to switch · analysis cached 5 min</div>`;
}

function rs5(v) { return (v !== null && v !== undefined && v > 0) ? 'up' :
  (v !== null && v !== undefined && v < 0) ? 'down' : 'muted'; }

function closeModal() { document.getElementById('modal').classList.remove('open'); }
document.getElementById('modal').addEventListener('click', (e) => {
  if (e.target.id === 'modal') closeModal();
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });

poll();
setInterval(poll, 30000);
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="F&O Top Gainers & Losers Tracker")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to serve on")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                        help="Quote refresh interval in seconds (60-300)")
    args = parser.parse_args()

    interval = max(60, min(300, args.interval))

    global _tracker
    _tracker = FuturesTracker(interval=interval)

    # Poll loop: first snapshot + periodic refresh
    t = threading.Thread(target=_tracker.loop, daemon=True)
    t.start()

    server = HTTPServer(("127.0.0.1", args.port), TrackerHandler)
    print(f"\nF&O Cash-Segment Movers Tracker running at http://127.0.0.1:{args.port}")
    print(f"  Refresh interval : {interval}s")
    print(f"  Snapshot file    : {OUTPUT_FILE}")
    print("  Press Ctrl+C to stop\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()
        _tracker.stop()


if __name__ == "__main__":
    main()
