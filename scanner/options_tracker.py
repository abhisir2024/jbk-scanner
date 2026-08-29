"""
Unusual Options Activity (UOA) Tracker
=======================================
Detects institutional/smart money activity in options markets.

Data from Fyers API:
- Volume: Current trading volume
- OI: Open Interest
- OI Change: Daily change in OI
- LTP: Last traded price

Scoring System (0-100):
- Volume ratio (40%): Volume vs average
- OI change (30%): New positions vs rolling
- Premium change (20%): Price movement
- Strike proximity (10%): Near ATM = more relevant

Usage:
    python -m scanner.options_tracker                    # Scan all F&O
    python -m scanner.options_tracker --symbol NIFTY50   # Scan specific
    python -m scanner.options_tracker --watch             # Continuous
"""

import os
import sys
import json
import time
from datetime import datetime, timedelta
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from auth.login import get_fyers_client, load_env


@dataclass
class OptionSignal:
    """Represents unusual options activity."""
    symbol: str
    strike: float
    option_type: str  # CE or PE
    ltp: float
    volume: int
    oi: int
    oi_change: int
    oi_change_pct: float
    premium_change_pct: float
    unusual_score: float  # 0-100
    signal_type: str  # BULLISH, BEARISH, NEUTRAL
    timestamp: str
    details: dict = field(default_factory=dict)


