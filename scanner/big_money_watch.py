"""
Big Money 15-Minute Watch — live large-order burst detection
=============================================================
Every `interval` minutes during market hours, the option chain is re-polled.
`volume` is cumulative for the day, so the DELTA between two polls is exactly
what traded in that 15-minute window. A large single order (like the
SHRIRAMFIN 1120 CE ~3.5 lakh example) shows up as a giant volume burst plus an
OI jump at one strike.

Detected per window:
- vol_delta  : volume traded in the window (>= threshold = big player active)
- oi_delta   : OI change in the window (positive = fresh positions)
- premium %  : option premium move in the window
- direction  : OI x Premium matrix -> fresh_buying / fresh_writing / covering / unwinding

Writes live bursts to big_money_live.json for the dashboard.

Usage:
    python -m scanner.big_money_watch                 # all 216 F&O stocks, 15 min
    python -m scanner.big_money_watch --stocks 50     # first 50 stocks (faster)
    python -m scanner.big_money_watch --interval 15 --min-vol 50000
"""

import os
import sys
import json
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth.login import get_fyers_client, load_env
from scanner.universe import FNO_STOCKS


def _short_name(symbol: str) -> str:
    return symbol.split(":")[-1].replace("-EQ", "").replace("-INDEX", "")


_MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
           "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def _expiry_days_left(symbol: str) -> int | None:
    """Days from today until the option expiry embedded in the symbol
    (e.g. NSE:SHRIRAMFIN26AUG1120CE -> 26-AUG). Returns None if unparseable."""
    import re
    m = re.search(r"(\d{2})([A-Z]{3})\d{4,}(CE|PE)$", symbol)
    if not m:
        return None
    day, mon = int(m.group(1)), _MONTHS.get(m.group(2).upper())
    if not mon:
        return None
    today = datetime.now()
    year = today.year
    exp = datetime(year, mon, day)
    if exp < today:
        exp = datetime(year + 1, mon, day)
    return (exp.date() - today.date()).days


def _expiry_base(symbol: str) -> str:
    """Symbol prefix WITHOUT the strike, e.g. NSE:RELIANCE26SEP680CE -> NSE:RELIANCE26SEP.
    The naive split('CE')[0] keeps the strike and wrongly collapses the chain to one strike."""
    import re
    m = re.match(r"^(.*?)(\d+(?:\.\d+)?)(CE|PE)$", symbol)
    return m.group(1) if m else symbol.split("CE")[0].split("PE")[0]


