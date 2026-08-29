"""
Fyers Live Watchlist — Real-time Quotes via WebSocket
=====================================================
Streams live tick data for symbols defined in watchlist.json using the
Fyers Data Socket.

Usage:
    python watchlist.py                  # stream all symbols in watchlist
    python watchlist.py NSE:TATAMOTORS-EQ  # stream specific symbol(s)

Configuration:
    Edit watchlist.json to add/remove symbols.
    "litemode": true  → LTP-only updates (lighter payload)
    "depth_symbols": ["NSE:SBIN-EQ"] → market depth for specific symbols

Requirements:
    - Run `python login.py` first to authenticate
    - fyers-apiv3 installed (pip install fyers-apiv3)
"""

import json
import os
import signal
import sys
import time
from datetime import datetime

from fyers_apiv3.FyersWebsocket import data_ws
from auth.login import _load_saved_token, get_fyers_client, load_env, FYERS_LOG_DIR

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), "data", "watchlist.json")

# Fields returned by SymbolUpdate ticks (full mode)
TICK_FIELDS = [
    "symbol", "ltp", "open", "high", "low", "prev_close",
    "volume", "atp", "oi", "ttq", "bid", "ask",
    "bid_size", "ask_size", "last_traded_time", "timestamp",
]

# ---------------------------------------------------------------------------
# Watchlist loading
# ---------------------------------------------------------------------------

def load_watchlist(extra_symbols: list[str] | None = None) -> dict:
    """
    Load the watchlist from watchlist.json, optionally appending extra symbols.
    Returns {"symbols": [...], "litemode": bool, "depth_symbols": [...]}.
    """
    data = {"watchlist": [], "litemode": False, "depth_symbols": []}
    if os.path.exists(WATCHLIST_FILE):
        with open(WATCHLIST_FILE) as f:
            data = json.load(f)

    symbols = list(data.get("watchlist", []))
    if extra_symbols:
        for s in extra_symbols:
            if s not in symbols:
                symbols.append(s)
    data["watchlist"] = symbols
    return data


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

# ANSI colors
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"

# Track previous LTP for arrow display
_prev_ltp: dict[str, float] = {}


def _color_change(current: float, previous: float) -> str:
    """Return ANSI-colored string with up/down arrow."""
    if previous == 0:
        return f"{current:>10.2f}"
    diff = current - previous
    pct = (diff / previous) * 100
    if diff > 0:
        return f"{GREEN}{current:>10.2f} ▲{pct:+.2f}%{RESET}"
    elif diff < 0:
        return f"{RED}{current:>10.2f} ▼{pct:+.2f}%{RESET}"
    return f"{current:>10.2f}"


def _format_symbol(symbol: str) -> str:
    """Shorten symbol for display: NSE:SBIN-EQ → SBIN."""
    return symbol.split(":")[-1].replace("-EQ", "").replace("-BE", "")


def _print_header():
    """Print the table header."""
    print()
    print(f"{BOLD}{CYAN}{'Symbol':<10} {'LTP':>12} {'Open':>10} {'High':>10} {'Low':>10} {'Prev':>10} {'Volume':>12} {'Bid':>10} {'Ask':>10}{RESET}")
    print(f"{DIM}{'─' * 106}{RESET}")


def _print_tick(msg: dict) -> None:
    """Pretty-print a SymbolUpdate tick."""
    symbol = msg.get("symbol", "???")
    ltp = msg.get("ltp", 0)
    prev = msg.get("prev_close", 0)

    short = _format_symbol(symbol)
    ltp_str = _color_change(ltp, _prev_ltp.get(symbol, prev))

    vol = msg.get("ttq", msg.get("volume", 0))
    vol_str = f"{vol:>12,}" if isinstance(vol, (int, float)) else f"{vol:>12}"

    print(
        f"{short:<10} {ltp_str} "
        f"{msg.get('open', 0):>10.2f} "
        f"{msg.get('high', 0):>10.2f} "
        f"{msg.get('low', 0):>10.2f} "
        f"{prev:>10.2f} "
        f"{vol_str} "
        f"{msg.get('bid', 0):>10.2f} "
        f"{msg.get('ask', 0):>10.2f}"
    )
    _prev_ltp[symbol] = ltp


