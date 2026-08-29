"""
Index Options Regime Strategy
=============================
Classifies each index's market state into one of three regimes and gives the
matching index-options trade plan:

  BULLISH  (trending up)   -> Buy CALL  (buyers make money)
  BEARISH  (trending down) -> Buy PUT   (sellers make money)
  RANGE    (choppy)        -> No directional trade / trade range edges / strangle

Detection (multi-indicator, scored 0-100 each regime):
  - Trend:   EMA20 vs EMA50, EMA50 slope, price vs EMA20/50/200
  - Strength: ADX(14)  (>22 trending, <20 ranging)
  - Momentum: RSI(14), position in 20-day range
  - Volatility: Bollinger Band width (compression = range)

Confidence = top regime score / total of all scores.

Usage:
    python -m scanner.regime                     # current regime for all indices
    python -m scanner.regime --backtest          # validate regime vs next-day moves
    python -m scanner.regime --index NIFTY50     # single index
"""

import os
import sys
import json
import argparse
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth.login import get_fyers_client, load_env
from scanner.strategies import _ema, _rsi

INDICES = {
    "NSE:NIFTY50-INDEX": "NIFTY",
    "NSE:NIFTYBANK-INDEX": "BANKNIFTY",
    "NSE:NIFTYMIDCAP100-INDEX": "MIDCAP100",
    "NSE:FINNIFTY-INDEX": "FINNIFTY",
    "NSE:NIFTYIT-INDEX": "NIFTYIT",
    "NSE:NIFTYNEXT50-INDEX": "NIFTYNEXT50",
}

RESULT_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "index_regime.json")


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------
def _wilder_smooth(values: list[float], n: int) -> list[float]:
    """Wilder smoothing (same as _atr/ADX use)."""
    out: list[float] = [None] * n
    if len(values) < n + 1:
        return out
    out.append(sum(values[1:n + 1]) / n)
    for i in range(n + 1, len(values)):
        out.append((out[-1] * (n - 1) + values[i]) / n)
    return out


def _adx(highs, lows, closes, n=14):
    """Average Directional Index (Wilder, iterative) — aligned with closes."""
    length = len(closes)
    if length < 30:
        return [None] * length
    pdi: list[float | None] = [None] * length
    mdi: list[float | None] = [None] * length
    adx: list[float | None] = [None] * length
    tr_s = pd_s = md_s = 0.0
    for i in range(1, length):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        pd = up if (up > down and up > 0) else 0.0
        md = down if (down > up and down > 0) else 0.0
        if i <= n:
            tr_s += tr; pd_s += pd; md_s += md
        else:
            tr_s = tr_s - tr_s / n + tr
            pd_s = pd_s - pd_s / n + pd
            md_s = md_s - md_s / n + md
        if i >= n and tr_s > 0:
            pdi[i] = pd_s / tr_s * 100
            mdi[i] = md_s / tr_s * 100
            dx = abs(pdi[i] - mdi[i]) / (pdi[i] + mdi[i]) * 100 if (pdi[i] + mdi[i]) > 0 else 0
            adx[i] = dx if i == n else ((adx[i - 1] * (n - 1) + dx) / n if adx[i - 1] is not None else dx)
    return adx


def _bb_width(closes, n=20, k=2.0) -> list[float | None]:
    out: list[float | None] = []
    for i in range(len(closes)):
        if i < n - 1:
            out.append(None)
            continue
        win = closes[i - n + 1:i + 1]
        mid = sum(win) / n
        std = (sum((x - mid) ** 2 for x in win) / n) ** 0.5
        if mid > 0:
            out.append(4 * k * std / mid * 100)
        else:
            out.append(None)
    return out


def _atr_list(highs, lows, closes, n=14) -> list[float | None]:
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    return _wilder_smooth(trs, n)