class BigMoneyWatch:
    def __init__(self, min_vol_delta: int = 100000, min_oi_delta: int = 50000,
                 min_score: float = 55.0, interval: int = 15):
        load_env()
        self.fyers = get_fyers_client()
        self.min_vol_delta = min_vol_delta
        self.min_oi_delta = min_oi_delta
        self.min_score = min_score
        self.interval = interval
        self.prev: dict = self._load_prev()  # {symbol: {strike: {"vol":..,"oi":..,"ltp":..}}} — persisted
        self.bursts: list[dict] = []

    def _load_prev(self) -> dict:
        """Load the persisted baseline so burst detection resumes without a fresh warm-up pass."""
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "big_money_watch_prev.json")
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_prev(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "big_money_watch_prev.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.prev, f)
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _fetch(self, symbol: str):
        try:
            from scanner.rate_limiter import get_limiter
            resp = get_limiter().retry_call(self.fyers.optionchain, data={"symbol": symbol})
            if not resp or resp.get("s") != "ok":
                return None, []
            data = resp.get("data", {}) or {}
            chain = data.get("optionsChain", []) or []
            spot = 0.0
            sd = data.get("spot")
            if isinstance(sd, dict):
                try:
                    spot = float(sd.get("ltp") or 0)
                except (TypeError, ValueError):
                    spot = 0.0
            if not spot:
                for o in chain:
                    if o.get("option_type") == "":
                        try:
                            spot = float(o.get("ltp") or 0)
                        except (TypeError, ValueError):
                            spot = 0.0
                        break
            options = [o for o in chain if o.get("option_type") in ("CE", "PE")]
            if options:
                # nearest expiry only (group by expiry base, keep the closest)
                exp_groups: dict[str, list] = {}
                for o in options:
                    exp_groups.setdefault(_expiry_base(o.get("symbol", "")), []).append(o)
                best_key = min(exp_groups, key=lambda k: _expiry_days_left(k) if _expiry_days_left(k) is not None else 999)
                options = exp_groups[best_key]
            return spot, options
        except Exception:
            return None, []

    def _score(self, vol_delta: int, oi_delta: int, premium_pct: float, is_atm: bool,
               vol_delta_ratio: float) -> float:
        score = 0.0
        # Volume relative to the stock's own cumulative volume (adaptive)
        if vol_delta_ratio >= 0.50: score += 40
        elif vol_delta_ratio >= 0.30: score += 32
        elif vol_delta_ratio >= 0.15: score += 24
        elif vol_delta_ratio >= 0.08: score += 16
        elif vol_delta_ratio >= 0.04: score += 8
        # Absolute boost for large sizes
        if vol_delta >= 300000: score += 8
        elif vol_delta >= 100000: score += 4

        if oi_delta >= 100000: score += 30
        elif oi_delta >= 50000: score += 22
        elif oi_delta >= 25000: score += 15
        elif oi_delta >= 10000: score += 8
        elif oi_delta <= -50000: score += 10

        ap = abs(premium_pct)
        if ap >= 25: score += 20
        elif ap >= 12: score += 14
        elif ap >= 5: score += 8
        elif ap >= 2: score += 4

        if is_atm: score += 10
        return min(100.0, score)

    def scan_once(self, symbols: list[str]) -> list[dict]:
        now = datetime.now().isoformat()
        hits = []
        for sym in symbols:
            # Skip expiry day and the day before — theta decay creates noise
            exp_days = _expiry_days_left(sym)
            if exp_days is not None and exp_days <= 1:
                self.prev.pop(sym, None)
                continue
            spot, options = self._fetch(sym)
            if not options:
                continue
            atm = min({o["strike_price"] for o in options if o.get("strike_price")},
                      key=lambda s: abs(s - (spot or options[0].get("ltp", 0))))
            prev_sym = self.prev.get(sym, {})

            for o in options:
                strike = float(o.get("strike_price") or 0)
                if strike <= 0:
                    continue
                vol = int(o.get("volume") or 0)
                oi = int(o.get("oi") or 0)
                ltp = float(o.get("ltp") or 0)
                is_atm = abs(strike - atm) <= atm * 0.02

                pv = prev_sym.get(strike)
                if pv is None:
                    continue  # first pass — no delta yet

                vol_delta = vol - pv["vol"]
                oi_delta = oi - pv["oi"]
                if vol_delta < 0 or oi_delta < 0:
                    # new expiry day / data reset — skip
                    continue
                premium_pct = ((ltp - pv["ltp"]) / pv["ltp"] * 100) if pv["ltp"] > 0 else 0

                # UNUSUAL is relative to EACH stock's own volume:
                # what fraction of the day's cumulative volume arrived in this window?
                vol_delta_ratio = vol_delta / max(pv["vol"], 1)
                oi_delta_ratio = oi_delta / max(pv["oi"], 1)

                # Flag if an unusual proportion traded this window (adaptive per stock)
                # OR a genuinely large absolute burst arrived.
                ratio_unusual = vol_delta_ratio >= 0.15 or oi_delta_ratio >= 0.10
                abs_unusual = vol_delta >= self.min_vol_delta or oi_delta >= self.min_oi_delta
                if not (ratio_unusual or abs_unusual):
                    continue

                score = self._score(vol_delta, oi_delta, premium_pct, is_atm, vol_delta_ratio)
                if score < self.min_score:
                    continue

                if oi_delta > 0 and premium_pct > 0:
                    sig, act = "BULLISH", "fresh_buying"
                elif oi_delta > 0 and premium_pct < 0:
                    sig, act = "BEARISH", "fresh_writing"
                elif oi_delta < 0 and premium_pct > 0:
                    sig, act = "BULLISH", "short_covering"
                elif oi_delta < 0 and premium_pct < 0:
                    sig, act = "BEARISH", "long_unwinding"
                else:
                    sig, act = "NEUTRAL", "mixed"

                hits.append({
                    "symbol": sym,
                    "symbol_name": _short_name(sym),
                    "strike": strike,
                    "option_type": o.get("option_type", ""),
                    "ltp": ltp,
                    "premium_change_pct": round(premium_pct, 2),
                    "volume": vol,
                    "oi": oi,
                    "oi_change": oi_delta,
                    "oi_change_pct": round(oi_delta / max(pv["oi"], 1) * 100, 2),
                    "vol_delta": vol_delta,
                    "vol_delta_ratio": round(vol_delta_ratio, 3),
                    "oi_delta_ratio": round(oi_delta_ratio, 3),
                    "vol_oi_ratio": round(vol_delta / max(oi_delta, 1), 2),
                    "is_atm": is_atm,
                    "signal_type": sig,
                    "activity": act,
                    "score": round(score, 1),
                    "mode": "15min_burst",
                    "timestamp": now,
                    "details": {"window_min": self.interval, "atm_strike": atm},
                })

# store new snapshot for next window
            self.prev[sym] = {st: {"vol": int(o.get("volume") or 0),
                                   "oi": int(o.get("oi") or 0),
                                   "ltp": float(o.get("ltp") or 0)}
                               for st, o in [(float(x.get("strike_price") or 0), x)
                                              for x in options if x.get("strike_price")]}

        self._save_prev()
        hits.sort(key=lambda h: h["score"], reverse=True)
        return hits

    def save(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "big_money_live.json")
        self.bursts = self.bursts[:50]
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"generated": datetime.now().isoformat(),
                       "interval_min": self.interval,
                       "bursts": self.bursts}, f, indent=2, ensure_ascii=False)

    def print_bursts(self, bursts: list[dict]):
        if not bursts:
            print(f"  [{datetime.now().strftime('%H:%M:%S')}] no large-order bursts this window")
            return
        print(f"\n  {'='*104}")
        print(f"  BIG MONEY 15-MIN BURSTS  {datetime.now().strftime('%d-%b %H:%M:%S')}")
        print(f"  {'='*104}")
        print(f"  {'Stock':<11}{'Strike':>8}{'T':>4}{'Sig':>10}{'Act':>14}{'Score':>6}"
              f"{'VolΔ':>12}{'Ratio':>7}{'OIΔ':>11}{'Prem%':>8}")
        for b in bursts[:20]:
            print(f"  {b['symbol_name']:<11}{b['strike']:>8.0f}{b['option_type']:>4}"
                  f"{b['signal_type']:>10}{b['activity']:>14}{b['score']:>6.0f}"
                  f"{b['vol_delta']:>12,}{b['vol_delta_ratio']*100:>6.0f}%"
                  f"{b['oi_change']:>+11,}{b['premium_change_pct']:>+7.1f}%")
        print(f"  {'='*104}\n")

    def run(self, symbols: list[str]):
        print(f"\n💎 Big Money 15-MIN WATCH (every {self.interval} min)")
        print(f"  Symbols: {len(symbols)} | min vol burst: {self.min_vol_delta:,} contracts")
        print("  First scan builds the baseline — bursts appear from the 2nd scan.")
        print("  Press Ctrl+C to stop\n")

        pass_no = 0
        while True:
            try:
                now = datetime.now()
                hour, minute = now.hour, now.minute
                market_open = (hour == 9 and minute >= 15) or (10 <= hour <= 14) or (hour == 15 and minute <= 30)
                if not market_open:
                    print(f"  [{now.strftime('%H:%M:%S')}] market closed — waiting (next check in 5 min)")
                    time.sleep(300)
                    continue

                pass_no += 1
                print(f"\n  --- 15-min pass #{pass_no} @ {now.strftime('%H:%M:%S')} ---")
                bursts = self.scan_once(symbols)
                if bursts:
                    self.bursts = bursts + self.bursts
                    self.print_bursts(bursts)
                    self.save()
                else:
                    print(f"  no bursts this window")
                    self.save()
                print(f"  next scan in {self.interval} min...")
                time.sleep(self.interval * 60)
            except KeyboardInterrupt:
                print("\n  Stopped.")
                break
            except Exception as e:
                print(f"  Error: {e}")
                time.sleep(60)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Big Money 15-min watch")
    parser.add_argument("--stocks", type=int, default=None, help="Watch first N F&O stocks")
    parser.add_argument("--interval", type=int, default=15, help="Scan interval in minutes")
    parser.add_argument("--min-vol", type=int, default=50000, help="Min volume burst (contracts) per window")
    parser.add_argument("--min-oi", type=int, default=20000, help="Min OI delta (contracts) per window")
    parser.add_argument("--score", type=float, default=55.0, help="Min score")
    parser.add_argument("--once", action="store_true", help="Single pass then exit (test)")
    args = parser.parse_args()

    w = BigMoneyWatch(min_vol_delta=args.min_vol, min_oi_delta=args.min_oi,
                      min_score=args.score, interval=args.interval)
    symbols = FNO_STOCKS if args.stocks is None else FNO_STOCKS[:args.stocks]

    if args.once:
        bursts = w.scan_once(symbols)
        w.bursts = bursts
        w.print_bursts(bursts)
        w.save()
    else:
        w.run(symbols)


if __name__ == "__main__":
    main()
