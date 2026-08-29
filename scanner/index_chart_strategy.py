"""
Index Chart Strategy — clear chart-based system for INDEX OPTIONS
=================================================================
Combines TWO layers (as requested):

  LAYER 1 — DIRECTION (daily) : the regime detector picks the bias
      BULLISH  -> only look for CALL setups
      BEARISH  -> only look for PUT setups
      RANGE    -> no directional trade (skip / strangle)

  LAYER 2 — ENTRY (15-min)    : chart signals time the exact entry
      PULLBACK  : in an uptrend, price pulls back to the 15-min EMA20,
                  prints a bullish candle and holds above VWAP -> BUY CALL
      BREAKOUT  : close breaks the recent 20-bar high above VWAP -> BUY CALL
      CANDLE    : strong candle (Engulfing / Hammer / Marubozu) at a key level
      VWAP BIAS : above VWAP = long-only, below VWAP = short-only filter

Trade plan:
      CALL: entry = signal close | SL = entry - 0.35*daily ATR | TGT = entry + 0.75*daily ATR
      PUT : entry = signal close | SL = entry + 0.35*daily ATR | TGT = entry - 0.75*daily ATR

Signal score = 50% regime confidence + 50% entry quality.

Usage:
    python -m scanner.index_chart_strategy              # current signals for all indices
    python -m scanner.index_chart_strategy --backtest   # validate entries on 15-min data
    python -m scanner.index_chart_strategy --index NIFTY50
"""

import os
import sys
import json
import argparse
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth.login import get_fyers_client, load_env
from scanner.regime import RegimeScanner, classify_regime, _trading_plan, INDICES
from scanner.strategies import _ema, _rsi, _atr

RESULT_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "index_chart_strategy.json")


def _session_vwap(closes, highs, lows, volumes, session_bars: int = 25):
    """VWAP over the current session (last ~25 x 15-min bars)."""
    n = len(closes)
    start = max(0, n - session_bars)
    tv = 0.0
    cum_v = 0.0
    for i in range(start, n):
        tp = (highs[i] + lows[i] + closes[i]) / 3
        tv += tp * volumes[i]
        cum_v += volumes[i]
    return tv / cum_v if cum_v > 0 else closes[-1]


