"""
Telegram Bot Alerts — sends trading signals to your Telegram chat.

Setup:
    1. Create a bot via @BotFather on Telegram
    2. Get your bot token
    3. Get your chat ID (message the bot, then visit https://api.telegram.org/bot<TOKEN>/getUpdates)
    4. Add to .env:
        TELEGRAM_BOT_TOKEN=your_bot_token
        TELEGRAM_CHAT_ID=your_chat_id
"""

import os
import ssl
import json
from urllib.request import Request, urlopen
from urllib.error import URLError

from auth.login import load_env


# Handle SSL verification issues on some Windows setups
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


def _get_config() -> tuple[str, str]:
    load_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    return token, chat_id


def _format_signal_message(signal: dict) -> str:
    """Format a single signal into a Telegram-friendly message."""
    # Handle both raw and aggregated signal formats
    strength = signal.get("strength", signal.get("signal_type", "BUY"))
    emoji = signal.get("emoji", "🟢" if signal["signal_type"] == "BUY" else "🔴")

    # Strategy names
    if "strategies" in signal:
        strat_list = ", ".join(signal["strategies"])
        strat_count = signal.get("strategy_count", len(signal["strategies"]))
    else:
        strat_list = signal.get("strategy", "")
        strat_count = 1

    # Reasons
    if "reasons" in signal:
        reasons = "\n".join(f"  • {r[:70]}" for r in signal["reasons"])
    else:
        reasons = signal.get("reason", "")

    msg = (
        f"{emoji} *{strength}* | *{signal['symbol_name']}* @ ₹{signal['price']:.2f}\n"
        f"📊 {strat_count} strategy(ies): {strat_list}\n"
        f"🎯 Target: ₹{signal['target']:.2f} | 🛑 SL: ₹{signal['stop_loss']:.2f}\n"
        f"📈 Confidence: {signal['confidence']:.0%}\n"
        f"📝 {reasons}"
    )
    return msg


def send_telegram_alerts(signals: list[dict], bot_token: str = "", chat_id: str = "") -> bool:
    """Send a batch of signal alerts to Telegram."""
    if not bot_token or not chat_id:
        bot_token, chat_id = _get_config()

    if not bot_token or not chat_id:
        print("Telegram not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
        return False

    header = f"🚨 *Stock Scanner Alert* — {len(signals)} signal(s)\n{'━' * 30}\n\n"
    messages = [_format_signal_message(s) for s in signals]
    full_msg = header + "\n\n".join(messages)

    # Telegram max message length is 4096
    if len(full_msg) > 4000:
        # Send individually
        success = True
        for s in messages:
            if not _send_message(bot_token, chat_id, s):
                success = False
        return success
    else:
        return _send_message(bot_token, chat_id, full_msg)


def _send_message(bot_token: str, chat_id: str, text: str) -> bool:
    """Send a single message via Telegram Bot API."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    # Telegram chat_id must be integer for private chats
    try:
        chat_id_int = int(chat_id)
    except (ValueError, TypeError):
        chat_id_int = chat_id
    payload = json.dumps({
        "chat_id": chat_id_int,
        "text": text,
    }).encode("utf-8")

    try:
        req = Request(url, data=payload, headers={"Content-Type": "application/json"})
        resp = urlopen(req, timeout=10, context=_ssl_ctx)
        result = json.loads(resp.read())
        return result.get("ok", False)
    except URLError as e:
        print(f"Telegram error: {e}")
        return False
