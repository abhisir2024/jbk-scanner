"""
Big Money Tracker — unusual positions in STOCK options (F&O stocks only, indices excluded).
=============================================================================================
Detects where big players opened large positions:

1. FRESH BIG POSITION (daily snapshot)
   - OI jump (oich / prev_oi) + heavy volume + premium move at a strike
   - Direction read from the OI x Premium matrix:
       OI up  + premium up   -> fresh aggressive buying (follow)
       OI up  + premium down -> fresh writing / supply (contrarian warning)
       OI down+ premium up   -> short covering (mild bullish)
       OI down+ premium down -> long unwinding (mild bearish)

2. SINGLE LARGE ORDER (--watch mode, snapshot-to-snapshot)
   - Chain is re-scanned every `interval` seconds; an OI jump at one strike
     between two scans is the signature of ONE big order hitting that strike.

Scoring (0-100): OI change 40 + volume 25 + premium move 20 + ATM proximity 10 + freshness 5.

Usage:
    python -m scanner.big_money --scan            # one full stock scan
    python -m scanner.big_money --watch           # continuous (detects single orders)
    python -m scanner.big_money --symbol RELIANCE # single stock
    python -m scanner.big_money --stocks 50       # quick scan of first 50 stocks
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime
from dataclasses import dataclass, field, asdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth.login import get_fyers_client, load_env


@dataclass
class BigMoneySignal:
    symbol: str
    symbol_name: str
    strike: float
    option_type: str          # CE / PE
    ltp: float
    premium_change_pct: float
    volume: int
    oi: int
    oi_change: int
    oi_change_pct: float
    vol_oi_ratio: float
    is_atm: bool
    signal_type: str          # BULLISH / BEARISH / NEUTRAL
    activity: str             # fresh_buying / fresh_writing / short_covering / long_unwinding / mixed
    score: float
    mode: str = "daily"       # daily / single_order
    timestamp: str = ""
    details: dict = field(default_factory=dict)


def _short_name(symbol: str) -> str:
    return symbol.split(":")[-1].replace("-EQ", "").replace("-INDEX", "")


_MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
           "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def _expiry_days_left(symbol: str) -> int | None:
    """Days from today until the option expiry embedded in the symbol.
    Returns None if unparseable."""
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


class BigMoneyTracker:
    def __init__(self, min_score: float = 60.0):
        load_env()
        self.fyers = get_fyers_client()
        self.min_score = min_score
        self.snapshot: dict = self._load_snapshot()   # {symbol: {strike: oi}} — persisted across runs
        self._oi_map: dict = {}                        # full OI map built during the current scan
        self.signals: list[BigMoneySignal] = []

    def _load_snapshot(self) -> dict:
        """Load the persisted OI snapshot so single-order detection works across daily runs."""
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "big_money_snapshot.json")
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    # ------------------------------------------------------------------
    def _fetch_chain(self, symbol: str):
        """Return (spot, options list) or (None, []) on failure."""
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
            # nearest expiry only (group by expiry base, keep the closest)
            if options:
                exp_groups: dict[str, list] = {}
                for o in options:
                    exp_groups.setdefault(_expiry_base(o.get("symbol", "")), []).append(o)
                best_key = min(exp_groups, key=lambda k: _expiry_days_left(k) if _expiry_days_left(k) is not None else 999)
                options = exp_groups[best_key]
            return spot, options
        except Exception as e:
            print(f"    error {symbol}: {e}")
            return None, []

    def _score(self, oi_delta_pct: float, volume: int, oi: int,
               premium_pct: float, is_atm: bool) -> float:
        score = 0.0
        if oi_delta_pct >= 25: score += 40
        elif oi_delta_pct >= 15: score += 30
        elif oi_delta_pct >= 8: score += 20
        elif oi_delta_pct >= 4: score += 10
        elif oi_delta_pct >= 2: score += 5

        vol_oi = volume / max(oi, 1)
        if vol_oi >= 3: score += 25
        elif vol_oi >= 2: score += 18
        elif vol_oi >= 1.2: score += 10
        elif vol_oi >= 0.6: score += 5

        ap = abs(premium_pct)
        if ap >= 30: score += 20
        elif ap >= 15: score += 14
        elif ap >= 8: score += 8
        elif ap >= 3: score += 4

        if is_atm: score += 10
        return min(100.0, score)

    @staticmethod
    def _activity(oi_delta: int, premium_pct: float):
        if oi_delta > 0 and premium_pct > 0:
            return "BULLISH", "fresh_buying"
        if oi_delta > 0 and premium_pct < 0:
            return "BEARISH", "fresh_writing"
        if oi_delta < 0 and premium_pct > 0:
            return "BULLISH", "short_covering"
        if oi_delta < 0 and premium_pct < 0:
            return "BEARISH", "long_unwinding"
        return "NEUTRAL", "mixed"

    # ------------------------------------------------------------------
    def analyze_symbol(self, symbol: str, mode: str = "daily",
                       single_lots: int = 25000) -> list[BigMoneySignal]:
        spot, options = self._fetch_chain(symbol)
        if not options:
            return []

        # Skip expiry day and the day before — theta decay noise
        first_sym = options[0].get("symbol", "")
        exp_days = _expiry_days_left(first_sym)
        if exp_days is not None and exp_days <= 1:
            return []

        atm_strike = min({o["strike_price"] for o in options if o.get("strike_price")},
                         key=lambda s: abs(s - (spot or options[0].get("ltp", 0))))
        out: list[BigMoneySignal] = []
        prev_snap = self.snapshot.get(symbol, {})

        for o in options:
            strike = float(o.get("strike_price") or 0)
            otype = o.get("option_type", "")
            volume = int(o.get("volume") or 0)
            oi = int(o.get("oi") or 0)
            oi_delta = int(o.get("oich") or 0)
            prev_oi = int(o.get("prev_oi") or 0)
            premium_pct = float(o.get("ltpchp") or 0)
            ltp = float(o.get("ltp") or 0)

            if oi <= 0 or strike <= 0:
                continue
            # Record every live strike so the snapshot captures the full chain
            # (needed for reliable single-order detection on the next run)
            self._oi_map.setdefault(symbol, {})[strike] = oi
            is_atm = abs(strike - atm_strike) <= atm_strike * 0.02
            oi_delta_pct = (oi_delta / prev_oi * 100) if prev_oi > 0 else 0.0

            score = self._score(oi_delta_pct, volume, oi, premium_pct, is_atm)

            # Single large order: OI jumped since the last scan snapshot.
            # Enabled in all modes now (snapshot is persisted to disk), so it
            # works across daily scheduled runs, not just a live --watch session.
            mode_used = mode
            delta_snap = 0
            if prev_snap.get(strike) is not None:
                delta_snap = oi - prev_snap.get(strike, oi)
                if delta_snap >= single_lots and (oi_delta_pct >= 2 or delta_snap / max(prev_oi, 1) >= 0.02):
                    score = min(100.0, score + 15)
                    mode_used = "single_order"

            if score < self.min_score:
                continue

            signal_type, activity = self._activity(oi_delta, premium_pct)
            # Writing (OI up, premium down) is often supply — don't label bullish
            out.append(BigMoneySignal(
                symbol=symbol,
                symbol_name=_short_name(symbol),
                strike=strike,
                option_type=otype,
                ltp=ltp,
                premium_change_pct=round(premium_pct, 2),
                volume=volume,
                oi=oi,
                oi_change=oi_delta,
                oi_change_pct=round(oi_delta_pct, 2),
                vol_oi_ratio=round(volume / max(oi, 1), 2),
                is_atm=is_atm,
                signal_type=signal_type,
                activity=activity,
                score=round(score, 1),
                mode=mode_used,
                timestamp=datetime.now().isoformat(),
                details={
                    "atm_strike": atm_strike,
                    "delta_snapshot": delta_snap,
                    "prev_oi": prev_oi,
                },
            ))

        out.sort(key=lambda s: s.score, reverse=True)
        return out

    def scan_all(self, max_stocks: int | None = None, mode: str = "daily",
                 single_lots: int = 25000, sleep: float = 0.25) -> list[BigMoneySignal]:
        from scanner.universe import FNO_STOCKS
        symbols = FNO_STOCKS if max_stocks is None else FNO_STOCKS[:max_stocks]
        total = len(symbols)
        print(f"\n{'='*70}")
        print(f"  BIG MONEY SCAN — {total} F&O stocks (indices excluded)")
        print(f"  Min score: {self.min_score} | Mode: {mode}")
        print(f"{'='*70}\n")

        all_sigs: list[BigMoneySignal] = []
        for i, sym in enumerate(symbols):
            name = _short_name(sym)
            try:
                sigs = self.analyze_symbol(sym, mode=mode, single_lots=single_lots)
                if sigs:
                    print(f"  [{i+1}/{total}] {name:<12} *** {len(sigs)} unusual (top score {sigs[0].score:.0f})")
                    all_sigs.extend(sigs)
                else:
                    print(f"  [{i+1}/{total}] {name:<12} normal")
            except Exception as e:
                print(f"  [{i+1}/{total}] {name:<12} ERROR {e}")
            time.sleep(sleep)

        self.signals = all_sigs
        self._store_snapshot()
        return all_sigs

    def _store_snapshot(self):
        """Merge the current full OI map and persist to disk so single-order
        detection works across separate daily scheduled runs."""
        for sym, strikes in self._oi_map.items():
            self.snapshot.setdefault(sym, {}).update(strikes)
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "big_money_snapshot.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.snapshot, f)
        except Exception:
            pass

    def save_results(self, path: str | None = None):
        if path is None:
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "data", "big_money_signals.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "generated": datetime.now().isoformat(),
                "signals": [asdict(s) for s in self.signals],
            }, f, indent=2, ensure_ascii=False)
        print(f"  Saved {len(self.signals)} signals to {path}")

    def print_report(self, signals: list[BigMoneySignal] | None = None):
        sigs = signals if signals is not None else self.signals
        if not sigs:
            print("\n  No big-money activity above threshold.\n")
            return
        emoji = {"BULLISH": "[B]", "BEARISH": "[S]", "NEUTRAL": "[N]"}
        print(f"\n{'='*96}")
        print(f"  BIG MONEY REPORT (stock options, indices excluded)")
        print(f"{'='*96}")
        print(f"  {'Stock':<12}{'Strike':>9}{'Type':>4}{'Signal':>12}{'Act':>16}{'Score':>7}{'OI%':>8}{'Vol/OI':>8}{'Prem%':>8}")
        print(f"  {'-'*12}{'-'*9}{'-'*4}{'-'*12}{'-'*16}{'-'*7}{'-'*8}{'-'*8}{'-'*8}")
        for s in sigs[:40]:
            mode_tag = " [FAST]" if s.mode == "single_order" else ""
            print(f"  {s.symbol_name:<12}{s.strike:>9.0f}{s.option_type:>4}"
                  f"{emoji.get(s.signal_type, '')+s.signal_type:>12}{s.activity:>16}"
                  f"{s.score:>7.0f}{s.oi_change_pct:>+7.1f}%{s.vol_oi_ratio:>8.1f}{s.premium_change_pct:>+7.1f}%{mode_tag}")
        print(f"{'='*96}\n")

    def monitor(self, interval: int = 300, single_lots: int = 25000):
        print(f"\n[SCAN] Big Money continuous monitor (every {interval}s) — detecting single large orders...")
        print("Press Ctrl+C to stop\n")
        from scanner.universe import FNO_STOCKS
        symbols = FNO_STOCKS
        while True:
            try:
                all_sigs = []
                for sym in symbols:
                    sigs = self.analyze_symbol(sym, mode="watch", single_lots=single_lots)
                    if sigs:
                        all_sigs.extend(sigs)
                    time.sleep(0.2)
                self.signals = all_sigs
                self._store_snapshot()
                self.save_results()
                self.print_report()
                print(f"\n  Next scan in {interval}s...")
                time.sleep(interval)
            except KeyboardInterrupt:
                print("\n  Stopped.")
                break
            except Exception as e:
                print(f"  Error: {e}")
                time.sleep(30)


def main():
    parser = argparse.ArgumentParser(description="Big Money Tracker (stock options, no indices)")
    parser.add_argument("--scan", action="store_true", help="One full scan of all F&O stocks")
    parser.add_argument("--watch", action="store_true", help="Continuous monitor (detects single large orders)")
    parser.add_argument("--symbol", type=str, help="Single symbol (e.g., RELIANCE)")
    parser.add_argument("--stocks", type=int, default=None, help="Limit scan to first N stocks")
    parser.add_argument("--score", type=float, default=50.0, help="Min score (0-100)")
    parser.add_argument("--interval", type=int, default=300, help="Monitor interval (s)")
    parser.add_argument("--lots", type=int, default=25000, help="Single-order OI threshold (lots)")
    parser.add_argument("--json", type=str, default=None, help="Save results to JSON file")
    args = parser.parse_args()

    tracker = BigMoneyTracker(min_score=args.score)

    if args.watch:
        tracker.monitor(args.interval, args.lots)
    elif args.symbol:
        sym = f"NSE:{args.symbol.upper()}-EQ"
        sigs = tracker.analyze_symbol(sym)
        tracker.signals = sigs
        tracker.print_report(sigs)
        if args.json:
            tracker.save_results(args.json)
    else:
        sigs = tracker.scan_all(max_stocks=args.stocks, single_lots=args.lots)
        tracker.print_report(sigs)
        tracker.save_results(args.json)


if __name__ == "__main__":
    main()