class UnusualOptionsTracker:
    """Tracks unusual options activity across F&O stocks."""
    
    def __init__(self):
        load_env()
        self.fyers = get_fyers_client()
        self.baseline_oi: dict = {}  # Store previous OI for comparison
        self.history: list[OptionSignal] = []
        
    def _fetch_option_chain(self, symbol: str) -> dict | None:
        """Fetch option chain data from Fyers API."""
        try:
            resp = self.fyers.optionchain(data={"symbol": symbol})
            if resp.get("s") == "ok" and "data" in resp:
                return resp["data"]
        except Exception as e:
            print(f"Error fetching option chain for {symbol}: {e}")
        return None
    
    def _calculate_unusual_score(self, volume: int, oi_change: int, 
                                   premium_change: float, is_atm: bool) -> float:
        """
        Calculate unusual activity score (0-100).
        
        Factors:
        - Volume (40% weight): Higher volume = more unusual
        - OI Change (30% weight): Positive = new positions, Negative = unwinding
        - Premium Change (20% weight): Large moves = significant
        - ATM Proximity (10% weight): ATM options are most liquid
        """
        score = 0.0
        
        # Volume scoring (0-40 points)
        if volume > 100000:
            score += 40  # Very high volume
        elif volume > 50000:
            score += 30
        elif volume > 20000:
            score += 20
        elif volume > 10000:
            score += 10
        elif volume > 5000:
            score += 5
        
        # OI Change scoring (0-30 points)
        if oi_change > 10000:
            score += 30  # Major OI increase
        elif oi_change > 5000:
            score += 20
        elif oi_change > 2000:
            score += 15
        elif oi_change > 1000:
            score += 10
        elif oi_change > 500:
            score += 5
        
        # Premium change scoring (0-20 points)
        abs_change = abs(premium_change)
        if abs_change > 50:
            score += 20  # Huge premium move
        elif abs_change > 30:
            score += 15
        elif abs_change > 20:
            score += 10
        elif abs_change > 10:
            score += 5
        
        # ATM proximity bonus (0-10 points)
        if is_atm:
            score += 10
        
        return min(100, score)
    
    def _determine_signal_type(self, ce_activity: list, pe_activity: list) -> str:
        """Determine if activity is bullish, bearish, or neutral."""
        ce_score = sum(a.unusual_score for a in ce_activity)
        pe_score = sum(a.unusual_score for a in pe_activity)
        
        if ce_score > pe_score * 1.5:
            return "BULLISH"
        elif pe_score > ce_score * 1.5:
            return "BEARISH"
        else:
            return "NEUTRAL"
    
    def scan_symbol(self, symbol: str, min_score: float = 50.0) -> list[OptionSignal]:
        """Scan a symbol for unusual options activity."""
        signals = []
        
        # Fetch option chain
        chain_data = self._fetch_option_chain(symbol)
        if not chain_data:
            return signals
        
        options_chain = chain_data.get("optionsChain", [])
        
        # Filter for actual options
        options = [o for o in options_chain if o.get("option_type") in ["CE", "PE"]]
        
        if not options:
            return signals
        
        # Find ATM strike (closest to current price)
        current_price = options[0].get("ltp", 0)
        strikes = sorted(set(o["strike_price"] for o in options))
        
        # Get nearest expiry options (first expiry only for now)
        # Group by expiry and take the nearest
        expiry_groups = {}
        for opt in options:
            # Extract expiry from symbol (e.g., NIFTY26AUG21750PE)
            sym = opt.get("symbol", "")
            expiry_key = sym.split("PE")[0].split("CE")[0] if "PE" in sym or "CE" in sym else "unknown"
            if expiry_key not in expiry_groups:
                expiry_groups[expiry_key] = []
            expiry_groups[expiry_key].append(opt)
        
        # Process nearest expiry
        if expiry_groups:
            nearest_expiry = list(expiry_groups.values())[0]
        else:
            nearest_expiry = options
        
        # Find ATM strike
        ltp = chain_data.get("optionsChain", [{}])[0].get("ltp", 0)
        if ltp == 0:
            ltp = current_price
        atm_strike = min(strikes, key=lambda s: abs(s - ltp))
        
        # Analyze each option
        for opt in nearest_expiry:
            strike = opt.get("strike_price", 0)
            option_type = opt.get("option_type", "")
            volume = opt.get("volume", 0)
            oi = opt.get("oi", 0)
            oi_change = opt.get("oich", 0)
            oi_change_pct = opt.get("oichp", 0)
            ltp_opt = opt.get("ltp", 0)
            ltp_change = opt.get("ltpch", 0)
            ltp_change_pct = opt.get("ltpchp", 0)
            
            # Skip if no volume
            if volume == 0:
                continue
            
            # Check if ATM
            is_atm = abs(strike - atm_strike) <= (atm_strike * 0.02)  # Within 2% of ATM
            
            # Calculate unusual score
            unusual_score = self._calculate_unusual_score(
                volume, oi_change, ltp_change_pct, is_atm
            )
            
            if unusual_score >= min_score:
                signal = OptionSignal(
                    symbol=symbol,
                    strike=strike,
                    option_type=option_type,
                    ltp=ltp_opt,
                    volume=volume,
                    oi=oi,
                    oi_change=oi_change,
                    oi_change_pct=oi_change_pct,
                    premium_change_pct=ltp_change_pct,
                    unusual_score=unusual_score,
                    signal_type="",  # Will be determined later
                    timestamp=datetime.now().isoformat(),
                    details={
                        "ltp_change": ltp_change,
                        "ltp_change_pct": ltp_change_pct,
                        "is_atm": is_atm,
                        "atm_strike": atm_strike,
                    }
                )
                signals.append(signal)
        
        # Determine overall signal type
        ce_signals = [s for s in signals if s.option_type == "CE"]
        pe_signals = [s for s in signals if s.option_type == "PE"]
        signal_type = self._determine_signal_type(ce_signals, pe_signals)
        
        for s in signals:
            s.signal_type = signal_type
        
        return signals
    
    def scan_all_fno(self, min_score: float = 50.0) -> list[OptionSignal]:
        """Scan all F&O stocks for unusual activity."""
        from scanner.universe import FNO_STOCKS
        
        all_signals = []
        total = len(FNO_STOCKS)
        
        print(f"\n{'='*70}")
        print(f"  SCANNING {total} F&O STOCKS FOR UNUSUAL OPTIONS ACTIVITY")
        print(f"{'='*70}\n")
        
        for idx, symbol in enumerate(FNO_STOCKS):
            name = symbol.split(":")[-1].replace("-EQ", "")
            print(f"  [{idx+1}/{total}] {name}...", end=" ", flush=True)
            
            signals = self.scan_symbol(symbol, min_score)
            if signals:
                top = max(signals, key=lambda s: s.unusual_score)
                print(f"🔥 {len(signals)} unusual! (top: {top.unusual_score:.0f})")
                all_signals.extend(signals)
            else:
                print("normal")
            
            time.sleep(0.3)  # Rate limit
        
        # Sort by unusual score
        all_signals.sort(key=lambda s: s.unusual_score, reverse=True)
        
        return all_signals
    
    def print_report(self, signals: list[OptionSignal]):
        """Print formatted unusual activity report."""
        if not signals:
            print("\n✅ No unusual options activity detected.")
            return
        
        print(f"\n{'='*90}")
        print(f"  UNUSUAL OPTIONS ACTIVITY REPORT")
        print(f"  Generated: {datetime.now().strftime('%d-%b-%Y %H:%M')}")
        print(f"{'='*90}\n")
        
        # Group by symbol
        by_symbol = {}
        for s in signals:
            if s.symbol not in by_symbol:
                by_symbol[s.symbol] = []
            by_symbol[s.symbol].append(s)
        
        for symbol, sym_signals in by_symbol.items():
            name = symbol.split(":")[-1].replace("-EQ", "")
            signal_type = sym_signals[0].signal_type
            
            emoji = "📈" if signal_type == "BULLISH" else "📉" if signal_type == "BEARISH" else "⚖️"
            
            print(f"  {emoji} {name} — {signal_type}")
            print(f"  {'Strike':>10} {'Type':>4} {'LTP':>10} {'Volume':>10} {'OI Change':>10} {'Score':>6}")
            print(f"  {'-'*10} {'-'*4} {'-'*10} {'-'*10} {'-'*10} {'-'*6}")
            
            for s in sorted(sym_signals, key=lambda x: x.unusual_score, reverse=True)[:5]:
                print(f"  {s.strike:>10.0f} {s.option_type:>4} {s.ltp:>10.2f} {s.volume:>10,} {s.oi_change:>+10,} {s.unusual_score:>5.0f}")
            
            print()
        
        # Summary
        bullish = sum(1 for s in signals if s.signal_type == "BULLISH")
        bearish = sum(1 for s in signals if s.signal_type == "BEARISH")
        neutral = sum(1 for s in signals if s.signal_type == "NEUTRAL")
        
        very_unusual = sum(1 for s in signals if s.unusual_score >= 80)
        unusual = sum(1 for s in signals if 60 <= s.unusual_score < 80)
        notable = sum(1 for s in signals if 50 <= s.unusual_score < 60)
        
        print(f"  {'='*80}")
        print(f"  SUMMARY")
        print(f"  {'='*80}")
        print(f"  Total unusual signals: {len(signals)}")
        print(f"  Very Unusual (80+): {very_unusual} | Unusual (60-80): {unusual} | Notable (50-60): {notable}")
        print(f"  Bullish: {bullish} | Bearish: {bearish} | Neutral: {neutral}")
        
        if bullish > bearish * 2:
            print(f"\n  📈 OVERALL MARKET SENTIMENT: BULLISH")
        elif bearish > bullish * 2:
            print(f"\n  📉 OVERALL MARKET SENTIMENT: BEARISH")
        else:
            print(f"\n  ⚖️ OVERALL MARKET SENTIMENT: NEUTRAL/MIXED")
        
        print(f"{'='*90}\n")
    
    def monitor(self, interval: int = 300, min_score: float = 50.0):
        """Continuously monitor for unusual activity."""
        print(f"\n🔍 Starting continuous monitoring (every {interval}s)...")
        print("Press Ctrl+C to stop\n")
        
        scan_count = 0
        while True:
            try:
                scan_count += 1
                print(f"\n--- Scan #{scan_count} at {datetime.now().strftime('%H:%M:%S')} ---")
                
                signals = self.scan_all_fno(min_score)
                if signals:
                    self.print_report(signals)
                    self.history.extend(signals)
                
                # Save to file
                self._save_history()
                
                print(f"\nNext scan in {interval}s...")
                time.sleep(interval)
                
            except KeyboardInterrupt:
                print("\n✅ Monitoring stopped.")
                break
            except Exception as e:
                print(f"❌ Error: {e}")
                time.sleep(30)
    
    def _save_history(self):
        """Save activity history to file."""
        try:
            history_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "uoa_history.json")
            data = []
            for s in self.history[-100:]:  # Keep last 100
                data.append({
                    "symbol": s.symbol,
                    "strike": s.strike,
                    "option_type": s.option_type,
                    "ltp": s.ltp,
                    "volume": s.volume,
                    "oi_change": s.oi_change,
                    "unusual_score": s.unusual_score,
                    "signal_type": s.signal_type,
                    "timestamp": s.timestamp,
                })
            with open(history_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Unusual Options Activity Tracker")
    parser.add_argument("--symbol", type=str, help="Scan specific symbol (e.g., NIFTY50)")
    parser.add_argument("--watch", action="store_true", help="Continuous monitoring")
    parser.add_argument("--score", type=float, default=50.0, help="Minimum unusual score (0-100)")
    parser.add_argument("--interval", type=int, default=300, help="Monitoring interval in seconds")
    args = parser.parse_args()
    
    tracker = UnusualOptionsTracker()
    
    if args.watch:
        tracker.monitor(args.interval, args.score)
    elif args.symbol:
        symbol = f"NSE:{args.symbol.upper()}-INDEX" if "NIFTY" in args.symbol.upper() else f"NSE:{args.symbol.upper()}-EQ"
        signals = tracker.scan_symbol(symbol, args.score)
        tracker.print_report(signals)
    else:
        signals = tracker.scan_all_fno(args.score)
        tracker.print_report(signals)


if __name__ == "__main__":
    main()
