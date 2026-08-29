"""
CLI Scanner — run the stock scanner from command line.

Usage:
    python scan.py                    # scan all F&O + indices (daily)
    python scan.py --timeframe 15min  # scan 15-min timeframe
    python scan.py --both             # scan both daily + 15min
    python scan.py --json             # output as JSON
    python scan.py --raw              # show individual signals (before aggregation)
    python scan.py --symbols NSE:SBIN-EQ NSE:RELIANCE-EQ  # scan specific symbols
    python scan.py --alerts           # scan + send Telegram/Email alerts
"""

import argparse
import json
import sys

from scanner.engine import StockScanner, format_signals_table
from scanner.aggregator import (
    aggregate_signals, format_aggregated_table,
    aggregated_to_dict,
)


def main():
    parser = argparse.ArgumentParser(description="Stock Scanner with Signal Strength")
    parser.add_argument("--timeframe", "-t", choices=["daily", "15min", "5min"], default="daily", help="Timeframe to scan")
    parser.add_argument("--both", action="store_true", help="Scan both daily and 15min timeframes")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    parser.add_argument("--raw", action="store_true", help="Show individual signals before aggregation")
    parser.add_argument("--symbols", nargs="+", help="Specific symbols to scan (e.g. NSE:SBIN-EQ)")
    parser.add_argument("--alerts", action="store_true", help="Send alerts via Telegram/Email for any signals found")
    args = parser.parse_args()

    scanner = StockScanner(symbols=args.symbols)
    all_raw = []

    if args.both:
        results = scanner.scan_both_timeframes()
        for tf, signals in results.items():
            all_raw.extend(signals)
    else:
        all_raw = scanner.scan_all(args.timeframe)

    # Aggregate signals by stock
    aggregated = aggregate_signals(all_raw)

    # Display
    if args.raw:
        print(format_signals_table(all_raw))
    else:
        print(format_aggregated_table(aggregated))

    # JSON output
    if args.json:
        output = [aggregated_to_dict(s) for s in aggregated]
        print(json.dumps(output, indent=2))

    # Alerts (use aggregated signals)
    if args.alerts and aggregated:
        try:
            from alerts.telegram_bot import send_telegram_alerts
            from alerts.email_alert import send_email_alerts
            dicts = [aggregated_to_dict(s) for s in aggregated]
            send_telegram_alerts(dicts)
            send_email_alerts(dicts)
            print("Alerts sent!")
        except Exception as e:
            print(f"Alert error: {e}")

    # Summary
    strong = [s for s in aggregated if "STRONG" in s.strength.value]
    print(f"\nTotal: {len(aggregated)} stock(s) | {len(strong)} strong signal(s)")


if __name__ == "__main__":
    main()
