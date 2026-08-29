"""
Signal Tracker CLI — run scanner, log signals, and track outcomes.

Usage:
    python track_signals.py scan              # scan + log new signals
    python track_signals.py update           # update prices for active signals
    python track_signals.py report           # generate performance report
    python track_signals.py history          # show all tracked signals
    python track_signals.py clear            # clear signals older than 30 days
    python track_signals.py backtest         # run backtest + log trades
"""

import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

from login import load_env, get_fyers_client
from scanner.engine import StockScanner
from scanner.aggregator import aggregate_signals, aggregated_to_dict
from scanner.tracker import SignalTracker


def scan_and_track():
    """Run scanner and log new signals."""
    load_env()
    
    print("\n[SCAN] Running scanner...")
    scanner = StockScanner()
    raw = scanner.scan_all("daily")
    aggregated = aggregate_signals(raw)
    dicts = [aggregated_to_dict(s) for s in aggregated]
    
    tracker = SignalTracker()
    new_count = 0
    
    for d in dicts:
        signal_id = tracker.log_signal(d)
        if signal_id:
            new_count += 1
            emoji = d.get("emoji", "")
            print(f"  [LOG] {d['symbol_name']} | {d['strength']} | {d['strategy_count']} strategies")
    
    print(f"\n[DONE] Scan complete: {len(dicts)} signals found, {new_count} new logged")
    print(f"[INFO] Total tracked: {len(tracker.signals)} | Active: {len(tracker.get_active_signals())}")


def update_prices():
    """Update current prices for all active signals."""
    load_env()
    fyers = get_fyers_client()
    tracker = SignalTracker()
    active = tracker.get_active_signals()
    
    if not active:
        print("No active signals to update.")
        return
    
    print(f"\n[UPDATE] Updating prices for {len(active)} active signals...")
    
    # Get unique symbols
    symbols = list(set(s.symbol for s in active))
    
    for symbol in symbols:
        data = {
            "symbol": symbol,
            "resolution": "D",
            "date_format": 1,
            "range_from": datetime.now().strftime("%Y-%m-%d"),
            "range_to": datetime.now().strftime("%Y-%m-%d"),
            "cont_flag": 1,
        }
        try:
            resp = fyers.quotes({"symbols": symbol})
            if resp.get("s") == "ok" and resp.get("d"):
                quote = resp["d"][0]
                current_price = quote.get("v", {}).get("lp", 0)
                if current_price > 0:
                    tracker.update_price(symbol, current_price)
                    print(f"  {symbol}: Rs.{current_price:.2f}")
        except Exception as e:
            print(f"  Error updating {symbol}: {e}")
    
    # Show status changes
    active_after = tracker.get_active_signals()
    closed = [s for s in tracker.signals if s.status != "active" and s.exit_time and 
              (datetime.now() - datetime.fromisoformat(s.exit_time)).seconds < 60]
    
    if closed:
        print(f"\n[STATUS] Changes:")
        for s in closed:
            emoji = "WIN" if s.actual_outcome == "win" else "LOSS" if s.actual_outcome == "loss" else "EXIT"
            print(f"  [{emoji}] {s.symbol_name}: {s.status} | P&L: {s.pnl_pct:+.2f}%")


def show_report():
    """Show performance report."""
    tracker = SignalTracker()
    print(tracker.generate_report())


def show_history():
    """Show all tracked signals."""
    tracker = SignalTracker()
    signals = tracker.signals
    
    if not signals:
        print("No signals tracked yet.")
        return
    
    print(f"\n{'='*110}")
    print(f"  SIGNAL HISTORY — {len(signals)} total")
    print(f"{'='*110}")
    print(f"  {'Symbol':<10} {'Type':<6} {'Strength':<15} {'Strategy':<25} {'Entry':>10} {'P&L':>8} {'Status':<12} {'Date'}")
    print(f"  {'─'*106}")
    
    for s in sorted(signals, key=lambda x: x.signal_time, reverse=True):
        pnl_str = f"{s.pnl_pct:+.2f}%" if s.pnl_pct is not None else "N/A"
        date_str = datetime.fromisoformat(s.signal_time).strftime("%Y-%m-%d %H:%M")
        print(
            f"  {s.symbol_name:<10} {s.signal_type:<6} {s.strength:<15} {s.strategy:<25} "
            f"{s.entry_price:>10.2f} {pnl_str:>8} {s.status:<12} {date_str}"
        )
    
    print(f"{'='*110}")


def clear_old():
    """Clear old signals."""
    tracker = SignalTracker()
    before = len(tracker.signals)
    tracker.clear_old_signals(days=30)
    after = len(tracker.signals)
    print(f"Cleared {before - after} signals older than 30 days. {after} signals remaining.")


def run_backtest():
    """Run backtest and log trades as tracked signals."""
    from scanner.backtest import Backtester
    
    load_env()
    bt = Backtester()
    tracker = SignalTracker()
    
    # Test on major stocks
    stocks = ["NSE:TCS-EQ", "NSE:RELIANCE-EQ", "NSE:SBIN-EQ", "NSE:HDFCBANK-EQ", "NSE:INFY-EQ"]
    
    print("\n[BACKTEST] Running on 5 major stocks...")
    
    for symbol in stocks:
        results = bt.run(symbol, days=252, hold_days=10)
        for strat_name, result in results.items():
            for trade in result.trades:
                # Log backtest trade
                signal_dict = {
                    "symbol": trade.symbol,
                    "symbol_name": symbol.split(":")[1].replace("-EQ", ""),
                    "strategy": trade.strategy,
                    "signal_type": trade.signal_type,
                    "strength": "BUY" if trade.signal_type == "BUY" else "SELL",
                    "price": trade.entry_price,
                    "stop_loss": trade.entry_price * (0.97 if trade.signal_type == "BUY" else 1.03),
                    "target": trade.entry_price * (1.05 if trade.signal_type == "BUY" else 0.95),
                    "confidence": 0.7,
                    "timeframe": "daily",
                    "reasons": [f"Backtest trade: {trade.entry_date} to {trade.exit_date}"],
                }
                tracker.log_signal(signal_dict)
                
                # Update with actual outcome
                for s in tracker.signals:
                    if s.symbol == trade.symbol and s.status == "active":
                        s.status = "hit_target" if trade.hit_target else ("hit_sl" if trade.hit_sl else "expired")
                        s.exit_price = trade.exit_price
                        s.exit_time = trade.exit_date
                        s.pnl_pct = trade.pnl_pct
                        s.actual_outcome = "win" if trade.pnl_pct > 0 else "loss"
                        break
                
                tracker._save_signals()
    
    print(f"\n[DONE] Backtest complete: {len(tracker.signals)} trades logged")
    print(tracker.generate_report())


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    
    command = sys.argv[1].lower()
    
    if command == "scan":
        scan_and_track()
    elif command == "update":
        update_prices()
    elif command == "report":
        show_report()
    elif command == "history":
        show_history()
    elif command == "clear":
        clear_old()
    elif command == "backtest":
        run_backtest()
    else:
        print(f"Unknown command: {command}")
        print(__doc__)