class IndexChartStrategy:
    def __init__(self, regime_min_conf: float = 55.0, sl_atr: float = 0.35, tgt_atr: float = 0.75):
        load_env()
        self.fyers = get_fyers_client()
        self.regime_min_conf = regime_min_conf
        self.sl_atr = sl_atr
        self.tgt_atr = tgt_atr

    # ------------------------------------------------------------------
    def _fetch_tf(self, symbol: str, resolution: str, bars: int = 300) -> tuple[list, list, list, list, list, list]:
        """Fetch candles; return (dates, closes, opens, highs, lows, volumes)."""
        end = datetime.now()
        # resolution D: ~bars*1.4 calendar days; intraday: bars in minutes
        if resolution == "D":
            days = bars + 20
            data = {"symbol": symbol, "resolution": "D", "date_format": 1,
                    "range_from": (end - timedelta(days=days)).strftime("%Y-%m-%d"),
                    "range_to": end.strftime("%Y-%m-%d"), "cont_flag": 1}
        else:
            # intraday: ~25 bars per session, date_format 1 with date-only strings
            days = int(bars / 25) + 5
            data = {"symbol": symbol, "resolution": resolution, "date_format": 1,
                    "range_from": (end - timedelta(days=days)).strftime("%Y-%m-%d"),
                    "range_to": end.strftime("%Y-%m-%d"), "cont_flag": 1}
        resp = self.fyers.history(data=data)
        candles = resp.get("candles", []) if resp.get("s") == "ok" else []
        if not candles:
            return [], [], [], [], [], []
        dates = [datetime.fromtimestamp(c[0]) for c in candles]
        closes = [c[4] for c in candles]
        opens = [c[1] for c in candles]
        highs = [c[2] for c in candles]
        lows = [c[3] for c in candles]
        volumes = [c[5] for c in candles]
        return dates, closes, opens, highs, lows, volumes

    # ------------------------------------------------------------------
    def detect_entry(self, closes, opens, highs, lows, volumes, regime: str, regime_conf: float):
        """Detect the best 15-min chart entry given the daily regime."""
        n = len(closes)
        if n < 60 or regime == "RANGE":
            return None

        ema20 = _ema(closes, 20)
        ema50 = _ema(closes, 50)
        atr = _atr(highs, lows, closes)
        rsi = _rsi(closes, 14)

        e20 = ema20[-1] if ema20[-1] else 0
        e50 = ema50[-1] if ema50[-1] else 0
        a = atr[-1] if atr[-1] else closes[-1] * 0.002
        r = rsi[-1] if rsi[-1] is not None else 50
        c = closes[-1]
        vwap = _session_vwap(closes, highs, lows, volumes)
        above_vwap = c > vwap
        below_vwap = c < vwap

        # candle type on the last bar (real open)
        o = opens[-1]
        body = c - o
        rng_bar = highs[-1] - lows[-1]
        candle = "NEUTRAL"
        if rng_bar > 0:
            body_ratio = abs(body) / rng_bar
            if body > 0 and body_ratio > 0.7:
                candle = "MARUBOZU_BULL"
            elif body < 0 and body_ratio > 0.7:
                candle = "MARUBOZU_BEAR"
            elif body > 0 and (c - lows[-1]) > rng_bar * 0.6 and abs(body) < rng_bar * 0.4:
                candle = "HAMMER"
            elif body < 0 and (highs[-1] - c) > rng_bar * 0.6 and abs(body) < rng_bar * 0.4:
                candle = "SHOOTING_STAR"

        swing_high = max(highs[-21:-1])
        swing_low = min(lows[-21:-1])
        e20_slope = ((ema20[-1] - ema20[-6]) / ema20[-6] * 100) if ema20[-6] else 0

        entry = None

        if regime == "BULLISH":
            pullback = (e20_slope > 0 and c > e50 and above_vwap and
                        abs(c - e20) / e20 < 0.004 and body > 0 and r > 45)
            breakout = (c > swing_high and above_vwap and body > 0)
            candle_ok = (candle in ("MARUBOZU_BULL", "HAMMER") and above_vwap and c > e20)
            if breakout:
                entry = {"type": "BREAKOUT", "quality": 0.9, "instrument": "CALL"}
            elif pullback:
                entry = {"type": "PULLBACK", "quality": 0.85, "instrument": "CALL"}
            elif candle_ok:
                entry = {"type": "CANDLE", "quality": 0.75, "instrument": "CALL"}

        elif regime == "BEARISH":
            pullback = (e20_slope < 0 and c < e50 and below_vwap and
                        abs(c - e20) / e20 < 0.004 and body < 0 and r < 55)
            breakdown = (c < swing_low and below_vwap and body < 0)
            candle_ok = (candle in ("MARUBOZU_BEAR", "SHOOTING_STAR") and below_vwap and c < e20)
            if breakdown:
                entry = {"type": "BREAKDOWN", "quality": 0.9, "instrument": "PUT"}
            elif pullback:
                entry = {"type": "PULLBACK", "quality": 0.85, "instrument": "PUT"}
            elif candle_ok:
                entry = {"type": "CANDLE", "quality": 0.75, "instrument": "PUT"}

        if not entry:
            return None

        # daily ATR for SL/target sizing (fallback ~1.2% of price)
        daily_atr = c * 0.012
        entry["daily_atr"] = round(daily_atr, 2)
        entry["vwap"] = round(vwap, 2)
        entry["above_vwap"] = above_vwap
        entry["swing_high"] = round(swing_high, 2)
        entry["swing_low"] = round(swing_low, 2)
        entry["ema20"] = round(e20, 2)
        entry["ema50"] = round(e50, 2)
        entry["atr"] = round(a, 2)
        entry["rsi"] = round(r, 1)
        entry["candle"] = candle
        entry["regime_conf"] = round(regime_conf, 1)
        entry["score"] = round(regime_conf * 0.5 + entry["quality"] * 50, 1)
        return entry

    # ------------------------------------------------------------------
    def analyze(self, symbol: str, name: str) -> dict:
        # Layer 1: daily regime
        rscan = RegimeScanner()
        d_closes, d_highs, d_lows = rscan.fetch_daily(symbol, 260)
        if len(d_closes) < 60:
            return {}
        regime, conf, drv = classify_regime(d_closes, d_highs, d_lows)
        spot = d_closes[-1]

        # Layer 2: 15-min chart entry
        _d, closes, opens, highs, lows, volumes = self._fetch_tf(symbol, "15", 200)
        entry = None
        if len(closes) >= 60:
            entry = self.detect_entry(closes, opens, highs, lows, volumes, regime, conf)

        # Trade plan
        plan = None
        if regime in ("BULLISH", "BEARISH") and conf >= self.regime_min_conf and entry:
            entry_px = round(closes[-1], 2)
            daily_atr = spot * 0.012
            if entry["instrument"] == "CALL":
                plan = {
                    "instrument": "BUY CALL",
                    "entry": entry_px,
                    "stop": round(entry_px - self.sl_atr * daily_atr, 2),
                    "target": round(entry_px + self.tgt_atr * daily_atr, 2),
                }
            else:
                plan = {
                    "instrument": "BUY PUT",
                    "entry": entry_px,
                    "stop": round(entry_px + self.sl_atr * daily_atr, 2),
                    "target": round(entry_px - self.tgt_atr * daily_atr, 2),
                }

        result = {
            "symbol": symbol, "name": name, "spot": round(spot, 2),
            "regime": regime, "regime_confidence": round(conf, 1),
            "drivers": drv, "entry": entry, "plan": plan,
            "timestamp": datetime.now().isoformat(),
        }
        return result

    # ------------------------------------------------------------------
    def scan_all(self) -> list[dict]:
        results = []
        print(f"\n{'='*96}")
        print("  INDEX CHART STRATEGY — regime direction + 15-min chart entry")
        print(f"{'='*96}")
        for symbol, name in INDICES.items():
            try:
                r = self.analyze(symbol, name)
                if not r:
                    continue
                results.append(r)
                e = r["entry"]
                plan = r["plan"]
                if e and plan:
                    print(f"  {name:<12} {r['regime']:<8} conf={r['regime_confidence']:>4.1f}% | "
                          f"{e['type']:<9} {plan['instrument']:<9} entry={plan['entry']:<9} "
                          f"SL={plan['stop']:<9} TGT={plan['target']:<9} score={e['score']}")
                else:
                    print(f"  {name:<12} {r['regime']:<8} conf={r['regime_confidence']:>4.1f}% | NO ENTRY (wait)")
            except Exception as ex:
                print(f"  {name:<12} error {ex}")
        with open(RESULT_FILE, "w", encoding="utf-8") as f:
            json.dump({"generated": datetime.now().isoformat(), "indices": results}, f, indent=2)
        print(f"\n  Saved to {RESULT_FILE}")
        return results

    # ------------------------------------------------------------------
    def backtest(self, symbol: str, name: str, days: int = 60):
        """Walk-forward on 15-min bars: daily regime as of each bar's date,
        entry from the 15-min chart, then simulate target/SL over next 16 bars."""
        # Daily data WITH dates
        d_dates, d_closes, d_opens, d_highs, d_lows, _dv = self._fetch_tf(symbol, "D", 300)
        if len(d_closes) < 80:
            return {}
        # Precompute daily regime cumulatively (no lookahead)
        daily_regimes = []
        for j in range(80, len(d_closes)):
            regime, conf, _ = classify_regime(d_closes[:j + 1], d_highs[:j + 1], d_lows[:j + 1])
            daily_regimes.append((d_dates[j], regime, conf))

        dates, closes, opens, highs, lows, volumes = self._fetch_tf(symbol, "15", 1500)
        n = len(closes)
        if n < 200:
            return {}

        def regime_at(bar_dt):
            out = ("UNKNOWN", 0.0)
            for dd, rr, cc in daily_regimes:
                if dd <= bar_dt:
                    out = (rr, cc)
                else:
                    break
            return out

        trades = []
        for i in range(150, n - 16):
            regime, conf = regime_at(dates[i])
            if regime == "RANGE" or conf < self.regime_min_conf:
                continue
            entry = self.detect_entry(closes[:i + 1], opens[:i + 1], highs[:i + 1],
                                      lows[:i + 1], volumes[:i + 1], regime, conf)
            if not entry:
                continue
            is_call = entry["instrument"] == "CALL"
            entry_px = closes[i]
            risk = self.sl_atr * entry_px * 0.012
            sl = entry_px - risk if is_call else entry_px + risk
            tgt = entry_px + self.tgt_atr * entry_px * 0.012 if is_call else entry_px - self.tgt_atr * entry_px * 0.012

            exit_px = None
            for k in range(i + 1, min(i + 16, n)):
                if is_call:
                    if lows[k] <= sl:
                        exit_px = sl; break
                    if highs[k] >= tgt:
                        exit_px = tgt; break
                else:
                    if highs[k] >= sl:
                        exit_px = sl; break
                    if lows[k] <= tgt:
                        exit_px = tgt; break
            if exit_px is None:
                exit_px = closes[min(i + 15, n - 1)]

            pnl = ((exit_px - entry_px) / entry_px) if is_call else ((entry_px - exit_px) / entry_px)
            trades.append({"entry_type": entry["type"], "pnl": pnl})

        if not trades:
            print(f"\n  {name} — no qualifying regime+entry setups in sample")
            return {}
        wins = [t for t in trades if t["pnl"] > 0]
        wr = len(wins) / len(trades) * 100
        avg = sum(t["pnl"] for t in trades) / len(trades) * 100
        by_type = {}
        for t in trades:
            by_type.setdefault(t["entry_type"], []).append(t["pnl"])
        print(f"\n  {name} — {len(trades)} 15-min entries | win rate {wr:.1f}% | avg {avg:+.3f}%")
        for typ, pnls in by_type.items():
            tw = len([p for p in pnls if p > 0]) / len(pnls) * 100
            ta = sum(pnls) / len(pnls) * 100
            print(f"      {typ:<10} n={len(pnls):>4} WR={tw:.1f}% avg={ta:+.3f}%")
        return {"n": len(trades), "win_rate": round(wr, 1), "avg_pnl_pct": round(avg, 3)}

    def run_backtest(self):
        print(f"\n{'='*96}")
        print("  INDEX CHART STRATEGY BACKTEST — regime + 15-min entries (target/SL sim)")
        print(f"{'='*96}")
        for symbol, name in INDICES.items():
            try:
                self.backtest(symbol, name)
            except Exception as e:
                print(f"  {name}: error {e}")
        print()


def main():
    parser = argparse.ArgumentParser(description="Index Chart Strategy")
    parser.add_argument("--backtest", action="store_true", help="Validate entries on 15-min data")
    parser.add_argument("--index", type=str, default=None, help="Single index name (e.g. NIFTY50)")
    args = parser.parse_args()

    strat = IndexChartStrategy()
    if args.backtest:
        strat.run_backtest()
        return
    if args.index:
        sym = f"NSE:{args.index.upper()}-INDEX"
        r = strat.analyze(sym, args.index.upper())
        print(json.dumps(r, indent=2, ensure_ascii=False))
        return
    strat.scan_all()


if __name__ == "__main__":
    main()