def _print_depth(msg: dict) -> None:
    """Pretty-print a DepthUpdate tick."""
    symbol = msg.get("symbol", "???")
    short = _format_symbol(symbol)
    print(f"\n{BOLD}{YELLOW}Depth: {short}{RESET}")
    depth = msg.get("depth", {})
    bids = depth.get("buy", [])
    asks = depth.get("sell", [])

    print(f"  {CYAN}{'BID':>12} {'Size':>8}   {'ASK':>12} {'Size':>8}{RESET}")
    for i in range(max(len(bids), len(asks))):
        b = bids[i] if i < len(bids) else {}
        a = asks[i] if i < len(asks) else {}
        print(
            f"  {GREEN}{b.get('price', 0):>12.2f} {b.get('qty', 0):>8}{RESET}"
            f"   "
            f"{RED}{a.get('price', 0):>12.2f} {a.get('qty', 0):>8}{RESET}"
        )


# ---------------------------------------------------------------------------
# WebSocket callbacks
# ---------------------------------------------------------------------------

_socket: data_ws.FyersDataSocket | None = None


def on_open():
    """Called when WebSocket connects — subscribe to symbols."""
    global _socket
    print(f"{GREEN}WebSocket connected{RESET} — subscribing to symbols...")

    watchlist = load_watchlist()
    symbols = watchlist["watchlist"]
    depth_symbols = watchlist.get("depth_symbols", [])

    if symbols:
        _socket.subscribe(symbols=symbols, data_type="SymbolUpdate")
        print(f"  Subscribed to {len(symbols)} symbol(s): {', '.join(_format_symbol(s) for s in symbols)}")

    if depth_symbols:
        _socket.subscribe(symbols=depth_symbols, data_type="DepthUpdate")
        print(f"  Subscribed to depth for {len(depth_symbols)} symbol(s)")

    _socket.keep_running()


def on_message(msg: dict):
    """Handle incoming tick data."""
    msg_type = msg.get("type", "")

    if msg_type == "if" or "symbol" in msg:
        _print_tick(msg)
    elif msg_type == "depth" or "depth" in msg:
        _print_depth(msg)
    else:
        # Unknown message — print raw
        print(f"{DIM}Raw: {json.dumps(msg, default=str)[:200]}{RESET}")


def on_error(msg: dict):
    """Handle WebSocket errors."""
    print(f"{RED}WebSocket error: {msg}{RESET}")


def on_close(msg: dict):
    """Handle WebSocket close."""
    print(f"{YELLOW}WebSocket closed: {msg}{RESET}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    load_env()

    # Allow passing symbols as CLI args
    extra = sys.argv[1:] if len(sys.argv) > 1 else None

    watchlist = load_watchlist(extra)
    symbols = watchlist["watchlist"]

    if not symbols:
        print("No symbols to watch. Edit watchlist.json or pass symbols as arguments.")
        print("Example: python watchlist.py NSE:TATAMOTORS-EQ NSE:WIPRO-EQ")
        sys.exit(1)

    # Get access token — auto-refreshes via login.py if needed
    load_env()
    saved = _load_saved_token()
    if not saved:
        print("No saved token found. Running login flow first...\n")
        get_fyers_client()
        saved = _load_saved_token()

    if not saved:
        print("ERROR: Could not obtain access token.")
        sys.exit(1)

    access_token = f"{saved['client_id']}:{saved['access_token']}"

    # Build symbol list for display
    all_symbols = list(symbols)
    depth_symbols = watchlist.get("depth_symbols", [])
    for s in depth_symbols:
        if s not in all_symbols:
            all_symbols.append(s)

    print(f"\n{BOLD}Live Watchlist{RESET}")
    print(f"  Symbols   : {', '.join(_format_symbol(s) for s in all_symbols)}")
    print(f"  Lite mode : {watchlist.get('litemode', False)}")
    print(f"  Press Ctrl+C to stop\n")

    _print_header()

    # Create and connect the data socket
    global _socket
    _socket = data_ws.FyersDataSocket(
        access_token=access_token,
        log_path=FYERS_LOG_DIR,
        litemode=watchlist.get("litemode", False),
        write_to_file=False,
        reconnect=True,
        on_connect=on_open,
        on_close=on_close,
        on_error=on_error,
        on_message=on_message,
    )

    # Graceful shutdown
    def _shutdown(sig, frame):
        print(f"\n{YELLOW}Disconnecting...{RESET}")
        try:
            _socket.disconnect()
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    _socket.connect()


if __name__ == "__main__":
    main()
