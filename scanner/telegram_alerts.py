"""
Real-Time Telegram Alerts
=========================
Sends alerts when NEW signals appear during market hours.
Tracks sent signals to avoid duplicate alerts.

Features:
- Only sends alerts for NEW signals (not repeated ones)
- Groups multiple new signals into one message
- Daily summary at market close (3:30 PM IST)
- Market hours check (9:15 AM - 3:30 PM IST)
- Configurable via .env

Usage:
    from scanner.telegram_alerts import TelegramAlerts
    
    alerts = TelegramAlerts()
    alerts.check_and_send(new_signals)  # Sends only new signals
"""

import json
import os
import ssl
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.request import Request, urlopen
from urllib.error import URLError

# SSL context for Windows
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE

IST = timezone(timedelta(hours=5, minutes=30))

# Track sent signals to avoid duplicates
SENT_SIGNALS_FILE = "sent_alerts.json"


class TelegramAlerts:
    """Real-time Telegram alert system for scanner signals."""
    
    def __init__(self):
        self._load_config()
        self._load_sent_signals()
    
    def _load_config(self):
        """Load Telegram credentials from .env."""
        env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "auth", ".env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, _, value = line.partition("=")
                        os.environ.setdefault(key.strip(), value.strip())
        
        self.bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        self.enabled = bool(self.bot_token and self.chat_id)
    
    def _load_sent_signals(self):
        """Load previously sent signal IDs. Reset daily."""
        today = datetime.now(IST).strftime("%Y-%m-%d")
        try:
            if os.path.exists(SENT_SIGNALS_FILE):
                with open(SENT_SIGNALS_FILE) as f:
                    data = json.load(f)
                saved_date = data.get("date", "")
                
                # Daily reset: if saved date is different from today, clear all
                if saved_date != today:
                    print(f"Telegram: New day ({today}) — resetting sent signals.")
                    self.sent_signals = set()
                    self.last_summary = ""
                    self.first_scan_today = True
                else:                self.sent_signals = set(data.get("sent", []))
                self.last_summary = data.get("last_summary", "")
                self.first_scan_today = data.get("first_scan_done", False)
                self.sent_signals_meta = data.get("meta", {})
            else:
                self.sent_signals = set()
                self.last_summary = ""
                self.first_scan_today = False
                self.sent_signals_meta = {}
        except Exception:
            self.sent_signals = set()
            self.last_summary = ""
            self.first_scan_today = False
            self.sent_signals_meta = {}
    
    def _save_sent_signals(self):
        """Save sent signal IDs to disk."""
        today = datetime.now(IST).strftime("%Y-%m-%d")
        
        # Keep only last 500 signals to prevent file bloat
        if len(self.sent_signals) > 500:
            self.sent_signals = set(list(self.sent_signals)[-500:])
        
        with open(SENT_SIGNALS_FILE, "w") as f:
            json.dump({
                "date": today,
                "sent": list(self.sent_signals),
                "last_summary": self.last_summary,
                "first_scan_done": self.first_scan_today,
                "meta": self.sent_signals_meta,
                "updated_at": datetime.now(IST).isoformat(),
            }, f, indent=2)
    
    def _signal_id(self, signal: dict) -> str:
        """Generate a unique ID for a signal."""
        sym = signal.get("symbol", signal.get("symbol_name", "?"))
        sig_type = signal.get("signal_type", "?")
        strategies = ",".join(sorted(signal.get("strategies", [])))
        return f"{sym}:{sig_type}:{strategies}"
    
    def _stock_key(self, signal: dict) -> str:
        """Generate a stock-only key (without strategies) for comparison."""
        sym = signal.get("symbol", signal.get("symbol_name", "?"))
        sig_type = signal.get("signal_type", "?")
        return f"{sym}:{sig_type}"
    
    def is_market_hours(self) -> bool:
        """Check if current time is within market hours (9:15 AM - 3:30 PM IST)."""
        now = datetime.now(IST)
        market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
        market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
        return market_open <= now <= market_close
    
    def is_market_open_day(self) -> bool:
        """Check if today is a trading day (Mon-Fri, not checking holidays)."""
        now = datetime.now(IST)
        return now.weekday() < 5  # Monday=0, Friday=4
    
    def _format_signal(self, signal: dict) -> str:
        """Format a signal for Telegram."""
        sig_type = signal.get("signal_type", "BUY")
        emoji = "🟢" if sig_type == "BUY" else "🔴"
        strength = signal.get("strength", sig_type)
        
        # Strategy list
        strategies = signal.get("strategies", [])
        strat_count = signal.get("strategy_count", len(strategies))
        strat_list = ", ".join(strategies[:3])
        if len(strategies) > 3:
            strat_list += f" +{len(strategies)-3}"
        
        # Get symbol name
        sym = signal.get("symbol_name", signal.get("symbol", "?"))
        if not sym or sym == "?":
            sym = signal.get("symbol", "?").replace("-EQ", "").replace("NSE:", "")
        
        price = signal.get("price", 0)
        sl = signal.get("stop_loss", 0)
        target = signal.get("target", 0)
        conf = signal.get("confidence", 0)
        
        # R:R ratio
        if sig_type == "BUY" and sl > 0 and target > 0:
            risk = price - sl
            reward = target - price
            rr = f"{reward/risk:.1f}" if risk > 0 else "—"
        elif sig_type == "SELL" and sl > 0 and target > 0:
            risk = sl - price
            reward = price - target
            rr = f"{reward/risk:.1f}" if risk > 0 else "—"
        else:
            rr = "—"
        
        msg = (
            f"{emoji} *{strength}* | *{sym}* @ ₹{price:.2f}\n"
            f"📊 {strat_count} strategy(ies): {strat_list}\n"
            f"🎯 Target: ₹{target:.2f} | 🛑 SL: ₹{sl:.2f}\n"
            f"📈 Confidence: {conf:.0f}% | R:R 1:{rr}"
        )
        return msg
    
    def _is_scheduled_time(self) -> bool:
        """Check if current time is a scheduled full-list time (9:15, 12:00, 14:00)."""
        now = datetime.now(IST)
        scheduled_times = [
            (9, 15),   # Morning open
            (12, 0),   # Noon
            (14, 0),   # Afternoon
        ]
        for h, m in scheduled_times:
            scheduled = now.replace(hour=h, minute=m, second=0, microsecond=0)
            # Within 5 minutes of scheduled time
            if abs((now - scheduled).total_seconds()) < 300:
                return True
        return False
    
    def _get_schedule_slot(self) -> str:
        """Return which scheduled slot we're in."""
        now = datetime.now(IST)
        hour = now.hour
        if hour < 12:
            return "morning"
        elif hour < 14:
            return "noon"
        else:
            return "afternoon"
    
    def check_and_send(self, signals: list[dict]) -> int:
        """
        Check for new signals and send alerts.
        Returns number of alerts sent.
        
        Daily behavior:
        - Scheduled times (9:15, 12:00, 14:00): Send FULL list
        - Between schedules: Only send NEW stocks or strategy updates
        - Next day: Reset and send full lists again
        """
        if not self.enabled:
            return 0
        
        now = datetime.now(IST)
        schedule_slot = self._get_schedule_slot()
        
        # Check if it's a scheduled full-list time
        is_scheduled = self._is_scheduled_time()
        last_slot = self.sent_signals_meta.get("last_full_list_slot", "")
        
        # Send full list at scheduled times (9:15, 12:00, 14:00)
        if is_scheduled and last_slot != schedule_slot:
            self.sent_signals_meta["last_full_list_slot"] = schedule_slot
            if signals:
                slot_names = {"morning": "🌅 Morning", "noon": "☀️ Noon", "afternoon": "🌆 Afternoon"}
                slot_name = slot_names.get(schedule_slot, "📊")
                
                header = f"{slot_name} Full List — {len(signals)} stocks\n"
                header += f"⏰ {now.strftime('%H:%M:%S IST')}\n"
                header += f"{'━' * 30}\n\n"
                
                # Sort: BUY first, then by strategy count
                sorted_signals = sorted(signals, key=lambda x: (
                    0 if x.get('signal_type') == 'BUY' else 1,
                    -x.get('strategy_count', 0)
                ))
                
                messages = [self._format_signal(s) for s in sorted_signals]
                full_msg = header + "\n\n".join(messages)
                
                # Mark all as sent
                for s in signals:
                    self.sent_signals.add(self._signal_id(s))
                
                # Split if too long
                if len(full_msg) > 4000:
                    sent = 0
                    for msg in messages:
                        if self._send(msg):
                            sent += 1
                    self._save_sent_signals()
                    return sent
                else:
                    success = self._send(full_msg)
                    self._save_sent_signals()
                    return len(signals) if success else 0
        
        # Between scheduled times → find NEW signals and STRATEGY UPDATES
        new_signals = []
        strategy_updates = []
        
        # Track which strategies each stock has seen
        stock_strategies = {}  # stock_key -> set of strategies seen
        
        for s in signals:
            sid = self._signal_id(s)
            sk = self._stock_key(s)
            strategies = set(s.get("strategies", []))
            
            if sid not in self.sent_signals:
                # Check if we've seen this stock before (with different strategies)
                if sk in stock_strategies:
                    prev_strats = stock_strategies[sk]
                    new_strats = strategies - prev_strats
                    if new_strats:
                        # Stock gained new strategies!
                        strategy_updates.append((s, new_strats, prev_strats))
                
                new_signals.append(s)
                self.sent_signals.add(sid)
                
                # Track strategies for this stock
                if sk not in stock_strategies:
                    stock_strategies[sk] = set()
                stock_strategies[sk].update(strategies)
        
        if not new_signals and not strategy_updates:
            return 0
        
        # Build message
        messages = []
        
        # New stocks
        if new_signals:
            header = f"🚨 *New Signals Detected!* — {len(new_signals)} new\n"
            header += f"⏰ {datetime.now(IST).strftime('%H:%M:%S IST')}\n"
            header += f"{'━' * 30}\n\n"
            messages.extend([self._format_signal(s) for s in new_signals])
        
        # Strategy updates (existing stock gained new strategy)
        if strategy_updates:
            if not new_signals:
                header = f"📈 *Strategy Updates!* — {len(strategy_updates)} stock(s) upgraded\n"
                header += f"⏰ {datetime.now(IST).strftime('%H:%M:%S IST')}\n"
                header += f"{'━' * 30}\n\n"
            
            for s, new_strats, prev_strats in strategy_updates:
                sym = s.get("symbol_name", s.get("symbol", "?"))
                sig_type = s.get("signal_type", "BUY")
                emoji = "🟢" if sig_type == "BUY" else "🔴"
                msg = (
                    f"{emoji} *{sym}* — Strategy Added!\n"
                    f"   ➕ New: {', '.join(new_strats)}\n"
                    f"   📊 Total: {len(s.get('strategies', []))} strategies now\n"
                    f"   💡 Stronger signal — more confirmation"
                )
                messages.append(msg)
        
        full_msg = header + "\n\n".join(messages)
        
        # Split if too long (Telegram limit: 4096 chars)
        if len(full_msg) > 4000:
            sent = 0
            for msg in messages:
                if self._send(msg):
                    sent += 1
            self._save_sent_signals()
            return sent
        else:
            success = self._send(full_msg)
            self._save_sent_signals()
            return len(new_signals) + len(strategy_updates) if success else 0
    
    def send_daily_summary(self, signals: list[dict]) -> bool:
        """Send end-of-day summary with all signals."""
        if not self.enabled:
            return False
        
        now = datetime.now(IST)
        today = now.strftime("%Y-%m-%d")
        
        # Don't send twice on same day
        if self.last_summary == today:
            return False
        
        buy_signals = [s for s in signals if s.get("signal_type") == "BUY"]
        sell_signals = [s for s in signals if s.get("signal_type") == "SELL"]
        
        # Count by strategy
        from collections import Counter
        strat_counts = Counter()
        for s in signals:
            for strat in s.get("strategies", []):
                strat_counts[strat] += 1
        
        msg = f"📊 *Daily Scanner Summary*\n"
        msg += f"📅 {today}\n"
        msg += f"{'━' * 30}\n\n"
        msg += f"📈 *Total Signals:* {len(signals)}\n"
        msg += f"🟢 *BUY:* {len(buy_signals)} | 🔴 *SELL:* {len(sell_signals)}\n\n"
        
        # Very strong signals
        very_strong = [s for s in signals if s.get("strategy_count", 0) >= 3]
        if very_strong:
            msg += f"💎 *Very Strong ({len(very_strong)}):*\n"
            for s in very_strong[:5]:
                sym = s.get("symbol_name", s.get("symbol", "?"))
                sig = s.get("signal_type", "?")
                emoji = "🟢" if sig == "BUY" else "🔴"
                msg += f"  {emoji} {sym} — {', '.join(s.get('strategies', [])[:2])}\n"
            msg += "\n"
        
        # Strategy breakdown
        msg += "📊 *Strategy Breakdown:*\n"
        for strat, count in strat_counts.most_common(8):
            msg += f"  • {strat}: {count}\n"
        
        # Top 5 by confidence
        top5 = sorted(signals, key=lambda x: x.get("confidence", 0), reverse=True)[:5]
        msg += "\n🏆 *Top 5 by Confidence:*\n"
        for s in top5:
            sym = s.get("symbol_name", s.get("symbol", "?"))
            sig = s.get("signal_type", "?")
            emoji = "🟢" if sig == "BUY" else "🔴"
            conf = s.get("confidence", 0)
            msg += f"  {emoji} {sym} — {conf:.0f}%\n"
        
        success = self._send(msg)
        if success:
            self.last_summary = today
            self._save_sent_signals()
        return success
    
    def send_test_message(self) -> bool:
        """Send a test message to verify Telegram is working."""
        if not self.enabled:
            print("Telegram not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
            return False
        
        msg = (
            "✅ *JBK Scanner — Telegram Connected!*\n\n"
            f"⏰ Time: {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}\n"
            f"🤖 Bot: @JBK21_bot\n"
            f"👤 Chat: {self.chat_id}\n\n"
            "You will receive:\n"
            "• 🚨 Real-time alerts for new signals\n"
            "• 📊 Daily summary at market close\n"
            "• 🔔 Only NEW signals (no duplicates)"
        )
        return self._send(msg)
    
    def _send(self, text: str) -> bool:
        """Send a message via Telegram Bot API."""
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        # Telegram chat_id must be integer for private chats
        try:
            chat_id = int(self.chat_id)
        except (ValueError, TypeError):
            chat_id = self.chat_id
        payload = json.dumps({
            "chat_id": chat_id,
            "text": text,
        }).encode("utf-8")
        
        try:
            req = Request(url, data=payload, headers={"Content-Type": "application/json"})
            resp = urlopen(req, timeout=15, context=_ssl_ctx)
            result = json.loads(resp.read())
            return result.get("ok", False)
        except URLError as e:
            print(f"Telegram error: {e}")
            return False
        except Exception as e:
            print(f"Telegram error: {e}")
            return False


# Singleton instance
_alerts: Optional[TelegramAlerts] = None


def get_alerts() -> TelegramAlerts:
    """Get the singleton TelegramAlerts instance."""
    global _alerts
    if _alerts is None:
        _alerts = TelegramAlerts()
    return _alerts


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Telegram Alerts")
    parser.add_argument("--test", action="store_true", help="Send test message")
    parser.add_argument("--summary", action="store_true", help="Send daily summary")
    args = parser.parse_args()
    
    alerts = get_alerts()
    
    if args.test:
        print("Sending test message...")
        if alerts.send_test_message():
            print("✅ Test message sent successfully!")
        else:
            print("❌ Failed to send test message.")
    elif args.summary:
        print("Sending daily summary...")
        # Load latest signals
        try:
            with open("last_scan.json") as f:
                data = json.load(f)
            signals = data.get("signals", [])
            if alerts.send_daily_summary(signals):
                print("✅ Daily summary sent!")
            else:
                print("❌ Failed or already sent today.")
        except FileNotFoundError:
            print("No scan data found. Run a scan first.")
    else:
        print("Usage:")
        print("  python -m scanner.telegram_alerts --test      # Test connection")
        print("  python -m scanner.telegram_alerts --summary   # Send daily summary")