# ---------------------------------------------------------------------------
# Regime classification
# ---------------------------------------------------------------------------
def classify_regime(closes, highs, lows):
    """Return (regime, confidence, drivers) for the LAST bar."""
    n = len(closes)
    if n < 60:
        return "UNKNOWN", 0.0, {}

    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    ema200 = _ema(closes, 200) if n >= 210 else [None] * n
    rsi = _rsi(closes, 14)
    adx = _adx(highs, lows, closes)
    atr = _atr_list(highs, lows, closes)
    bbw = _bb_width(closes)

    c = closes[-1]
    e20, e50 = ema20[-1], ema50[-1]
    e50_slope = ((ema50[-1] - ema50[-6]) / ema50[-6] * 100) if ema50[-6] else 0
    r = rsi[-1] if rsi[-1] is not None else 50
    a = adx[-1] if adx[-1] is not None else 15
    b = bbw[-1] if bbw[-1] is not None else 100
    atr_now = atr[-1] if atr[-1] else c * 0.01
    e200 = ema200[-1] if ema200[-1] else c

    hi20 = max(highs[-20:])
    lo20 = min(lows[-20:])
    pos_range = (c - lo20) / (hi20 - lo20) * 100 if hi20 > lo20 else 50

    # --- scores (each 0..100 max) ---
    bull = 0
    if e20 > e50: bull += 20
    if c > e20: bull += 15
    if r > 55: bull += 15
    if pos_range > 55: bull += 15
    if a >= 22: bull += 15
    if e50_slope > 0: bull += 10
    if c > e200: bull += 10

    bear = 0
    if e20 < e50: bear += 20
    if c < e20: bear += 15
    if r < 45: bear += 15
    if pos_range < 45: bear += 15
    if a >= 22: bear += 15
    if e50_slope < 0: bear += 10
    if c < e200: bear += 10

    rng = 0
    if a < 20: rng += 30
    if 40 <= r <= 60: rng += 20
    if 35 <= pos_range <= 65: rng += 20
    if b < 4.0: rng += 15

    scores = {"BULLISH": bull, "BEARISH": bear, "RANGE": rng}
    total = bull + bear + rng
    regime = max(scores, key=scores.get)
    conf = scores[regime] / total * 100 if total else 0

    drivers = {
        "ema20": round(e20, 2), "ema50": round(e50, 2), "ema200": round(e200, 2),
        "ema50_slope_pct": round(e50_slope, 2), "rsi": round(r, 1), "adx": round(a, 1),
        "pos_in_range_pct": round(pos_range, 1), "bb_width_pct": round(b, 2),
        "atr_pct": round(atr_now / c * 100, 2), "close": round(c, 2),
        "scores": {k: round(v, 1) for k, v in scores.items()},
    }
    return regime, conf, drivers


