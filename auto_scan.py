"""
Auto Scanner — runs during market hours and sends alerts.
Designed to be triggered by Windows Task Scheduler.

Scans at: 9:25 AM (post-open), 10:30 AM, 12:00 PM, 1:30 PM, 3:00 PM (pre-close)
"""

import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

from auth.login import load_env
from scanner.engine import StockScanner, format_signals_table
from scanner.aggregator import aggregate_signals, aggregated_to_dict, format_aggregated_table
from scanner.big_money import BigMoneyTracker
from alerts.telegram_bot import send_telegram_alerts
from alerts.email_alert import send_email_alerts

LOG_FILE = os.path.join(os.path.dirname(__file__), "scanner_log.txt")


def log(msg: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _check_token() -> bool:
    """Check if the Fyers token is usable — auto-refresh it first if it's old."""
    import json
    from datetime import datetime, timezone
    token_file = os.path.join(os.path.dirname(__file__), "auth", "fyers_token.json")
    if not os.path.exists(token_file):
        return False
    try:
        with open(token_file) as f:
            data = json.load(f)
        created = data.get("created_at", "")
        if not created:
            return False
        created_dt = datetime.fromisoformat(created)
        age_hours = (datetime.now(timezone.utc) - created_dt).total_seconds() / 3600
        if age_hours < 22:
            return True  # Token is still fresh

        # Token is old — try to refresh it so the scheduled scan can proceed
        try:
            from auth.login import load_env, _resolve_credentials, _refresh_access_token, _save_token
            load_env()
            client_id, secret_key, _ = _resolve_credentials()
            refreshed = _refresh_access_token(client_id, secret_key)
            if refreshed:
                _save_token(refreshed, client_id)
                log(f"Token refreshed automatically (was {age_hours:.1f}h old).")
                return True
        except Exception as e:
            log(f"Token refresh failed: {e}")
        return False
    except Exception:
        return False


def run():
    now = datetime.now()
    hour = now.hour
    minute = now.minute

    # Check if market is open (9:15 AM - 3:30 PM IST)
    market_open = (hour == 9 and minute >= 15) or (10 <= hour <= 14) or (hour == 15 and minute <= 30)
    if not market_open:
        log("Market closed. Skipping scan.")
        return

    # Check token validity
    if not _check_token():
        log("Token expired! Please run daily_login.bat to re-authenticate.")
        return

    log("Starting auto scan...")
    load_env()

    try:
        scanner = StockScanner()
        raw_signals = scanner.scan_all("daily")
        aggregated = aggregate_signals(raw_signals)
        dicts = [aggregated_to_dict(s) for s in aggregated]

        if dicts:
            log(f"Found {len(dicts)} stock(s) with signals! Sending alerts...")

            # Send Telegram
            try:
                send_telegram_alerts(dicts)
                log("Telegram alerts sent.")
            except Exception as e:
                log(f"Telegram error: {e}")

            # Send Email
            try:
                send_email_alerts(dicts)
                log("Email alerts sent.")
            except Exception as e:
                log(f"Email error: {e}")
        else:
            log("No signals found.")

        # Log results
        for s in dicts:
            log(f"  {s['strength']} {s['symbol_name']} @ {s['price']} | {s['strategy_count']} strategies | Conf: {s['confidence']}")

    except Exception as e:
        log(f"Scan error: {e}")

    # Big Money scan (stock options)
    log("Starting big money scan...")
    try:
        tracker = BigMoneyTracker(min_score=50.0)
        bm_signals = tracker.scan_all(max_stocks=None, mode="daily")
        tracker.save_results()
        log(f"Big money: {len(bm_signals)} unusual signals found")

        if bm_signals:
            # Send top 10 big money signals via Telegram
            top_bm = bm_signals[:10]
            bm_text = f"\n💰 Big Money Alert — {len(bm_signals)} unusual signals\n"
            bm_text += f"⏰ {datetime.now().strftime('%H:%M:%S')} IST\n"
            bm_text += "━" * 30 + "\n\n"
            for s in top_bm:
                emoji = "🟢" if s.signal_type == "BULLISH" else "🔴" if s.signal_type == "BEARISH" else "⚪"
                bm_text += f"{emoji} {s.symbol_name} {s.strike:.0f}{s.option_type} | Score: {s.score:.0f}\n"
                bm_text += f"   OI: {s.oi_change_pct:+.1f}% | Vol/OI: {s.vol_oi_ratio:.1f} | Prem: {s.premium_change_pct:+.1f}%\n\n"

            try:
                from alerts.telegram_bot import send_message
                send_message(bm_text)
                log("Big money Telegram alert sent.")
            except Exception as e:
                log(f"Big money Telegram error: {e}")

    except Exception as e:
        log(f"Big money scan error: {e}")

    log("Scan complete.\n")


if __name__ == "__main__":
    run()