def _trading_plan(regime: str, conf: float, spot: float, atr_pct: float) -> dict:
    atr_pts = spot * atr_pct / 100
    if regime == "BULLISH" and conf >= 60:
        return {
            "trade": "BUY CALL",
            "instrument": "ATM / ITM CALL",
            "entry": spot,
            "stop": round(spot - 1.5 * atr_pts, 2),
            "target": round(spot + 2.5 * atr_pts, 2),
            "hold": "while index close > EMA20",
            "reason": "Trending up — buyers in control.",
        }
    if regime == "BEARISH" and conf >= 60:
        return {
            "trade": "BUY PUT",
            "instrument": "ATM / ITM PUT",
            "entry": spot,
            "stop": round(spot + 1.5 * atr_pts, 2),
            "target": round(spot - 2.5 * atr_pts, 2),
            "hold": "while index close < EMA20",
            "reason": "Trending down — sellers in control.",
        }
    if regime == "RANGE" and conf >= 50:
        return {
            "trade": "NO DIRECTIONAL TRADE / SELL STRANGLE",
            "instrument": "Short Strangle (OTM CE+PE) OR buy near range edges",
            "entry": spot,
            "stop": "range high + buffer (CE) / range low - buffer (PE)",
            "target": "collect premium decay",
            "hold": "until range breakout / expiry",
            "reason": "Chopping sideways — no trend, sell premium or wait.",
        }
    return {
        "trade": "WAIT / NO TRADE",
        "instrument": "—",
        "entry": spot,
        "stop": "—",
        "target": "—",
        "hold": "—",
        "reason": "Conflicting signals (confidence too low).",
    }


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------
class RegimeScanner:
    def __init__(self):
        load_env()
        self.fyers = get_fyers_client()

    def fetch_daily(self, symbol: str, days: int = 260) -> tuple[list, list, list]:
        """Fetch daily candles, chunking the request (Fyers caps ~200-260 days per call)."""
        end = datetime.now()
        merged: dict = {}
        current_end = end
        remaining = min(days, 500)
        guard = 0
        while remaining > 0 and guard < 12:
            chunk_days = min(remaining, 200)
            start = current_end - timedelta(days=chunk_days + 10)
            data = {"symbol": symbol, "resolution": "D", "date_format": 1,
                    "range_from": start.strftime("%Y-%m-%d"), "range_to": current_end.strftime("%Y-%m-%d"),
                    "cont_flag": 1}
            try:
                resp = self.fyers.history(data=data)
                if resp.get("s") == "ok" and resp.get("candles"):
                    for c in resp["candles"]:
                        merged[c[0]] = c
            except Exception:
                pass
            current_end = start
            remaining -= chunk_days
            guard += 1

        if not merged:
            return [], [], []
        candles = [merged[k] for k in sorted(merged)]
        closes = [c[4] for c in candles]
        highs = [c[2] for c in candles]
        lows = [c[3] for c in candles]
        return closes, highs, lows

    def scan(self) -> list[dict]:
        results = []
        print(f"\n{'='*88}")
        print("  INDEX REGIME SCAN (options strategy)")
        print(f"{'='*88}")
        for symbol, name in INDICES.items():
            closes, highs, lows = self.fetch_daily(symbol)
            if len(closes) < 60:
                print(f"  {name:<12} no data")
                continue
            spot = closes[-1]
            regime, conf, drv = classify_regime(closes, highs, lows)
            plan = _trading_plan(regime, conf, spot, drv["atr_pct"])
            results.append({
                "symbol": symbol, "name": name, "spot": spot,
                "regime": regime, "confidence": round(conf, 1),
                "drivers": drv, "plan": plan,
                "timestamp": datetime.now().isoformat(),
            })
            emoji = {"BULLISH": "[B] CALL", "BEARISH": "[S] PUT", "RANGE": "[R] RANGE"}.get(regime, "[?]")
            print(f"  {name:<12} spot={spot:>10,.1f}  regime={emoji:<9} conf={conf:>5.1f}%"
                  f"  ADX={drv['adx']:>4.1f} RSI={drv['rsi']:>4.1f} pos={drv['pos_in_range_pct']:>3.0f}%")
        with open(RESULT_FILE, "w", encoding="utf-8") as f:
            json.dump({"generated": datetime.now().isoformat(), "indices": results}, f, indent=2)
        print(f"\n  Saved to {RESULT_FILE}")
        return results

    # ------------------------------------------------------------------
    def backtest(self, symbol: str, name: str, days: int = 400) -> dict:
        """Walk-forward: classify regime using data up to each bar, then check
        the next-day (and 3-day) index move. Validates the regime labels."""
        closes, highs, lows = self.fetch_daily(symbol, days)
        if len(closes) < 100:
            return {}
        stats = {"BULLISH": {"n": 0, "up": 0, "ret": []},
                 "BEARISH": {"n": 0, "up": 0, "ret": []},
                 "RANGE": {"n": 0, "up": 0, "ret": [], "abs_move": []}}
        for i in range(80, len(closes) - 3):
            regime, conf, _ = classify_regime(closes[:i + 1], highs[:i + 1], lows[:i + 1])
            if regime == "UNKNOWN":
                continue
            ret_1d = (closes[i + 1] / closes[i] - 1) * 100
            ret_3d = (closes[i + 3] / closes[i] - 1) * 100
            s = stats[regime]
            s["n"] += 1
            if ret_1d > 0:
                s["up"] += 1
            s["ret"].append(ret_1d)
            if regime == "RANGE":
                s["abs_move"].append(abs(ret_3d))
        print(f"\n  BACKTEST — {name} (next-day moves by regime)")
        print(f"  {'Regime':<10}{'Days':>7}{'Next-day up%':>13}{'Avg 1d ret':>12}{'Avg |3d| move':>14}")
        print(f"  {'-'*10}{'-'*7}{'-'*13}{'-'*12}{'-'*14}")
        out = {"name": name}
        for regime, s in stats.items():
            if s["n"] == 0:
                continue
            up_rate = s["up"] / s["n"] * 100
            avg_ret = sum(s["ret"]) / len(s["ret"])
            abs_moves = s.get("abs_move", [])
            avg_abs = sum(abs_moves) / len(abs_moves) if abs_moves else 0
            out[regime] = {"days": s["n"], "up_rate": round(up_rate, 1),
                           "avg_ret": round(avg_ret, 3), "avg_abs_3d": round(avg_abs, 3)}
            print(f"  {regime:<10}{s['n']:>7}{up_rate:>12.1f}%{avg_ret:>+11.3f}%{avg_abs:>13.3f}%")
        return out

    def run_backtest(self):
        print(f"\n{'='*88}")
        print("  REGIME BACKTEST — does BULLISH/BEARISH/RANGE actually predict moves?")
        print(f"{'='*88}")
        for symbol, name in INDICES.items():
            try:
                self.backtest(symbol, name)
            except Exception as e:
                print(f"  {name}: error {e}")
        print()


def main():
    parser = argparse.ArgumentParser(description="Index Options Regime Strategy")
    parser.add_argument("--backtest", action="store_true", help="Validate regime vs next-day moves")
    parser.add_argument("--index", type=str, default=None, help="Single index name (e.g. NIFTY50)")
    args = parser.parse_args()

    scanner = RegimeScanner()
    if args.backtest:
        scanner.run_backtest()
        return
    if args.index:
        sym = f"NSE:{args.index.upper()}-INDEX"
        closes, highs, lows = scanner.fetch_daily(sym)
        if closes:
            regime, conf, drv = classify_regime(closes, highs, lows)
            plan = _trading_plan(regime, conf, closes[-1], drv["atr_pct"])
            print(json.dumps({"name": args.index, "spot": closes[-1], "regime": regime,
                              "confidence": round(conf, 1), "drivers": drv, "plan": plan},
                             indent=2, ensure_ascii=False))
        return
    scanner.scan()


if __name__ == "__main__":
    main()
