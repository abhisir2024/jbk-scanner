"""
Stock Scanner Web Dashboard v2
================================
Modern web app for viewing scanner signals with advanced features.

Features:
- Summary cards (signal counts, market status, top picks)
- Search by stock name/symbol
- Filter by signal type and strategy
- Sortable columns (click headers)
- Click row to expand stock details
- CSV export
- Keyboard shortcuts (S=scan, F=focus search, Esc=close)
- Auto-refresh every 15s
- Responsive design

Usage:
    python dashboard.py              # start on http://localhost:5001
    python dashboard.py --port 8080  # custom port
"""

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(__file__))

from scanner.engine import StockScanner
from scanner.aggregator import aggregate_signals, aggregated_to_dict
from scanner.universe import get_symbol_name
from scanner.strategies import _ema, _rsi
from scanner.movers import (
    refresh_movers, analyze_future, SYMBOL_UNIVERSE, get_symbol_name as _movers_name,
)
from scanner.rate_limiter import get_limiter

# ---------------------------------------------------------------------------
# Static file cache (loaded once at startup, served with gzip)
# ---------------------------------------------------------------------------
_STATIC_CACHE: dict[str, bytes] = {}


def _load_static_once():
    """Load static assets into memory once for fast serving."""
    import gzip as _gzip
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    for fname in ("index.html", "app.js", "style.css"):
        fp = os.path.join(static_dir, fname)
        try:
            with open(fp, "r", encoding="utf-8") as f:
                raw = f.read().encode("utf-8")
            # Only gzip if it actually shrinks
            gz = _gzip.compress(raw, 6)
            _STATIC_CACHE[fname] = gz if len(gz) < len(raw) else raw
        except FileNotFoundError:
            pass


_load_static_once()


# ---------------------------------------------------------------------------
# Scanner state
# ---------------------------------------------------------------------------
_last_scan_result: dict = {"signals": [], "last_scan": None, "scanning": False}
_lock = threading.Lock()
# Auto-scan is ON by default so the dashboard refreshes itself every 5 minutes
# during market hours without needing a manual toggle in the UI.
_auto_scan_enabled = True
_auto_scan_thread: threading.Thread | None = None
_backtest_status: dict = {"running": False}

# ---------------------------------------------------------------------------
# F&O Movers state (top gainers / losers — cash segment)
# ---------------------------------------------------------------------------
_movers_state: dict = {
    "gainers": [], "losers": [], "all": [],
    "updated": None, "total": 0,
    "signal_buys": 0, "signal_sells": 0,
}
_movers_lock = threading.Lock()
_movers_fyers = None
_movers_analysis_cache: dict = {}
_movers_stop = False

# Shared scanner for on-demand chart history. Reused across requests so we
# avoid re-running get_fyers_client() (which makes a get_profile() API call)
# on every chart expand. History calls are rate-limited via get_limiter().
_history_scanner = None


def _get_history_scanner():
    """Lazily create and cache the scanner used by /api/history."""
    global _history_scanner
    if _history_scanner is None:
        _history_scanner = StockScanner()
    return _history_scanner


def _init_movers_fyers():
    global _movers_fyers
    if _movers_fyers is None:
        from auth.login import _load_saved_token, load_env, FYERS_LOG_DIR
        load_env()
        from fyers_apiv3 import fyersModel
        saved = _load_saved_token()
        if saved:
            _movers_fyers = fyersModel.FyersModel(
                token=saved["access_token"], is_async=False,
                client_id=saved.get("client_id", ""), log_path=FYERS_LOG_DIR,
            )


def _refresh_movers_once():
    global _movers_fyers
    try:
        _init_movers_fyers()
        if _movers_fyers is None:
            return
        rows = refresh_movers(_movers_fyers)
        gainers = rows[:10]
        losers = rows[-10:][::-1]
        signal_buys = sum(1 for r in rows if r["signal"] and r["signal"].endswith("BUY"))
        signal_sells = sum(1 for r in rows if r["signal"] and r["signal"].endswith("SELL"))
        with _movers_lock:
            _movers_state["gainers"] = gainers
            _movers_state["losers"] = losers
            _movers_state["all"] = rows
            _movers_state["total"] = len(rows)
            _movers_state["signal_buys"] = signal_buys
            _movers_state["signal_sells"] = signal_sells
            _movers_state["updated"] = datetime.now().astimezone().isoformat(timespec="seconds")
    except Exception as e:
        print(f"  [movers] refresh error: {e}")


def _movers_loop(interval: int = 120):
    _refresh_movers_once()
    while not _movers_stop:
        time.sleep(interval)
        _refresh_movers_once()


def _start_movers_thread(interval: int = 120):
    t = threading.Thread(target=_movers_loop, args=(interval,), daemon=True)
    t.start()
    return t

# Watchlist storage
_WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), "data", "scanner_watchlist.json")
_watchlist: list[dict] = []


def _load_watchlist():
    global _watchlist
    try:
        if os.path.exists(_WATCHLIST_FILE):
            with open(_WATCHLIST_FILE, "r") as f:
                _watchlist = json.load(f)
        else:
            _watchlist = []
    except Exception:
        _watchlist = []


def _save_watchlist():
    try:
        with open(_WATCHLIST_FILE, "w") as f:
            json.dump(_watchlist, f, indent=2)
    except Exception as e:
        print(f"Error saving watchlist: {e}")


_load_watchlist()

# ---------------------------------------------------------------------------
# Signal quality stats (from tracked signal history)
# ---------------------------------------------------------------------------
_SIGNAL_HISTORY_FILE = os.path.join(os.path.dirname(__file__), "signal_history.json")
_quality_stats: dict = {}


def _load_quality_stats():
    global _quality_stats
    try:
        if os.path.exists(_SIGNAL_HISTORY_FILE):
            with open(_SIGNAL_HISTORY_FILE, encoding="utf-8") as f:
                history = json.load(f)
        else:
            history = []
    except Exception:
        history = []

    closed_by_strat: dict[str, list[float]] = {}
    for s in history:
        status = s.get("status")
        pnl = s.get("pnl_pct")
        if status in ("active", None) or pnl is None:
            continue
        strat = s.get("strategy") or "?"
        closed_by_strat.setdefault(strat, []).append(float(pnl))

    stats = {}
    for strat, pnls in closed_by_strat.items():
        wins = [p for p in pnls if p > 0]
        stats[strat] = {
            "closed": len(pnls),
            "wins": len(wins),
            "losses": len(pnls) - len(wins),
            "win_rate": round(len(wins) / len(pnls) * 100, 1),
            "avg_pnl": round(sum(pnls) / len(pnls), 2),
        }
    _quality_stats = stats


_load_quality_stats()


# ---------------------------------------------------------------------------
# Live quotes (Fyers WebSocket, litemode) — updates _live_prices
# ---------------------------------------------------------------------------
_live_prices: dict = {}
_live_subscribed: set[str] = set()


def _watchlist_symbols() -> list[str]:
    """Union of watchlist.json + scanner watchlist symbols."""
    syms: list[str] = [w.get("symbol") for w in _watchlist if w.get("symbol")]
    wj = os.path.join(os.path.dirname(__file__), "data", "watchlist.json")
    try:
        if os.path.exists(wj):
            with open(wj, encoding="utf-8") as f:
                data = json.load(f)
            syms += list(data.get("watchlist", []))
    except Exception:
        pass
    return list(dict.fromkeys([s for s in syms if s]))


class LiveQuotes:
    """Fyers data WebSocket (litemode) feeding latest LTP into _live_prices."""

    def __init__(self):
        self.socket = None

    def _on_message(self, msg):
        if not isinstance(msg, dict) or not msg.get("symbol"):
            return
        sym = msg["symbol"]
        _live_prices[sym] = {
            "ltp": msg.get("ltp", 0),
            "open": msg.get("open", 0),
            "high": msg.get("high", 0),
            "low": msg.get("low", 0),
            "prev_close": msg.get("prev_close", 0),
            "volume": msg.get("ttq", msg.get("volume", 0)),
        }

    def _on_error(self, msg):
        pass

    def _on_close(self, msg):
        pass

    def _on_connect(self):
        try:
            self._subscribe(list(_live_subscribed))
        except Exception as e:
            print(f"  live subscribe error: {e}")

    def _subscribe(self, symbols):
        if not self.socket or not symbols:
            return
        new_syms = [s for s in symbols if s not in _live_subscribed]
        if not new_syms:
            return
        try:
            self.socket.subscribe(symbols=new_syms, data_type="SymbolUpdate")
            self.socket.keep_running()
            _live_subscribed.update(new_syms)
        except Exception as e:
            print(f"  live subscribe error: {e}")

    def start(self):
        try:
            from auth.login import _load_saved_token, load_env, FYERS_LOG_DIR
            from fyers_apiv3.FyersWebsocket import data_ws
            load_env()
            saved = _load_saved_token()
            if not saved:
                print("  Live quotes: no token")
                return
            token = f"{saved['client_id']}:{saved['access_token']}"
            self.socket = data_ws.FyersDataSocket(
                access_token=token,
                log_path=FYERS_LOG_DIR,
                litemode=True,
                write_to_file=False,
                reconnect=True,
                on_connect=self._on_connect,
                on_close=self._on_close,
                on_error=self._on_error,
                on_message=self._on_message,
            )
            self.socket.connect()
        except Exception as e:
            print(f"  Live quotes error: {e}")


_live_quotes = None


def _ensure_live_quotes():
    global _live_quotes
    if _live_quotes is None:
        _live_subscribed.update(_watchlist_symbols())
        _live_quotes = LiveQuotes()
        t = threading.Thread(target=_live_quotes.start, daemon=True)
        t.start()
    else:
        with _lock:
            syms = [s.get("symbol") for s in _last_scan_result.get("signals", [])]
            syms += [s.get("symbol") for s in _last_scan_result.get("index_signals", [])]
        syms += _watchlist_symbols()
        _live_quotes._subscribe(list(dict.fromkeys([s for s in syms if s])))


def _run_scan(timeframe: str = "D"):
    """Run scanner in background."""
    import sys, io
    if sys.platform == 'win32' and hasattr(sys.stdout, 'buffer'):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    global _last_scan_result
    with _lock:
        _last_scan_result["scanning"] = True

    try:
        scanner = StockScanner()
        tf = timeframe.lower()
        if tf in ("15", "15min", "5", "5min"):
            # Intraday: scan only the watchlist universe for speed
            intraday_syms = _watchlist_symbols()
            if intraday_syms:
                scanner.symbols = intraday_syms
        signals = scanner.scan_all(timeframe)
        aggregated = aggregate_signals(signals)
        dicts = [aggregated_to_dict(s) for s in aggregated]
        # Separate index signals from stock signals
        index_dicts = [d for d in dicts if "-INDEX" in d.get("symbol", "")]
        stock_dicts = [d for d in dicts if "-INDEX" not in d.get("symbol", "")]
        with _lock:
            _last_scan_result["signals"] = stock_dicts
            _last_scan_result["index_signals"] = index_dicts
            _last_scan_result["last_scan"] = datetime.now().isoformat()
            _last_scan_result["scanning"] = False
        # Keep live quotes subscribed to the latest signal symbols
        _ensure_live_quotes()
        # Send Telegram alerts for new signals
        try:
            from scanner.telegram_alerts import get_alerts
            tg = get_alerts()
            if tg.enabled:
                sent = tg.check_and_send(stock_dicts)
                if sent > 0:
                    print(f"Telegram: {sent} new signal alert(s) sent.")
        except Exception as e:
            print(f"Telegram alert error: {e}")
    except Exception as e:
        with _lock:
            _last_scan_result["scanning"] = False
            _last_scan_result["error"] = str(e)


def _auto_scan_loop(interval: int = 300):
    """Auto-scan during market hours. Daily candles don't change intraday, so
    the full-universe daily scan runs at most every 30 min — leaving Fyers API
    quota for the live big-money single-order watcher instead of burning it."""
    global _auto_scan_enabled
    last_daily_scan = 0.0
    while _auto_scan_enabled:
        now = datetime.now()
        hour = now.hour
        minute = now.minute
        market_open = (hour == 9 and minute >= 15) or (10 <= hour <= 14) or (hour == 15 and minute <= 30)
        if market_open:
            if time.time() - last_daily_scan >= 1800:
                last_daily_scan = time.time()
                _run_scan("D")
        time.sleep(interval)


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------
class DashboardHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            # If no valid token, redirect to login page
            if not self._has_valid_token():
                self.send_response(302)
                self.send_header("Location", "/api/login")
                self.end_headers()
                return
            self._serve_static("index.html", "text/html")
        elif path == "/style.css":
            self._serve_static("style.css", "text/css")
        elif path == "/app.js":
            self._serve_static("app.js", "application/javascript")
        elif path == "/api/signals":
            self._serve_signals(params)
        elif path == "/api/scan":
            self._trigger_scan(params)
        elif path == "/api/auto":
            self._toggle_auto(params)
        elif path == "/api/status":
            self._serve_status()
        elif path == "/api/ratelimit":
            self._serve_ratelimit()
        elif path == "/api/quality":
            self._serve_quality()
        elif path == "/api/bigmoney":
            self._serve_bigmoney()
        elif path == "/api/bigmoney/history":
            self._serve_bigmoney_history()
        elif path == "/api/bigmoney/scan":
            self._trigger_bigmoney_scan()
        elif path == "/api/movers":
            self._serve_movers()
        elif path == "/api/movers/analysis":
            self._serve_movers_analysis(params)
        elif path == "/api/regime":
            self._serve_regime()
        elif path == "/api/chartstrategy":
            self._serve_chartstrategy()
        elif path == "/api/telegram":
            self._serve_telegram(params)
        elif path == "/api/signal-groups":
            self._serve_signal_groups()
        elif path == "/api/backtest":
            self._serve_backtest(params)
        elif path == "/api/live":
            self._serve_live()
        elif path == "/api/stream":
            self._serve_stream()
        elif path == "/api/history":
            self._serve_history(params)
        elif path == "/api/watchlist":
            self._serve_watchlist()
        elif path == "/api/watchlist/add":
            self._add_to_watchlist(params)
        elif path == "/api/watchlist/remove":
            self._remove_from_watchlist(params)
        elif path == "/api/login":
            self._serve_login(params)
        elif path == "/api/login/submit":
            self._serve_login_submit(params)
        elif path == "/api/login/status":
            self._serve_login_status()
        else:
            self.send_error(404)

    def _serve_static(self, filename, content_type):
        import gzip as _gzip
        cached = _STATIC_CACHE.get(filename)
        if cached is None:
            # Fall back to disk if not in cache
            static_dir = os.path.join(os.path.dirname(__file__), "static")
            filepath = os.path.join(static_dir, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    raw = f.read().encode("utf-8")
                cached = _gzip.compress(raw, 6)
                _STATIC_CACHE[filename] = cached
            except FileNotFoundError:
                self.send_error(404, f"Static file not found: {filename}")
                return
        is_gzip = cached[:2] == b"\x1f\x8b"  # gzip magic bytes
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Cache-Control", "public, max-age=300")
        if is_gzip:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(cached)))
        self.end_headers()
        self.wfile.write(cached)

    # ---- Login endpoints (for Render.com headless login) ----

    def _serve_login(self, params):
        """Show login page with Fyers auth URL and auth_code input."""
        from auth.login import load_env, _resolve_credentials
        load_env()
        client_id, secret_key, redirect_uri = _resolve_credentials()
        login_url = (
            f"https://api-t1.fyers.in/api/v3/generate-authcode"
            f"?client_id={client_id}"
            f"&redirect_uri={redirect_uri}"
            f"&response_type=code&state=fyers_login"
        )
        # Check if token is valid
        token_valid = False
        try:
            from auth.login import _load_saved_token, is_token_valid
            saved = _load_saved_token()
            if saved and is_token_valid(saved["access_token"], saved.get("client_id", client_id)):
                token_valid = True
        except Exception:
            pass
        html = f"""<!DOCTYPE html>
<html><head><title>JBK Scanner - Login</title>
<style>
body {{ font-family: -apple-system, sans-serif; background: #0a0e1a; color: #f1f5f9; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }}
.card {{ background: #1a1f35; border: 1px solid #2d3a52; border-radius: 16px; padding: 40px; max-width: 500px; width: 90%; text-align: center; }}
h1 {{ background: linear-gradient(135deg, #3b82f6, #06b6d4); -webkit-background-clip: text; -webkit-text-fill-color: transparent; font-size: 28px; }}
.btn {{ display: inline-block; padding: 14px 28px; border-radius: 8px; font-size: 16px; font-weight: 600; cursor: pointer; border: none; margin: 10px 5px; text-decoration: none; }}
.btn-primary {{ background: #3b82f6; color: white; }}
.btn-primary:hover {{ background: #2563eb; }}
.btn-success {{ background: #22c55e; color: white; }}
.btn-success:hover {{ background: #16a34a; }}
.input {{ width: 100%; padding: 14px; border: 1px solid #2d3a52; border-radius: 8px; background: #111827; color: #f1f5f9; font-size: 14px; margin: 10px 0; box-sizing: border-box; }}
.input:focus {{ outline: none; border-color: #3b82f6; }}
.status {{ padding: 12px; border-radius: 8px; margin: 15px 0; font-size: 14px; }}
.status-ok {{ background: #052e16; border: 1px solid #22c55e; color: #22c55e; }}
.status-err {{ background: #450a0a; border: 1px solid #ef4444; color: #ef4444; }}
.step {{ text-align: left; margin: 15px 0; padding: 12px; background: #111827; border-radius: 8px; }}
.step b {{ color: #3b82f6; }}
</style></head><body>
<div class="card">
  <h1>🔍 JBK Scanner</h1>
  <p style="color: #94a3b8; margin: 10px 0 25px;">Login to start scanning 208+ F&O stocks</p>
  {'<div class="status status-ok">✅ Already logged in! <a href="/" style="color: #22c55e;">Go to Dashboard →</a></div>' if token_valid else ''}
  <div class="step"><b>Step 1:</b> Click the button below to open Fyers login</div>
  <a href="{login_url}" target="_blank" class="btn btn-primary">🔗 Login to Fyers</a>
  <div class="step"><b>Step 2:</b> After login, you'll be redirected to a URL like:<br><code style="color:#60a5fa; word-break:break-all;">https://trade.fyers.in/...?auth_code=XXXX</code><br>Copy the <b>auth_code</b> value from that URL.</div>
  <div class="step"><b>Step 3:</b> Paste the auth_code below and click Submit</div>
  <form method="POST" action="/api/login/submit">
    <input class="input" name="auth_code" placeholder="Paste auth_code here..." required />
    <button type="submit" class="btn btn-success">✅ Submit & Login</button>
  </form>
</div></body></html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())

    def _serve_login_submit(self, params):
        """Exchange auth_code for access_token."""
        auth_code = params.get("auth_code", [""])[0]
        if not auth_code:
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write('<html><body style="background:#0a0e1a;color:#f1f5f9;font-family:sans-serif;text-align:center;padding:50px;"><h2>Error: No auth_code provided</h2><a href="/api/login" style="color:#3b82f6;">Back to login</a></body></html>'.encode())
            return
        try:
            from auth.login import load_env, _resolve_credentials, _save_token, is_token_valid
            from fyers_apiv3 import fyersModel
            load_env()
            client_id, secret_key, redirect_uri = _resolve_credentials()
            # Exchange auth_code for access_token
            session = fyersModel.SessionModel(
                client_id=client_id, redirect_uri=redirect_uri,
                response_type="code", state="fyers_login",
                secret_key=secret_key, grant_type="authorization_code",
            )
            session.set_token(auth_code)
            response = session.generate_token()
            if "access_token" not in response:
                raise Exception(f"Token generation failed: {response}")
            access_token = response["access_token"]
            _save_token(access_token, client_id)
            # Verify it works
            if is_token_valid(access_token, client_id):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write('<html><body style="background:#0a0e1a;color:#f1f5f9;font-family:sans-serif;text-align:center;padding:50px;"><h2 style="color:#22c55e;">Login Successful!</h2><p>Token saved. Redirecting to dashboard...</p><script>setTimeout(()=>window.location="/",2000);</script></body></html>'.encode())
                # Trigger first scan
                threading.Thread(target=_run_scan, args=("D",), daemon=True).start()
            else:
                raise Exception("Token was saved but validation failed")
        except Exception as e:
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = str(e).replace("<", "&lt;").replace(">", "&gt;")
            error_page = (
                '<html><body style="background:#0a0e1a;color:#f1f5f9;font-family:sans-serif;text-align:center;padding:50px;">'
                '<h2 style="color:#ef4444;">Login Failed</h2>'
                '<p>' + msg + '</p>'
                '<a href="/api/login" style="color:#3b82f6;">Try again</a></body></html>'
            )
            self.wfile.write(error_page.encode())

    def _serve_login_status(self):
        """Check if logged in."""
        result = {"logged_in": False}
        try:
            from auth.login import load_env, _resolve_credentials, _load_saved_token, is_token_valid
            load_env()
            client_id = _resolve_credentials()[0]
            saved = _load_saved_token()
            if saved and is_token_valid(saved["access_token"], saved.get("client_id", client_id)):
                result["logged_in"] = True
        except Exception:
            pass
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())

    def do_POST(self):
        """Handle POST requests for watchlist and login."""
        parsed = urlparse(self.path)
        path = parsed.path
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length) if content_length > 0 else b'{}'
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            # Try form data (URL-encoded)
            from urllib.parse import parse_qs as _pq
            data = _pq(body.decode('utf-8', errors='replace'))
        params = {k: v[0] if isinstance(v, list) else v for k, v in data.items()}
        if path == "/api/login/submit":
            self._serve_login_submit(params)
        elif path == "/api/watchlist/add":
            self._add_to_watchlist(params)
        elif path == "/api/watchlist/remove":
            self._remove_from_watchlist(params)
        else:
            self.send_error(404)

    def _has_valid_token(self):
        """Check if we have a valid Fyers token."""
        try:
            from auth.login import load_env, _resolve_credentials, _load_saved_token, is_token_valid
            load_env()
            client_id = _resolve_credentials()[0]
            saved = _load_saved_token()
            return saved and is_token_valid(saved["access_token"], saved.get("client_id", client_id))
        except Exception:
            return False

    def _serve_dashboard(self):
        self._serve_static("index.html", "text/html")

    def _serve_signals(self, params):
        with _lock:
            data = json.dumps(_last_scan_result)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data.encode())

    def _trigger_scan(self, params):
        tf = params.get("timeframe", ["D"])[0]
        t = threading.Thread(target=_run_scan, args=(tf,), daemon=True)
        t.start()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"scan_started"}')

    def _toggle_auto(self, params):
        global _auto_scan_enabled, _auto_scan_thread
        enable = params.get("enable", ["true"])[0].lower() == "true"
        _auto_scan_enabled = enable
        if enable and (_auto_scan_thread is None or not _auto_scan_thread.is_alive()):
            _auto_scan_thread = threading.Thread(target=_auto_scan_loop, daemon=True)
            _auto_scan_thread.start()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"auto_scan": _auto_scan_enabled}).encode())

    def _serve_status(self):
        with _lock:
            status = {
                "scanning": _last_scan_result["scanning"],
                "last_scan": _last_scan_result["last_scan"],
                "auto_scan": _auto_scan_enabled,
                "signal_count": len(_last_scan_result["signals"]),
            }
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(status).encode())

    def _serve_ratelimit(self):
        """Rate limiter stats — API calls, retries, throttles."""
        limiter = get_limiter()
        stats = limiter.get_stats()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(stats).encode())

    def _serve_quality(self):
        """Per-strategy win-rate stats from tracked signal history."""
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"strategies": _quality_stats}).encode())

    def _serve_regime(self):
        """Index regime scan (index options strategy)."""
        path = os.path.join(os.path.dirname(__file__), "index_regime.json")
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {"generated": None, "indices": []}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _serve_chartstrategy(self):
        """Index Chart Strategy results (regime direction + 15-min chart entry)."""
        path = os.path.join(os.path.dirname(__file__), "index_chart_strategy.json")
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {"generated": None, "indices": []}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _serve_telegram(self, params):
        """Handle Telegram test and config."""
        action = params.get("action", ["status"])[0]
        try:
            from scanner.telegram_alerts import get_alerts
            tg = get_alerts()
            
            if action == "test":
                success = tg.send_test_message()
                result = {"success": success, "message": "Test sent!" if success else "Failed"}
            elif action == "summary":
                with _lock:
                    signals = _last_scan_result.get("signals", [])
                success = tg.send_daily_summary(signals)
                result = {"success": success, "message": "Summary sent!" if success else "Already sent or failed"}
            else:
                result = {
                    "enabled": tg.enabled,
                    "chat_id": tg.chat_id[:10] + "..." if tg.chat_id else "Not set",
                    "market_hours": tg.is_market_hours(),
                    "market_open_day": tg.is_market_open_day(),
                    "sent_count": len(tg.sent_signals),
                }
        except Exception as e:
            result = {"error": str(e)}
        
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())

    def _serve_signal_groups(self):
        """Group signals by sector, strategy, pattern, strength."""
        try:
            from scanner.signal_groups import group_signals
            with _lock:
                signals = _last_scan_result.get("signals", [])
            groups = group_signals(signals)
            # Convert SignalGroup dataclasses to JSON-safe dicts
            def _gdict(g):
                return {"group_type": g.group_type, "group_name": g.group_name,
                        "count": g.count, "emoji": g.emoji, "highlight": g.highlight,
                        "signals": g.signals}
            result = {
                "sectors": [_gdict(g) for g in groups.get("sectors", [])],
                "summary": groups.get("summary", []),
            }
        except Exception as e:
            import traceback
            traceback.print_exc()
            result = {"error": str(e)}
        
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())

    def _serve_backtest(self, params):
        """Run backtest or load cached results."""
        action = params.get("action", ["load"])[0]
        try:
            if action == "run":
                # Run full backtest in background thread
                max_sym = int(params.get("max", ["30"])[0])
                days = int(params.get("days", ["252"])[0])
                hold = int(params.get("hold", ["10"])[0])
                
                def _run():
                    from scanner.backtest import run_full_backtest, save_backtest_results
                    from scanner.universe import ALL_SYMBOLS
                    symbols = [s for s in ALL_SYMBOLS if "-INDEX" not in s][:max_sym]
                    results = run_full_backtest(symbols=symbols, days=days, hold_days=hold)
                    save_backtest_results(results)
                    global _backtest_status
                    _backtest_status = {"running": False, "generated": datetime.now().isoformat()}
                
                global _backtest_status
                _backtest_status = {"running": True, "started": datetime.now().isoformat()}
                threading.Thread(target=_run, daemon=True).start()
                result = {"status": "started", "message": f"Backtesting {max_sym} symbols..."}
            elif action == "status":
                result = _backtest_status
            else:
                # Load cached results
                from scanner.backtest import load_backtest_results
                data = load_backtest_results()
                result = data if data else {"strategies": {}, "generated": None}
        except Exception as e:
            result = {"error": str(e)}
        
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())

    def _serve_live(self):
        """Latest WebSocket LTP quotes for subscribed symbols."""
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"prices": _live_prices}).encode())

    def _serve_stream(self):
        """Server-Sent Events — push scan results + live prices in real time.

        Replaces 30s/10s client polling. Sends:
          event: signals  — whenever a scan finishes (or starts)
          event: live     — live LTP prices every ~1s
          event: ping     — every 15s to keep the connection alive
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.flush()
        except Exception:
            return

        self.close_connection = False

        def _emit(name: str, payload: dict):
            chunk = f"event: {name}\ndata: {json.dumps(payload)}\n\n".encode("utf-8")
            self.wfile.write(chunk)
            self.wfile.flush()

        last_sig_key = None
        last_heartbeat = time.time()

        # Initial state
        try:
            with _lock:
                sigs = _last_scan_result.get("signals")
                idx = _last_scan_result.get("index_signals", [])
                scanning = _last_scan_result.get("scanning")
                last_scan = _last_scan_result.get("last_scan")
            last_sig_key = (len(sigs or []), last_scan, scanning)
            _emit("signals", {
                "signals": sigs, "index_signals": idx,
                "scanning": scanning, "last_scan": last_scan,
            })
        except Exception:
            pass

        while not self.close_connection:
            try:
                # Push live prices (throttled to ~1s loop)
                try:
                    _emit("live", {"prices": _live_prices})
                except Exception:
                    break

                # Push signals again only if something changed
                with _lock:
                    sigs = _last_scan_result.get("signals")
                    idx = _last_scan_result.get("index_signals", [])
                    scanning = _last_scan_result.get("scanning")
                    last_scan = _last_scan_result.get("last_scan")
                key = (len(sigs or []), last_scan, scanning)
                if key != last_sig_key:
                    last_sig_key = key
                    _emit("signals", {
                        "signals": sigs, "index_signals": idx,
                        "scanning": scanning, "last_scan": last_scan,
                    })

                # Heartbeat
                now = time.time()
                if now - last_heartbeat > 15:
                    try:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                    except Exception:
                        break
                    last_heartbeat = now

                time.sleep(1)
            except (BrokenPipeError, ConnectionResetError, OSError):
                break
            except Exception:
                break

    def _serve_bigmoney(self):
        """Big Money — prefer the WebSocket scanner (simple punch criteria,
        source=websocket) over the legacy daily scan."""
        from datetime import datetime as _dt
        live_path = os.path.join(os.path.dirname(__file__), "data", "big_money_live.json")
        daily_path = os.path.join(os.path.dirname(__file__), "data", "big_money_signals.json")

        def _load(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return None

        def _ts(d):
            try:
                return _dt.fromisoformat((d or {}).get("generated") or "1970-01-01T00:00:00")
            except Exception:
                return _dt.min

        live = _load(live_path)
        daily = _load(daily_path)

        # Always prefer the WebSocket scanner (source=websocket) with simple punch criteria.
        # Fall back to the legacy daily scan only if the WS file is missing.
        ws = live if live and live.get("source") == "websocket" else None
        if ws is None:
            ws = daily

        if ws is None:
            data = {"generated": None, "signals": []}
        else:
            signals = ws.get("bursts") or ws.get("signals") or []
            data = {"generated": ws.get("generated"), "signals": signals}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _serve_bigmoney_history(self):
        """Persistent recent-punch tracker — every share that met the criteria."""
        path = os.path.join(os.path.dirname(__file__), "data", "big_money_punch_history.json")
        try:
            with open(path, encoding="utf-8") as f:
                history = json.load(f)
            if not isinstance(history, list):
                history = []
        except Exception:
            history = []
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"punches": history}).encode())

    def _trigger_bigmoney_scan(self):
        """Legacy on-demand daily big-money scan. Writes to the DAILY file only
        (never the WebSocket live file, which the punch tracker owns)."""
        global _backtest_status
        if _backtest_status.get("running"):
            self.send_response(409)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Another scan is running"}).encode())
            return

        _backtest_status["running"] = True
        def _run():
            try:
                from scanner.big_money import BigMoneyTracker
                tracker = BigMoneyTracker(min_score=50.0)
                signals = tracker.scan_all(max_stocks=None, mode="daily")
                daily_path = os.path.join(os.path.dirname(__file__), "data", "big_money_signals.json")
                import datetime as _dt
                with open(daily_path, "w") as f:
                    json.dump({
                        "generated": _dt.datetime.now().isoformat(),
                        "interval_min": 1440,
                        "signals": [asdict(s) for s in signals],
                    }, f, indent=2)
            except Exception as e:
                print(f"Big money scan error: {e}")
            finally:
                _backtest_status["running"] = False

        from dataclasses import asdict
        threading.Thread(target=_run, daemon=True).start()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "started", "message": "Big money scan started"}).encode())

    def _serve_movers(self):
        """F&O Movers — top gainers / losers of the cash segment."""
        with _movers_lock:
            data = {
                "gainers": _movers_state["gainers"],
                "losers": _movers_state["losers"],
                "updated": _movers_state["updated"],
                "total": _movers_state["total"],
                "signal_buys": _movers_state["signal_buys"],
                "signal_sells": _movers_state["signal_sells"],
            }
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _serve_movers_analysis(self, params):
        """Technical analysis for a mover stock (double-click)."""
        symbol = params.get("symbol", [""])[0]
        if not symbol:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"symbol required"}')
            return
        cached = _movers_analysis_cache.get(symbol)
        if cached and time.time() - cached[1] < 300:
            analysis = cached[0]
        else:
            try:
                _init_movers_fyers()
                if _movers_fyers is None:
                    analysis = {"symbol": symbol, "error": "No Fyers token"}
                else:
                    analysis = analyze_future(_movers_fyers, symbol)
                    _movers_analysis_cache[symbol] = (analysis, time.time())
            except Exception as e:
                analysis = {"symbol": symbol, "error": str(e)}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(analysis).encode())

    def _serve_history(self, params):
        symbol = params.get("symbol", [""])[0]
        days = int(params.get("days", ["30"])[0])
        resolution = params.get("resolution", ["D"])[0]
        if not symbol:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"symbol required"}')
            return
        try:
            from datetime import timedelta
            scanner = _get_history_scanner()
            end = datetime.now()
            # Fetch extra to warm up EMAs, then slice to requested days
            extra = 60
            if resolution == "D":
                start = end - timedelta(days=days + extra)
                data = {
                    "symbol": symbol, "resolution": "D",
                    "date_format": 1,
                    "range_from": start.strftime("%Y-%m-%d"),
                    "range_to": end.strftime("%Y-%m-%d"),
                    "cont_flag": 1,
                }
            else:
                start = end - timedelta(days=days)
                data = {
                    "symbol": symbol, "resolution": resolution,
                    "date_format": 1,
                    "range_from": start.strftime("%Y-%m-%d"),
                    "range_to": end.strftime("%Y-%m-%d"),
                    "cont_flag": 1,
                }
            from scanner.rate_limiter import get_limiter
            resp = get_limiter().retry_call(scanner.fyers.history, data=data)
            if resp is None:
                # Likely an expired token — rebuild the cached client once and retry
                global _history_scanner
                _history_scanner = StockScanner()
                resp = get_limiter().retry_call(_history_scanner.fyers.history, data=data)
            all_candles = resp.get("candles", []) if resp and resp.get("s") == "ok" else []
            if resolution == "D" and len(all_candles) > days:
                candles = all_candles[-days:]
            else:
                # intraday: keep all bars returned (already scoped by days)
                candles = all_candles
            closes = [c[4] for c in candles]
            highs = [c[2] for c in candles]
            lows = [c[3] for c in candles]
            # Full series for EMA warmup, then slice last days
            full_closes = [c[4] for c in all_candles]
            full_highs = [c[2] for c in all_candles]
            full_lows = [c[3] for c in all_candles]

            ema9 = _ema(full_closes, 9)
            ema21 = _ema(full_closes, 21)
            ema50 = _ema(full_closes, 50)
            rsi = _rsi(full_closes, 14)
            offset = len(full_closes) - len(candles)
            ema9 = ema9[offset:] if offset > 0 else ema9
            ema21 = ema21[offset:] if offset > 0 else ema21
            ema50 = ema50[offset:] if offset > 0 else ema50
            rsi = rsi[offset:] if offset > 0 else rsi

            result = {
                "symbol": symbol,
                "candles": [{
                    "date": c[0], "open": c[1], "high": c[2],
                    "low": c[3], "close": c[4], "volume": c[5]
                } for c in candles],
                "ema9": ema9,
                "ema21": ema21,
                "ema50": ema50,
                "rsi": rsi,
                "support": round(min(lows[-20:]), 2) if len(lows) >= 20 else None,
                "resistance": round(max(highs[-20:]), 2) if len(highs) >= 20 else None,
                "high_52w": round(max(full_highs[-252:]), 2) if len(full_highs) >= 50 else None,
                "low_52w": round(min(full_lows[-252:]), 2) if len(full_lows) >= 50 else None,
            }
        except Exception as e:
            result = {"symbol": symbol, "candles": [], "error": str(e)}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())

    def _serve_watchlist(self):
        """Return the current watchlist."""
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"watchlist": _watchlist}).encode())

    def _add_to_watchlist(self, params):
        """Add a stock to the watchlist."""
        symbol = params.get("symbol", "")
        name = params.get("name", "")
        notes = params.get("notes", "")

        if not symbol:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"symbol required"}')
            return

        # Check if already in watchlist
        if any(w["symbol"] == symbol for w in _watchlist):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "already_exists", "watchlist": _watchlist}).encode())
            return

        entry = {
            "symbol": symbol,
            "name": name or symbol.split(":")[-1].replace("-EQ", ""),
            "notes": notes,
            "added_at": datetime.now().isoformat(),
        }
        _watchlist.append(entry)
        _save_watchlist()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "added", "watchlist": _watchlist}).encode())

    def _remove_from_watchlist(self, params):
        """Remove a stock from the watchlist."""
        symbol = params.get("symbol", "")
        if not symbol:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"symbol required"}')
            return

        global _watchlist
        _watchlist = [w for w in _watchlist if w["symbol"] != symbol]
        _save_watchlist()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "removed", "watchlist": _watchlist}).encode())

    def log_message(self, format, *args):
        pass  # Suppress request logs


# ---------------------------------------------------------------------------
# Dashboard HTML — Modern UI v2
# ---------------------------------------------------------------------------
def _get_dashboard_html() -> str:
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>JBK Scanner</title>
<style>
:root {
  --bg-primary: #0a0e1a;
  --bg-secondary: #111827;
  --bg-card: #1a1f35;
  --bg-card-hover: #222842;
  --bg-table-row: #0f1424;
  --bg-table-hover: #1a2040;
  --border: #1e293b;
  --border-light: #2d3a52;
  --text-primary: #f1f5f9;
  --text-secondary: #94a3b8;
  --text-muted: #64748b;
  --accent-blue: #3b82f6;
  --accent-blue-light: #60a5fa;
  --accent-green: #22c55e;
  --accent-green-dark: #15803d;
  --accent-red: #ef4444;
  --accent-red-dark: #b91c1c;
  --accent-yellow: #eab308;
  --accent-purple: #a855f7;
  --accent-cyan: #06b6d4;
  --accent-orange: #f97316;
  --gradient-buy: linear-gradient(135deg, #059669, #22c55e);
  --gradient-sell: linear-gradient(135deg, #dc2626, #ef4444);
  --gradient-strong-buy: linear-gradient(135deg, #047857, #10b981);
  --gradient-very-strong: linear-gradient(135deg, #065f46, #059669);
  --shadow: 0 4px 24px rgba(0,0,0,0.4);
  --shadow-lg: 0 8px 40px rgba(0,0,0,0.5);
  --radius: 12px;
  --radius-sm: 8px;
  --radius-xs: 6px;
  --thead-bg: #151b30;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
  background: var(--bg-primary);
  color: var(--text-primary);
  line-height: 1.5;
  overflow-x: hidden;
}
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: var(--bg-primary); }
::-webkit-scrollbar-thumb { background: #334155; border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: #475569; }

/* Header */
.header {
  background: linear-gradient(135deg, #111827 0%, #1a1f35 100%);
  padding: 16px 28px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-bottom: 1px solid var(--border);
  position: sticky;
  top: 0;
  z-index: 100;
  backdrop-filter: blur(20px);
}
.header-left { display: flex; align-items: center; gap: 16px; }
.logo {
  font-size: 24px;
  font-weight: 800;
  background: linear-gradient(135deg, #3b82f6, #06b6d4);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  letter-spacing: -0.5px;
}
.logo-sub {
  font-size: 12px;
  color: var(--text-muted);
  font-weight: 500;
  letter-spacing: 1px;
  text-transform: uppercase;
}
.controls { display: flex; gap: 8px; align-items: center; }
.btn {
  padding: 8px 16px;
  border: none;
  border-radius: var(--radius-xs);
  cursor: pointer;
  font-size: 13px;
  font-weight: 600;
  transition: all 0.2s ease;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
}
.btn:active { transform: scale(0.96); }
.btn-primary { background: var(--accent-blue); color: white; }
.btn-primary:hover { background: #2563eb; box-shadow: 0 4px 16px rgba(59,130,246,0.3); }
.btn-success { background: var(--accent-green); color: white; }
.btn-success:hover { background: #16a34a; box-shadow: 0 4px 16px rgba(34,197,94,0.3); }
.btn-danger { background: var(--accent-red); color: white; }
.btn-danger:hover { background: #dc2626; }
.btn-outline {
  background: transparent;
  color: var(--text-secondary);
  border: 1px solid var(--border-light);
}
.btn-outline:hover { background: var(--bg-card); color: var(--text-primary); }
.btn-outline.active { background: var(--accent-blue); color: white; border-color: var(--accent-blue); }
.btn-ghost {
  background: transparent;
  color: var(--text-secondary);
  padding: 6px 10px;
}
.btn-ghost:hover { color: var(--text-primary); background: var(--bg-card); }

/* Market Status Bar */
.market-bar {
  padding: 8px 28px;
  background: var(--bg-secondary);
  display: flex;
  gap: 24px;
  font-size: 12px;
  color: var(--text-muted);
  border-bottom: 1px solid var(--border);
  align-items: center;
}
.market-status {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
  color: var(--text-secondary);
}
.pulse-dot {
  width: 8px; height: 8px;
  border-radius: 50%;
  display: inline-block;
}
.pulse-dot.green { background: var(--accent-green); box-shadow: 0 0 8px rgba(34,197,94,0.5); }
.pulse-dot.yellow { background: var(--accent-yellow); animation: pulse 1.5s infinite; }
.pulse-dot.red { background: var(--accent-red); }
@keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:0.3; } }
.market-bar .stat { display: flex; align-items: center; gap: 4px; }
.market-bar .stat strong { color: var(--text-primary); }

/* Summary Cards */
.summary-cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 16px;
  padding: 20px 28px;
}
.summary-card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 18px 20px;
  transition: all 0.3s ease;
  cursor: default;
  position: relative;
  overflow: hidden;
}
.summary-card::before {
  content: '';
  position: absolute;
  top: 0; left: 0;
  width: 100%; height: 3px;
}
.summary-card:hover {
  transform: translateY(-2px);
  box-shadow: var(--shadow);
  border-color: var(--border-light);
}
.card-total::before { background: var(--accent-blue); }
.card-buy::before { background: var(--accent-green); }
.card-sell::before { background: var(--accent-red); }
.card-strong::before { background: var(--accent-purple); }
.card-range::before { background: var(--accent-cyan); }
.card-early::before { background: var(--accent-orange); }
.card-retrace::before { background: #38bdf8; }
.card-retrace .card-value { color: #38bdf8; }
.card-label {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.8px;
  margin-bottom: 8px;
}
.card-value {
  font-size: 32px;
  font-weight: 800;
  line-height: 1;
  margin-bottom: 4px;
}
.card-total .card-value { color: var(--accent-blue-light); }
.card-buy .card-value { color: var(--accent-green); }
.card-sell .card-value { color: var(--accent-red); }
.card-strong .card-value { color: var(--accent-purple); }
.card-range .card-value { color: var(--accent-cyan); }
.card-early .card-value { color: var(--accent-orange); }
.card-sub {
  font-size: 11px;
  color: var(--text-muted);
}

/* Toolbar */
.toolbar {
  padding: 12px 28px;
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
  border-bottom: 1px solid var(--border);
  background: var(--bg-primary);
  position: sticky;
  top: 60px;
  z-index: 90;
}
.search-box {
  position: relative;
  flex: 0 0 280px;
}
.search-box input {
  width: 100%;
  padding: 8px 12px 8px 36px;
  background: var(--bg-card);
  border: 1px solid var(--border-light);
  border-radius: var(--radius-xs);
  color: var(--text-primary);
  font-size: 13px;
  outline: none;
  transition: border-color 0.2s;
}
.search-box input:focus {
  border-color: var(--accent-blue);
  box-shadow: 0 0 0 3px rgba(59,130,246,0.15);
}
.search-box input::placeholder { color: var(--text-muted); }
.search-icon {
  position: absolute;
  left: 10px; top: 50%;
  transform: translateY(-50%);
  color: var(--text-muted);
  font-size: 14px;
  pointer-events: none;
}
.filter-divider {
  width: 1px;
  height: 28px;
  background: var(--border-light);
  margin: 0 4px;
}
.filter-group {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.filter-btn {
  padding: 5px 12px;
  border-radius: 20px;
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
  border: 1px solid var(--border-light);
  background: var(--bg-card);
  color: var(--text-secondary);
  transition: all 0.2s;
  white-space: nowrap;
}
.filter-btn:hover { border-color: var(--accent-blue); color: var(--text-primary); }
.filter-btn.active {
  background: var(--accent-blue);
  color: white;
  border-color: var(--accent-blue);
}
.filter-btn.active-green { background: var(--accent-green); border-color: var(--accent-green); color: white; }
.filter-btn.active-red { background: var(--accent-red); border-color: var(--accent-red); color: white; }
.filter-btn.bigmoney-toggle.active { background: var(--accent-purple); border-color: var(--accent-purple); color: white; }
.bm-stock-row { cursor: pointer; }
.bm-stock-row:hover { background: rgba(139,92,246,0.08); }
.bm-stock-row.selected { background: rgba(139,92,246,0.15); }
.bm-expand-row { display: none; }
.bm-expand-row.open { display: table-row; }
.filter-count {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  border-radius: 9px;
  background: rgba(255,255,255,0.15);
  font-size: 10px;
  margin-left: 4px;
}
.toolbar-right {
  margin-left: auto;
  display: flex;
  gap: 8px;
  align-items: center;
}
.result-count {
  font-size: 12px;
  color: var(--text-muted);
  padding-right: 8px;
}

/* Main Table */
.main { padding: 0 28px 28px; }
.table-container {
  background: var(--bg-card);
  border-radius: var(--radius);
  border: 1px solid var(--border);
  overflow: auto;
  max-height: calc(100vh - 230px);
  box-shadow: var(--shadow);
}
table { width: 100%; border-collapse: collapse; }
thead th {
  padding: 12px 16px;
  text-align: left;
  background: var(--thead-bg);
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  color: var(--text-muted);
  letter-spacing: 0.8px;
  border-bottom: 2px solid var(--border);
  cursor: pointer;
  user-select: none;
  white-space: nowrap;
  position: sticky;
  top: 0;
  z-index: 5;
  transition: color 0.2s;
}
thead th:hover { color: var(--text-primary); }
thead th .sort-arrow {
  margin-left: 4px;
  font-size: 10px;
  opacity: 0.4;
}
thead th.sorted-asc .sort-arrow,
thead th.sorted-desc .sort-arrow { opacity: 1; color: var(--accent-blue); }
tbody tr {
  background: var(--bg-table-row);
  border-bottom: 1px solid var(--border);
  cursor: pointer;
  transition: background 0.15s ease, box-shadow 0.15s ease;
  contain: layout style;
}
tbody tr:hover { background: var(--bg-table-hover); }
tbody tr.selected { background: #1a2545; border-left: 3px solid var(--accent-blue); }
tbody td {
  padding: 12px 16px;
  font-size: 13px;
  vertical-align: middle;
}
.symbol-cell {
  display: flex;
  align-items: center;
  gap: 10px;
}
.symbol-icon {
  width: 36px; height: 36px;
  border-radius: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 800;
  font-size: 12px;
  color: white;
  flex-shrink: 0;
}
.symbol-info { display: flex; flex-direction: column; }
.symbol-name { font-weight: 700; font-size: 14px; color: var(--text-primary); }
.symbol-tag { font-size: 11px; color: var(--text-muted); }

/* Strength Badges */
.strength-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 10px;
  border-radius: 20px;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.3px;
  white-space: nowrap;
}
.strength-buy {
  background: rgba(34,197,94,0.15);
  color: var(--accent-green);
  border: 1px solid rgba(34,197,94,0.3);
}
.strength-strong-buy {
  background: rgba(16,185,129,0.2);
  color: #34d399;
  border: 1px solid rgba(16,185,129,0.3);
}
.strength-very-strong-buy {
  background: rgba(5,150,105,0.25);
  color: #6ee7b7;
  border: 1px solid rgba(5,150,105,0.35);
}
.strength-sell {
  background: rgba(239,68,68,0.15);
  color: var(--accent-red);
  border: 1px solid rgba(239,68,68,0.3);
}
.strength-strong-sell {
  background: rgba(220,38,38,0.2);
  color: #f87171;
  border: 1px solid rgba(220,38,38,0.3);
}
.strength-very-strong-sell {
  background: rgba(185,28,28,0.25);
  color: #fca5a5;
  border: 1px solid rgba(185,28,28,0.35);
}

/* Strategy Tags */
.strat-tags { display: flex; gap: 4px; flex-wrap: wrap; }
.strat-tag {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 10px;
  font-weight: 600;
  background: rgba(59,130,246,0.12);
  color: var(--accent-blue-light);
  border: 1px solid rgba(59,130,246,0.2);
  white-space: nowrap;
}
.strat-tag.range { background: rgba(6,182,212,0.12); color: var(--accent-cyan); border-color: rgba(6,182,212,0.2); }
.strat-tag.channel { background: rgba(168,85,247,0.12); color: var(--accent-purple); border-color: rgba(168,85,247,0.2); }
.strat-tag.early { background: rgba(249,115,22,0.12); color: var(--accent-orange); border-color: rgba(249,115,22,0.2); }
.strat-tag.high52w { background: rgba(234,179,8,0.12); color: var(--accent-yellow); border-color: rgba(234,179,8,0.2); }
.strat-tag.candle { background: rgba(236,72,153,0.12); color: #f472b6; border-color: rgba(236,72,153,0.2); }
.strat-tag.volume { background: rgba(34,197,94,0.12); color: var(--accent-green); border-color: rgba(34,197,94,0.2); }
.strat-tag.medch { background: rgba(251,146,60,0.12); color: #fb923c; border-color: rgba(251,146,60,0.2); }
.strat-tag.indexrb { background: rgba(139,92,246,0.12); color: #a78bfa; border-color: rgba(139,92,246,0.2); }
.strat-tag.indexsr { background: rgba(236,72,153,0.12); color: #f472b6; border-color: rgba(236,72,153,0.2); }

.price-cell { font-weight: 700; font-size: 14px; font-variant-numeric: tabular-nums; }
.sl-cell { color: var(--accent-red); font-weight: 600; font-variant-numeric: tabular-nums; }
.target-cell { color: var(--accent-green); font-weight: 600; font-variant-numeric: tabular-nums; }
.confidence-cell { font-weight: 700; font-variant-numeric: tabular-nums; }
.risk-reward { font-size: 11px; color: var(--text-muted); }

/* Expanded Row */
.expand-row { display: none; }
.expand-row.open { display: table-row; animation: expandIn 0.2s ease; }
@keyframes expandIn { from { opacity: 0; } to { opacity: 1; } }
.expand-content {
  padding: 16px 20px;
  background: #111830;
  border-top: 1px solid var(--border);
}
.expand-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 16px;
}
.expand-item { }
.expand-label {
  font-size: 10px;
  font-weight: 600;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 4px;
}
.expand-value { font-size: 13px; color: var(--text-secondary); }
.expand-reasons {
  margin-top: 12px;
  padding: 12px;
  background: rgba(59,130,246,0.06);
  border-radius: var(--radius-xs);
  border: 1px solid rgba(59,130,246,0.12);
}
.expand-reasons-title {
  font-size: 11px;
  font-weight: 700;
  color: var(--accent-blue-light);
  margin-bottom: 8px;
}
.reason-item {
  font-size: 12px;
  color: var(--text-secondary);
  padding: 4px 0;
  border-bottom: 1px solid rgba(255,255,255,0.04);
}
.reason-item:last-child { border-bottom: none; }

/* Sparkline Chart */
.sparkline-container {
  margin: 12px 0;
  background: #0c1020;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  padding: 12px;
  position: relative;
}
.sparkline-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}
.sparkline-title {
  font-size: 11px;
  font-weight: 700;
  color: var(--accent-cyan);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.sparkline-stats {
  display: flex;
  gap: 16px;
  font-size: 11px;
}
.sparkline-stat { color: var(--text-muted); }
.sparkline-stat strong { color: var(--text-secondary); }
.sparkline-stat .up { color: var(--accent-green); }
.sparkline-stat .down { color: var(--accent-red); }
.sparkline-svg { width: 100%; display: block; }
.sparkline-loading {
  text-align: center;
  padding: 20px;
  color: var(--text-muted);
  font-size: 12px;
}
.chart-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}
.chart-range { display: flex; gap: 4px; align-items: center; }
.chart-range-label { font-size: 10px; color: var(--text-muted); margin-right: 2px; }
.chart-range-btn {
  padding: 3px 10px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
  border: 1px solid var(--border-light);
  background: var(--bg-card);
  color: var(--text-secondary);
  transition: all 0.2s;
}
.chart-range-btn:hover { border-color: var(--accent-blue); color: var(--text-primary); }
.chart-range-btn.active { background: var(--accent-blue); color: white; border-color: var(--accent-blue); }
.chart-legend {
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
  font-size: 10px;
  color: var(--text-muted);
  margin-bottom: 8px;
}
.chart-tooltip {
  position: absolute;
  z-index: 10;
  background: #0f172a;
  border: 1px solid var(--border-light);
  border-radius: 6px;
  padding: 8px 10px;
  font-size: 11px;
  color: var(--text-primary);
  pointer-events: none;
  box-shadow: var(--shadow);
  white-space: nowrap;
}
.chart-tooltip b { color: var(--accent-blue-light); }
.quality-chip { display:inline-block; padding:2px 8px; border-radius:6px; font-size:11px; font-weight:700; }.q-good { background:rgba(34,197,94,0.15); color:#34d399; border:1px solid rgba(34,197,94,0.3); }
.q-mid { background:rgba(234,179,8,0.15); color:#facc15; border:1px solid rgba(234,179,8,0.3); }
.q-bad { background:rgba(239,68,68,0.15); color:#f87171; border:1px solid rgba(239,68,68,0.3); }
.confirmed-badge { font-size:12px; cursor:help; }
.filter-info {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  background: rgba(139,92,246,0.08);
  border: 1px solid rgba(139,92,246,0.25);
  border-radius: var(--radius);
  padding: 10px 14px;
  margin: 0 0 12px;
  font-size: 12px;
  line-height: 1.5;
}
.filter-info-title { color: #c4b5fd; font-weight: 700; white-space: nowrap; }
.filter-info-text { color: var(--text-secondary); }
.filter-info-close {
  margin-left: auto;
  background: none;
  border: none;
  color: var(--text-muted);
  font-size: 13px;
  cursor: pointer;
}
.filter-info-close:hover { color: var(--text-primary); }
.tabs-bar {
  display: flex;
  gap: 8px;
  padding: 10px 20px;
  border-bottom: 1px solid var(--border);
  background: var(--bg-secondary);
}
.tab-btn {
  padding: 8px 24px;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 700;
  cursor: pointer;
  border: 1px solid var(--border-light);
  background: var(--bg-card);
  color: var(--text-secondary);
  transition: all 0.2s;
}
.tab-btn:hover { border-color: var(--accent-blue); color: var(--text-primary); }
.tab-btn.active { background: var(--accent-blue); color: white; border-color: var(--accent-blue); }
.ics-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 14px; }
/* Signal Groups */
.group-card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 12px 16px;
  margin-bottom: 8px;
  cursor: pointer;
  transition: all 0.15s ease;
}
.group-card:hover {
  background: var(--bg-card-hover);
  border-color: var(--accent-blue);
  transform: translateY(-1px);
}
.group-card .gc-header {
  display: flex; align-items: center; gap: 8px; margin-bottom: 6px;
}
.group-card .gc-emoji { font-size: 18px; }
.group-card .gc-name { font-weight: 700; font-size: 14px; color: var(--text-primary); }
.group-card .gc-count {
  margin-left: auto; background: rgba(59,130,246,0.15); color: #60a5fa;
  padding: 2px 10px; border-radius: 20px; font-size: 12px; font-weight: 700;
}
.group-card .gc-stocks {
  font-size: 12px; color: var(--text-secondary);
  display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px;
}
.group-card .gc-stock {
  background: var(--bg-secondary); padding: 2px 8px; border-radius: 6px;
  font-size: 11px; white-space: nowrap;
}
.group-card .gc-stock.buy { border-left: 2px solid #22c55e; }
.group-card .gc-stock.sell { border-left: 2px solid #ef4444; }
.summary-pill {
  display: inline-flex; align-items: center; gap: 6px;
  background: var(--bg-card); border: 1px solid var(--border);
  border-radius: 20px; padding: 6px 14px; font-size: 12px; font-weight: 600;
  color: var(--text-secondary); white-space: nowrap;
}
.summary-pill .sp-emoji { font-size: 14px; }
.summary-pill.highlight { border-color: #a855f7; background: rgba(168,85,247,0.1); color: #c084fc; }
.ics-card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 14px 16px;
  box-shadow: var(--shadow);
}
.idxdiag-card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 16px 18px;
  box-shadow: var(--shadow);
}
.idxdiag-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.idxdiag-name { font-size: 16px; font-weight: 700; color: var(--text-primary); }
.idxdiag-close { font-size: 20px; color: var(--text-muted); }
.idxdiag-regime { display: inline-block; padding: 3px 10px; border-radius: 12px; font-size: 11px; font-weight: 600; margin-left: 8px; }
.idxdiag-regime.choppy { background: rgba(251,191,36,0.15); color: #fbbf24; }
.idxdiag-regime.trending { background: rgba(34,197,94,0.15); color: #22c55e; }
.idxdiag-metrics { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 12px; }
.idxdiag-metric { background: rgba(255,255,255,0.03); border-radius: 8px; padding: 8px 10px; }
.idxdiag-metric-label { font-size: 10px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }
.idxdiag-metric-value { font-size: 15px; font-weight: 700; color: var(--text-primary); margin-top: 2px; }
.idxdiag-sr { margin-bottom: 12px; }
.idxdiag-sr-title { font-size: 11px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 6px; }
.idxdiag-levels { display: flex; flex-wrap: wrap; gap: 6px; }
.idxdiag-level { padding: 3px 8px; border-radius: 6px; font-size: 11px; font-weight: 600; }
.idxdiag-level.support { background: rgba(34,197,94,0.12); color: #22c55e; }
.idxdiag-level.resistance { background: rgba(239,68,68,0.12); color: #ef4444; }
.idxdiag-action { padding: 10px 12px; border-radius: 8px; border-left: 3px solid; }
.idxdiag-action.buy { background: rgba(34,197,94,0.08); border-color: #22c55e; }
.idxdiag-action.sell { background: rgba(239,68,68,0.08); border-color: #ef4444; }
.idxdiag-action.watch { background: rgba(251,191,36,0.08); border-color: #fbbf24; }
.idxdiag-action.wait { background: rgba(148,163,184,0.08); border-color: #94a3b8; }
.idxdiag-action-label { font-size: 12px; font-weight: 700; text-transform: uppercase; margin-bottom: 3px; }
.idxdiag-action-label.buy { color: #22c55e; }
.idxdiag-action-label.sell { color: #ef4444; }
.idxdiag-action-label.watch { color: #fbbf24; }
.idxdiag-action-label.wait { color: #94a3b8; }
.idxdiag-action-detail { font-size: 11px; color: var(--text-secondary); line-height: 1.4; }
.idxdiag-consolidation { height: 4px; background: rgba(255,255,255,0.05); border-radius: 2px; margin: 8px 0; }
.idxdiag-consolidation-fill { height: 100%; border-radius: 2px; transition: width 0.3s; }
.ics-head { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 8px; }
.ics-name { font-size: 15px; font-weight: 700; }
.ics-spot { font-size: 11px; color: var(--text-muted); }
.ics-drivers { font-size: 10px; color: var(--text-muted); margin: 2px 0 6px; }
.ics-regime { font-size: 12px; font-weight: 700; }
.ics-mid { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.ics-entry-type { font-size: 11px; font-weight: 700; border: 1px solid; border-radius: 6px; padding: 3px 9px; }
.ics-score { font-size: 11px; color: var(--text-muted); }
.ics-plan { font-size: 12px; padding: 8px 10px; border-radius: 6px; background: rgba(34,197,94,0.08); border: 1px solid rgba(34,197,94,0.2); }
.ics-plan.down { background: rgba(239,68,68,0.08); border-color: rgba(239,68,68,0.2); }
.ics-plan.muted { background: rgba(148,163,184,0.08); border-color: rgba(148,163,184,0.2); color: var(--text-muted); }
.ics-chart-toggle { margin-top: 10px; font-size: 11px; font-weight: 700; color: var(--accent-blue); cursor: pointer; }
.ics-chart-toggle:hover { text-decoration: underline; }
.ics-chart { margin-top: 8px; }
.strat-tag.watchlist { background:rgba(250,204,21,0.12); color:#facc15; border-color:rgba(250,204,21,0.2); }
.strat-tag.retracement { background:rgba(56,189,248,0.12); color:#38bdf8; border-color:rgba(56,189,248,0.2); }
.strat-tag.momentum { background:rgba(251,146,60,0.15); color:#fb923c; border-color:rgba(251,146,60,0.3); }
@media (max-width: 768px) {
  .table-container { overflow: auto; }
  .chart-legend { gap:8px; font-size:9px; }
  .chart-range-btn { padding:2px 6px; font-size:10px; }
}
.bm-expand-row .table-container { max-height: 280px; }

/* Empty State */
.empty-state {
  text-align: center;
  padding: 60px 20px;
  color: var(--text-muted);
}
.empty-state .icon { font-size: 48px; margin-bottom: 16px; opacity: 0.5; }
.empty-state h3 { font-size: 18px; margin-bottom: 8px; color: var(--text-secondary); }
.empty-state p { font-size: 13px; }

/* Spinner */
.spinner {
  display: inline-block;
  width: 16px; height: 16px;
  border: 2px solid rgba(255,255,255,0.2);
  border-top-color: white;
  border-radius: 50%;
  animation: spin 0.7s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* F&O Movers */
.sig{display:inline-block;padding:3px 10px;border-radius:6px;font-size:11px;font-weight:800;letter-spacing:0.4px;white-space:nowrap;animation:mvrpulse 2s infinite;}
.sig-buy{background:rgba(34,197,94,0.22);color:#4ade80;border:1px solid rgba(34,197,94,0.45);box-shadow:0 0 10px rgba(34,197,94,0.25);}
.sig-sell{background:rgba(239,68,68,0.22);color:#f87171;border:1px solid rgba(239,68,68,0.45);box-shadow:0 0 10px rgba(239,68,68,0.25);}
@keyframes mvrpulse{0%,100%{opacity:1}50%{opacity:0.65}}
.trk{display:inline-block;padding:1px 6px;border-radius:4px;font-size:10px;font-weight:700;margin-left:6px;letter-spacing:0.3px;}
.trk-buy{background:rgba(34,197,94,0.12);color:#4ade80;}
.trk-sell{background:rgba(239,68,68,0.12);color:#f87171;}
.signal-row{outline:1px solid rgba(34,197,94,0.35);outline-offset:-1px;}
.signal-row.sell-row{outline-color:rgba(239,68,68,0.35);}
.effbar{display:inline-block;vertical-align:middle;margin-left:4px;background:var(--bg2,var(--bg-secondary));border-radius:3px;height:5px;overflow:hidden;position:relative;}
.effbar i{display:block;height:100%;background:#22c55e;border-radius:3px;}
.movers-table tbody td{padding:7px 10px;border-top:1px solid var(--border);text-align:right;white-space:nowrap;font-size:12.5px;}
.movers-table tbody td:first-child{text-align:left;}
.movers-table tbody tr{cursor:pointer;transition:background 0.15s;}
.movers-table tbody tr:hover{background:var(--bg-table-hover);}
.movers-table thead th{padding:8px 10px;text-align:right;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color:var(--text-muted);background:var(--thead-bg);}
.movers-table thead th:first-child{text-align:left;}
#movers-modal .metric{background:var(--bg-secondary);border:1px solid var(--border);border-radius:10px;padding:10px 12px;}
#movers-modal .metric .k{font-size:10px;text-transform:uppercase;letter-spacing:0.5px;color:var(--text-muted);}
#movers-modal .metric .v{font-size:16px;font-weight:700;margin-top:2px;}
#movers-modal .metric .v.up{color:#4ade80;}
#movers-modal .metric .v.down{color:#f87171;}
#movers-modal .section-title{font-size:13px;font-weight:700;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.5px;margin:16px 0 8px;}
#movers-modal .tag{display:inline-block;padding:4px 12px;border-radius:20px;font-size:13px;font-weight:700;}
#movers-modal .tag.bull{background:rgba(34,197,94,0.15);color:#4ade80;}
#movers-modal .tag.bear{background:rgba(239,68,68,0.15);color:#f87171;}
#movers-modal .tag.neutral{background:rgba(234,179,8,0.15);color:#eab308;}
#movers-modal .hint{text-align:center;color:var(--text-muted);font-size:12px;padding:6px;}
#movers-modal .score-bar{background:var(--bg-secondary);height:8px;border-radius:4px;margin-top:4px;overflow:hidden;}
#movers-modal .score-fill{height:100%;border-radius:4px;transition:width 0.4s;}

/* Progress Bar */
.progress-bar {
  height: 3px;
  background: var(--bg-card);
  position: relative;
  overflow: hidden;
  display: none;
}
.progress-bar.active { display: block; }
.progress-bar::after {
  content: '';
  position: absolute;
  left: 0; top: 0;
  width: 30%;
  height: 100%;
  background: linear-gradient(90deg, var(--accent-blue), var(--accent-cyan));
  animation: progress-slide 1.5s ease-in-out infinite;
}
@keyframes progress-slide {
  0% { left: -30%; }
  100% { left: 100%; }
}

/* Toast */
.toast {
  position: fixed;
  bottom: 24px;
  right: 24px;
  background: var(--accent-green);
  color: white;
  padding: 12px 20px;
  border-radius: var(--radius-sm);
  font-size: 13px;
  font-weight: 600;
  display: none;
  z-index: 1000;
  box-shadow: 0 8px 32px rgba(34,197,94,0.3);
  animation: toast-in 0.3s ease;
}
@keyframes toast-in {
  from { opacity: 0; transform: translateY(20px); }
  to { opacity: 1; transform: translateY(0); }
}

/* Watchlist Panel */
.watchlist-overlay {
  position: fixed;
  top: 0; left: 0; right: 0; bottom: 0;
  background: rgba(0,0,0,0.5);
  z-index: 200;
  display: none;
  backdrop-filter: blur(4px);
}
.watchlist-overlay.open { display: block; }
.watchlist-panel {
  position: fixed;
  top: 0; right: -420px;
  width: 420px;
  height: 100vh;
  background: var(--bg-secondary);
  border-left: 1px solid var(--border);
  z-index: 201;
  transition: right 0.3s ease;
  display: flex;
  flex-direction: column;
}
.watchlist-panel.open { right: 0; }
.wl-header {
  padding: 20px;
  border-bottom: 1px solid var(--border);
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.wl-header h3 {
  font-size: 16px;
  color: var(--text-primary);
}
.wl-close {
  background: none;
  border: none;
  color: var(--text-muted);
  font-size: 20px;
  cursor: pointer;
  padding: 4px 8px;
  border-radius: 4px;
}
.wl-close:hover { background: var(--bg-card); color: var(--text-primary); }
.wl-add-section {
  padding: 16px 20px;
  border-bottom: 1px solid var(--border);
  display: flex;
  gap: 8px;
}
.wl-add-input {
  flex: 1;
  padding: 8px 12px;
  background: var(--bg-card);
  border: 1px solid var(--border-light);
  border-radius: var(--radius-xs);
  color: var(--text-primary);
  font-size: 13px;
  outline: none;
}
.wl-add-input:focus { border-color: var(--accent-blue); }
.wl-add-input::placeholder { color: var(--text-muted); }
.wl-add-btn {
  padding: 8px 16px;
  background: var(--accent-blue);
  color: white;
  border: none;
  border-radius: var(--radius-xs);
  font-weight: 600;
  font-size: 13px;
  cursor: pointer;
  white-space: nowrap;
}
.wl-add-btn:hover { background: #2563eb; }
.wl-notes-input {
  width: 100%;
  padding: 6px 10px;
  background: var(--bg-card);
  border: 1px solid var(--border-light);
  border-radius: var(--radius-xs);
  color: var(--text-primary);
  font-size: 12px;
  outline: none;
  margin-top: 8px;
}
.wl-notes-input:focus { border-color: var(--accent-blue); }
.wl-list {
  flex: 1;
  overflow-y: auto;
  padding: 12px 20px;
}
.wl-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px;
  background: var(--bg-card);
  border-radius: var(--radius-sm);
  margin-bottom: 8px;
  border: 1px solid var(--border);
  transition: all 0.2s;
}
.wl-item:hover { border-color: var(--border-light); }
.wl-item-icon {
  width: 36px; height: 36px;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 800;
  font-size: 11px;
  color: white;
  flex-shrink: 0;
}
.wl-item-info { flex: 1; }
.wl-item-name { font-weight: 700; font-size: 14px; color: var(--text-primary); }
.wl-item-symbol { font-size: 11px; color: var(--text-muted); }
.wl-item-notes { font-size: 11px; color: var(--text-secondary); margin-top: 2px; }
.wl-item-remove {
  background: none;
  border: none;
  color: var(--text-muted);
  font-size: 16px;
  cursor: pointer;
  padding: 4px 8px;
  border-radius: 4px;
}
.wl-item-remove:hover { background: rgba(239,68,68,0.15); color: var(--accent-red); }
.wl-empty {
  text-align: center;
  padding: 40px 20px;
  color: var(--text-muted);
}
.wl-empty .icon { font-size: 36px; margin-bottom: 12px; opacity: 0.5; }
.wl-empty p { font-size: 13px; }

/* Add to watchlist button on table rows */
.add-wl-btn {
  background: none;
  border: 1px solid var(--border-light);
  color: var(--text-muted);
  font-size: 12px;
  padding: 2px 8px;
  border-radius: 4px;
  cursor: pointer;
  transition: all 0.2s;
  white-space: nowrap;
}
.add-wl-btn:hover { border-color: var(--accent-yellow); color: var(--accent-yellow); }
.add-wl-btn.added { border-color: var(--accent-green); color: var(--accent-green); background: rgba(34,197,94,0.1); }

/* Shortcuts hint */
.shortcuts-hint {
  position: fixed;
  bottom: 24px;
  left: 24px;
  font-size: 11px;
  color: var(--text-muted);
  display: flex;
  gap: 16px;
}
.shortcuts-hint kbd {
  padding: 2px 6px;
  background: var(--bg-card);
  border: 1px solid var(--border-light);
  border-radius: 4px;
  font-family: inherit;
  font-size: 10px;
  margin-right: 4px;
}

/* Responsive */
@media (max-width: 768px) {
  .header { padding: 12px 16px; flex-wrap: wrap; gap: 10px; }
  .controls { flex-wrap: wrap; }
  .toolbar { padding: 10px 16px; }
  .search-box { flex: 0 0 100%; }
  .summary-cards { padding: 12px 16px; grid-template-columns: repeat(2, 1fr); }
  .main { padding: 0 16px 16px; }
  .shortcuts-hint { display: none; }
  tbody td { padding: 10px 12px; }
}
</style>
</head>
<body>

<!-- Header -->
<div class="header">
  <div class="header-left">
    <div>
      <div class="logo">JBK Scanner</div>
      <div class="logo-sub">Stock &amp; Index Options Dashboard</div>
    </div>
  </div>
  <div class="controls">
    <button class="btn btn-outline active" onclick="setTimeframe('D')" id="tf-daily">Daily</button>
    <button class="btn btn-outline" onclick="setTimeframe('15min')" id="tf-15">15 Min</button>
    <button class="btn btn-outline" onclick="setTimeframe('5min')" id="tf-5">5 Min</button>
    <div class="filter-divider"></div>
    <button class="btn btn-primary" onclick="triggerScan()" id="scan-btn">🔍 Scan Now</button>
    <button class="btn btn-success" onclick="toggleAuto()" id="auto-btn">▶ Auto</button>
    <button class="btn btn-ghost" onclick="exportCSV()" title="Export CSV">📥</button>
    <button class="btn btn-outline" onclick="toggleWatchlist()" id="wl-btn">⭐ Watchlist (<span id="wl-count">0</span>)</button>
    <button class="btn btn-ghost" onclick="cycleTheme()" id="theme-btn" title="Change background / theme">🎨</button>
    <button class="btn btn-ghost" onclick="testTelegram()" id="tg-btn" title="Test Telegram Alerts">📱</button>
  </div>
</div>

<!-- Market Status -->
<div class="market-bar">
  <span class="market-status">
    <span class="pulse-dot green" id="status-dot"></span>
    <span id="status-text">Ready</span>
  </span>
  <span class="stat">Last scan: <strong id="last-scan">Never</strong></span>
  <span class="stat">Signals: <strong id="signal-count">0</strong></span>
  <span class="stat" id="market-clock"></span>
</div>

<!-- Progress Bar -->
<div class="progress-bar" id="progress-bar"></div>

<!-- Tabs: Stocks vs Indices vs Movers -->
<div class="tabs-bar">
  <button class="tab-btn active" id="tab-stocks-btn" onclick="switchTab('stocks')">📈 Stocks</button>
  <button class="tab-btn" id="tab-indices-btn" onclick="switchTab('indices')">📉 Indices</button>
  <button class="tab-btn" id="tab-movers-btn" onclick="switchTab('movers')">🎯 F&O Movers</button>
  <button class="tab-btn" id="tab-backtest-btn" onclick="switchTab('backtest')">🧪 Backtest</button>
  <button class="tab-btn" id="tab-sectors-btn" onclick="switchTab('sectors')">🏭 Sectors</button>
</div>
<div id="stocks-tab">

<!-- Summary Cards -->
<div class="summary-cards" id="summary-cards">
  <div class="summary-card card-total">
    <div class="card-label">Total Signals</div>
    <div class="card-value" id="card-total">0</div>
    <div class="card-sub">Across all strategies</div>
  </div>
  <div class="summary-card card-buy">
    <div class="card-label">Buy Signals</div>
    <div class="card-value" id="card-buy">0</div>
    <div class="card-sub" id="card-buy-sub">Normal + Strong</div>
  </div>
  <div class="summary-card card-sell">
    <div class="card-label">Sell Signals</div>
    <div class="card-value" id="card-sell">0</div>
    <div class="card-sub" id="card-sell-sub">Normal + Strong</div>
  </div>
  <div class="summary-card card-strong">
    <div class="card-label">Very Strong</div>
    <div class="card-value" id="card-strong">0</div>
    <div class="card-sub">3+ strategies agree</div>
  </div>
  <div class="summary-card card-range">
    <div class="card-label">Breakout</div>
    <div class="card-value" id="card-range">0</div>
    <div class="card-sub">Range + Channel break</div>
  </div>
  <div class="summary-card card-early">
    <div class="card-label">Early + 52W</div>
    <div class="card-value" id="card-early">0</div>
    <div class="card-sub">Pre-breakout + support</div>
  </div>
  <div class="summary-card card-retrace">
    <div class="card-label">Retracement</div>
    <div class="card-value" id="card-retrace">0</div>
    <div class="card-sub">Buy the dip</div>
  </div>
</div>

<!-- Toolbar -->
<div class="toolbar">
  <div class="search-box">
    <span class="search-icon">🔎</span>
    <input type="text" id="search-input" placeholder="Search stock name or symbol..." oninput="debouncedApplyFilters()" autocomplete="off">
  </div>
  <div class="filter-divider"></div>
  <div class="filter-group">
    <button class="filter-btn active" onclick="filterType('all')" id="f-all" title="Show both buy and sell signals">All</button>
    <button class="filter-btn" onclick="filterType('BUY')" id="f-buy" title="Only BUY signals — bullish setups">🟢 Buy</button>
    <button class="filter-btn" onclick="filterType('SELL')" id="f-sell" title="Only SELL signals — bearish setups">🔴 Sell</button>
  </div>
  <div class="filter-divider"></div>
  <div class="filter-group" id="strategy-filters">
    <button class="filter-btn active" onclick="filterStrategy('all')" id="fs-all" title="Show signals from every strategy">All Strategies</button>
    <button class="filter-btn" onclick="filterStrategy('range')" id="fs-range" title="Breakout above 9/15/21/60-day range high">📊 Range Breakout</button>
    <button class="filter-btn" onclick="filterStrategy('Channel Consolidation Breakout')" id="fs-ch" title="Bollinger squeeze + breakout">🔲 Channel</button>
    <button class="filter-btn" onclick="filterStrategy('Early Breakout')" id="fs-early" title="Early entry before the breakout">⚡ Early</button>
    <button class="filter-btn" onclick="filterStrategy('52W High Support Buy')" id="fs-high" title="Buy pullback to support near 52-week high">🏔️ 52W</button>
    <button class="filter-btn" onclick="filterStrategy('Candlestick Pattern')" id="fs-candle" title="Reversal candles (Engulfing, Hammer, etc.)">🕯️ Candle</button>
    <button class="filter-btn" onclick="filterStrategy('Volume Shocker')" id="fs-vol" title="Volume ≥ 2x average with strong candle">🔊 Volume</button>
    <button class="filter-btn" onclick="filterStrategy('Med Channel Breakout')" id="fs-medch" title="30-day channel + squeeze + candle confirm">📐 Med CH</button>
    <button class="filter-btn" onclick="filterStrategy('Watchlist Range Breakout')" id="fs-wl" title="Fresh breakout above 15-day high, SL at support">⭐ Watchlist</button>
    <button class="filter-btn" onclick="filterStrategy('Buy on Retracement')" id="fs-retrace" title="Buy the dip in an uptrend at EMA20 support">🔁 Retracement</button>
    <button class="filter-btn" onclick="filterStrategy('Trendline Channel Breakout')" id="fs-chbrk" title="Ascending / descending / horizontal channel breakout">📈 Ch Brk</button>
    <button class="filter-btn" onclick="filterStrategy('Momentum Breakout')" id="fs-momentum" title="Trend + consolidation + volume breakout + VWAP, ATM option strike">🚀 Momentum</button>
    <button class="filter-btn" onclick="toggleConfirmed()" id="fs-confirmed" title="Only signals passing volume gate + hold + pullback confirmation">✅ Confirmed</button>
    <button class="filter-btn bigmoney-toggle" onclick="toggleBigMoney()" id="bm-toggle">💎 F&O Big Money</button>
  </div>
  <div class="toolbar-right">
    <span class="result-count" id="result-count"></span>
  </div>
</div>

<!-- Active filter explanation -->
<div class="filter-info" id="filter-info" style="display:none">
  <span class="filter-info-title" id="filter-info-title"></span>
  <span class="filter-info-text" id="filter-info-text"></span>
  <button class="filter-info-close" onclick="closeFilterInfo()" title="Dismiss">✕</button>
</div>

<!-- Main Table -->
<div class="main">
  <div class="table-container" id="signals-table-container">
    <table id="signals-table">
      <thead>
        <tr>
          <th onclick="sortBy('symbol_name')" id="th-symbol">Symbol <span class="sort-arrow">↕</span></th>
          <th onclick="sortBy('strategy_count')" id="th-strength">Strength <span class="sort-arrow">↕</span></th>
          <th>Strategies</th>
          <th onclick="sortBy('quality_score')" id="th-quality" style="cursor:pointer">Quality <span class="sort-arrow">↕</span></th>
          <th onclick="sortBy('price')" id="th-price">Price <span class="sort-arrow">↕</span></th>
          <th>Stop Loss</th>
          <th>Target</th>
          <th onclick="sortBy('confidence')" id="th-conf">Confidence <span class="sort-arrow">↕</span></th>
          <th>R:R</th>
          <th>Reason</th>
        </tr>
      </thead>
      <tbody id="signals-body"></tbody>
    </table>
    <div class="empty-state" id="empty-state">
      <div class="icon">📡</div>
      <h3>No signals yet</h3>
      <p>Click <strong>Scan Now</strong> or press <kbd>S</kbd> to start scanning 208+ F&O stocks</p>
    </div>
  </div>

  <!-- Index Signals Section (separate column — index-only strategies) -->
  <div class="index-section" id="index-section" style="margin-top:24px;display:none">
    <h3 class="section-title" style="font-size:15px;font-weight:700;color:var(--text-secondary);margin-bottom:12px">
      📈 Index Signals <span id="index-count" style="color:var(--text-muted);font-weight:500;font-size:12px"></span>
    </h3>
    <div class="toolbar" style="margin-bottom:12px;flex-wrap:wrap">
      <div class="filter-group">
        <span class="filter-label" style="font-size:11px;color:var(--text-muted);font-weight:700;margin-right:6px">Strategy:</span>
        <button class="filter-btn active" onclick="filterIndexSignals('all')" id="ifs-all">All</button>
        <button class="filter-btn" onclick="filterIndexSignals('Trendline Channel Breakout')" id="ifs-chbrk" title="Ascending / descending / horizontal channel breakout">📈 Ch Brk</button>
        <button class="filter-btn" onclick="filterIndexSignals('Index Range Breakout')" id="ifs-irb" title="15-min consolidation breakout">📊 Index RB</button>
        <button class="filter-btn" onclick="filterIndexSignals('Index Support/Resistance')" id="ifs-isr" title="Choppy market support/resistance">🎯 Index S/R</button>
        <button class="filter-btn" onclick="filterIndexSignals('Momentum Breakout')" id="ifs-momentumbreakout" title="Trend + consolidation + volume breakout + VWAP, ATM option strike">🚀 Momentum</button>
      </div>
    </div>
    <div class="table-container">
      <table id="index-table">
        <thead>
          <tr>
            <th>Symbol</th>
            <th>Strength</th>
            <th>Strategies</th>
            <th>Price</th>
            <th>Stop Loss</th>
            <th>Target</th>
            <th>Confidence</th>
            <th>R:R</th>
          </tr>
        </thead>
        <tbody id="index-body"></tbody>
      </table>
      <div class="empty-state" id="index-empty">
        <div class="icon">📊</div>
        <h3>No index signals</h3>
        <p>Indices use breakout strategies only (Range / Channel / Early)</p>
      </div>
    </div>
  </div>

  <!-- Index Chart Strategy Section -->
  <div class="index-section" id="chartstrategy-section" style="margin-top:24px;display:none">
    <h3 class="section-title" style="font-size:15px;font-weight:700;color:var(--text-secondary);margin-bottom:12px">
      📈 Index Strategy <span id="chartstrategy-count" style="color:var(--text-muted);font-weight:500;font-size:12px"></span>
    </h3>
    <div class="toolbar" id="chartstrategy-filters" style="margin-bottom:12px;flex-wrap:wrap">
      <div class="filter-group">
        <span class="filter-label" style="font-size:11px;color:var(--text-muted);font-weight:700;margin-right:6px">Regime:</span>
        <button class="filter-btn active" onclick="filterIcs('regime','all')" id="icf-r-all">All</button>
        <button class="filter-btn" onclick="filterIcs('regime','BULLISH')" id="icf-r-BULLISH" title="Only BULLISH — CALL setups">🟢 BULLISH</button>
        <button class="filter-btn" onclick="filterIcs('regime','BEARISH')" id="icf-r-BEARISH" title="Only BEARISH — PUT setups">🔴 BEARISH</button>
        <button class="filter-btn" onclick="filterIcs('regime','RANGE')" id="icf-r-RANGE" title="Only RANGE — no directional">🟡 RANGE</button>
      </div>
      <div class="filter-divider"></div>
      <div class="filter-group">
        <span class="filter-label" style="font-size:11px;color:var(--text-muted);font-weight:700;margin-right:6px">Entry:</span>
        <button class="filter-btn active" onclick="filterIcs('entry','all')" id="icf-e-all">All</button>
        <button class="filter-btn" onclick="filterIcs('entry','BREAKOUT')" id="icf-e-BREAKOUT" title="Breakout above range high">🚀 Breakout</button>
        <button class="filter-btn" onclick="filterIcs('entry','PULLBACK')" id="icf-e-PULLBACK" title="Pullback to EMA20">🔻 Pullback</button>
        <button class="filter-btn" onclick="filterIcs('entry','CANDLE')" id="icf-e-CANDLE" title="Candle confirmation">🕯️ Candle</button>
        <button class="filter-btn" onclick="filterIcs('entry','WAIT')" id="icf-e-WAIT" title="No qualifying entry">⏸ Wait</button>
      </div>
    </div>
    <div class="table-container">
      <table id="chartstrategy-table">
        <thead>
          <tr>
            <th>Index</th>
            <th>Regime</th>
            <th>Conf</th>
            <th>ADX</th>
            <th>RSI</th>
            <th>Position</th>
            <th>Entry</th>
            <th>Score</th>
            <th>Trade</th>
          </tr>
        </thead>
        <tbody id="chartstrategy-body"></tbody>
      </table>
      <div class="empty-state" id="chartstrategy-empty" style="display:none">
        <div class="icon">📊</div>
        <h3>No strategy data</h3>
        <p>Run <code>python -m scanner.index_chart_strategy</code> to generate</p>
      </div>
    </div>
  </div>

  <!-- Big Money Section (unusual stock options activity) -->
  <div class="index-section" id="bigmoney-section" style="margin-top:24px;display:none">
    <h3 class="section-title" style="font-size:15px;font-weight:700;color:var(--text-secondary);margin-bottom:12px">
      💎 Big Money — Unusual Stock Options <span id="bigmoney-count" style="color:var(--text-muted);font-weight:500;font-size:12px"></span>
    </h3>
    <div class="table-container">
      <table id="bigmoney-table">
        <thead>
          <tr>
            <th>Stock</th>
            <th>Signal</th>
            <th>Activity</th>
            <th>Score</th>
            <th>Mode</th>
            <th>Nearby Strikes</th>
          </tr>
        </thead>
        <tbody id="bigmoney-body"></tbody>
      </table>
      <div class="empty-state" id="bigmoney-empty">
        <div class="icon">💎</div>
        <h3>No big-money activity</h3>
        <p>Run <code>python -m scanner.big_money --scan</code> to scan stock options (indices excluded)</p>
      </div>
    </div>
  </div>
</div>
</div><!-- end stocks-tab -->

<!-- Indices Tab -->
<div id="indices-tab" style="display:none"></div>

<!-- F&O Movers Tab -->
<div id="movers-tab" style="display:none">
  <div class="container" style="padding:24px 28px">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:6px;flex-wrap:wrap;gap:8px">
      <h3 style="font-size:20px;font-weight:800;background:linear-gradient(135deg,#3b82f6,#06b6d4);-webkit-background-clip:text;-webkit-text-fill-color:transparent">
        🎯 F&O Movers — Top Gainers &amp; Losers
      </h3>
      <span id="movers-status" style="color:var(--text-muted);font-weight:500;font-size:12px"></span>
    </div>
    <div style="font-size:11px;color:var(--text-muted);margin-bottom:16px;line-height:1.6">
      TRACKING MODE: opened &ge;2% vs prev close — highlighted while price holds above the open &nbsp;|&nbsp;
      Signal closes when price drops below the open &nbsp;|&nbsp;
      Stages: EARLY 1.1% &rarr; BUY 1.5% &rarr; STRONG 2% (goal 5%+) &nbsp;|&nbsp;
      Double-click a row for share strength &amp; technical analysis
    </div>
    <div class="movers-cards" style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px">
      <div class="movers-card" style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;overflow:hidden">
        <div style="padding:10px 14px;font-size:13px;font-weight:700;color:#4ade80;background:rgba(34,197,94,0.10);border-bottom:1px solid var(--border)">
          🟢 TOP 10 GAINERS <span id="movers-gain-count" style="font-size:11px;color:var(--text-muted);font-weight:600"></span>
        </div>
        <div class="table-container" style="max-height:calc(100vh - 240px);overflow:auto">
          <table id="movers-gain-table" class="movers-table" style="font-size:12.5px">
            <thead><tr>
              <th>Stock</th><th>LTP</th><th>% Chg</th><th>From Open</th><th>High</th><th>Low</th><th>Eff</th><th>Vol</th><th>Signal</th>
            </tr></thead>
            <tbody id="movers-gain-body"></tbody>
          </table>
        </div>
      </div>
      <div class="movers-card" style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;overflow:hidden">
        <div style="padding:10px 14px;font-size:13px;font-weight:700;color:#f87171;background:rgba(239,68,68,0.10);border-bottom:1px solid var(--border)">
          🔴 TOP 10 LOSERS <span id="movers-lose-count" style="font-size:11px;color:var(--text-muted);font-weight:600"></span>
        </div>
        <div class="table-container" style="max-height:calc(100vh - 240px);overflow:auto">
          <table id="movers-lose-table" class="movers-table" style="font-size:12.5px">
            <thead><tr>
              <th>Stock</th><th>LTP</th><th>% Chg</th><th>From Open</th><th>High</th><th>Low</th><th>Eff</th><th>Vol</th><th>Signal</th>
            </tr></thead>
            <tbody id="movers-lose-body"></tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- Movers Analysis Modal -->
<div class="watchlist-overlay" id="movers-modal-overlay" style="display:none;z-index:300" onclick="if(event.target===this)closeMoversModal()"></div>
<div id="movers-modal" style="display:none;position:fixed;inset:0;z-index:301;align-items:center;justify-content:center;padding:20px;pointer-events:none">
  <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:14px;width:100%;max-width:680px;max-height:90vh;overflow:auto;pointer-events:auto;padding:0">
    <div style="padding:14px 18px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;background:linear-gradient(135deg,#111827,#1a1f35);position:sticky;top:0;border-radius:14px 14px 0 0">
      <h3 id="movers-modal-title" style="font-size:16px">—</h3>
      <button onclick="closeMoversModal()" style="cursor:pointer;background:none;border:none;color:var(--text-muted);font-size:22px">✕</button>
    </div>
    <div id="movers-modal-body" style="padding:16px 18px"></div>
  </div>
</div>

<!-- ==================== BACKTEST TAB ==================== -->
<div id="backtest-tab" style="display:none">
  <div class="container" style="padding:24px 28px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;flex-wrap:wrap;gap:8px">
      <h3 style="font-size:20px;font-weight:800;background:linear-gradient(135deg,#a855f7,#ec4899);-webkit-background-clip:text;-webkit-text-fill-color:transparent">🧪 Strategy Backtest</h3>
      <div style="display:flex;gap:8px;align-items:center">
        <select id="bt-max-symbols" style="background:var(--bg-secondary);color:var(--text-primary);border:1px solid var(--border);border-radius:8px;padding:6px 12px;font-size:12px">
          <option value="20">20 stocks</option>
          <option value="30" selected>30 stocks</option>
          <option value="50">50 stocks</option>
          <option value="100">100 stocks</option>
        </select>
        <select id="bt-hold-days" style="background:var(--bg-secondary);color:var(--text-primary);border:1px solid var(--border);border-radius:8px;padding:6px 12px;font-size:12px">
          <option value="5">5 day hold</option>
          <option value="10" selected>10 day hold</option>
          <option value="15">15 day hold</option>
          <option value="20">20 day hold</option>
        </select>
        <button id="bt-run-btn" onclick="runBacktest()" style="background:linear-gradient(135deg,#a855f7,#ec4899);color:white;border:none;border-radius:8px;padding:8px 16px;font-size:13px;font-weight:700;cursor:pointer">▶ Run Backtest</button>
      </div>
    </div>
    <div id="bt-status" style="margin-bottom:12px;color:var(--text-muted);font-size:12px"></div>
    <div id="bt-summary" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:20px"></div>
    <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:14px;overflow:hidden">
      <table style="width:100%;border-collapse:collapse">
        <thead><tr style="background:var(--thead-bg)">
          <th style="padding:12px 16px;text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color:var(--text-muted)">Strategy</th>
          <th style="padding:12px 12px;text-align:center;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color:var(--text-muted)">Grade</th>
          <th style="padding:12px 12px;text-align:right;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color:var(--text-muted)">Trades</th>
          <th style="padding:12px 12px;text-align:right;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color:var(--text-muted)">Win Rate</th>
          <th style="padding:12px 12px;text-align:right;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color:var(--text-muted)">PF</th>
          <th style="padding:12px 12px;text-align:right;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color:var(--text-muted)">Expectancy</th>
          <th style="padding:12px 12px;text-align:right;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color:var(--text-muted)">Avg P&L</th>
          <th style="padding:12px 12px;text-align:right;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color:var(--text-muted)">Total P&L</th>
          <th style="padding:12px 12px;text-align:right;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color:var(--text-muted)">Max DD</th>
          <th style="padding:12px 12px;text-align:right;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color:var(--text-muted)">Sharpe</th>
          <th style="padding:12px 12px;text-align:right;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color:var(--text-muted)">Avg Win</th>
          <th style="padding:12px 12px;text-align:right;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color:var(--text-muted)">Avg Loss</th>
        </tr></thead>
        <tbody id="bt-body"></tbody>
      </table>
    </div>
    <div id="bt-empty" style="text-align:center;padding:60px 20px;color:var(--text-muted)">
      <div style="font-size:48px;margin-bottom:12px">🧪</div>
      <div style="font-size:16px;font-weight:600;margin-bottom:4px">No backtest results yet</div>
      <div style="font-size:13px">Click "Run Backtest" to evaluate all strategies on historical data</div>
    </div>
  </div>
</div>

<!-- ==================== SECTORS TAB ==================== -->
<div id="sectors-tab" style="display:none">
  <div class="container" style="padding:24px 28px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <h3 style="font-size:20px;font-weight:800;background:linear-gradient(135deg,#f59e0b,#f97316);-webkit-background-clip:text;-webkit-text-fill-color:transparent">🏭 Sector Analysis</h3>
      <span id="sectors-status" style="color:var(--text-muted);font-size:12px"></span>
    </div>
    <div id="sectors-summary" style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px"></div>
    <div id="sectors-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:12px"></div>
    <div id="sectors-empty" style="text-align:center;padding:60px 20px;color:var(--text-muted)">
      <div style="font-size:48px;margin-bottom:12px">🏭</div>
      <div style="font-size:16px;font-weight:600;margin-bottom:4px">No sector data</div>
      <div style="font-size:13px">Run a scan first, then switch to Sectors tab</div>
    </div>
  </div>
</div>

<!-- Toast -->
<!-- Watchlist Overlay -->
<div class="watchlist-overlay" id="wl-overlay" onclick="toggleWatchlist()"></div>

<!-- Watchlist Panel -->
<div class="watchlist-panel" id="wl-panel">
  <div class="wl-header">
    <h3>⭐ My Watchlist</h3>
    <button class="wl-close" onclick="toggleWatchlist()">✕</button>
  </div>
  <div class="wl-add-section">
    <div style="flex:1">
      <input type="text" class="wl-add-input" id="wl-add-symbol" placeholder="Add stock (e.g. TCS, RELIANCE)" onkeypress="if(event.key==='Enter')addToWatchlist()">
      <input type="text" class="wl-notes-input" id="wl-add-notes" placeholder="Notes (optional)">
    </div>
    <button class="wl-add-btn" onclick="addToWatchlist()">+ Add</button>
  </div>
  <div class="wl-list" id="wl-list"></div>
</div>

<div class="toast" id="toast"></div>

<!-- Shortcuts -->
<div class="shortcuts-hint">
  <span><kbd>S</kbd> Scan</span>
  <span><kbd>/</kbd> Search</span>
  <span><kbd>Esc</kbd> Close</span>
</div>

<script>
// ==================== STATE ====================
let allSignals = [];
let filteredSignals = [];
let allIndexSignals = [];
let qualityStats = {};
let livePrices = {};
let currentType = 'all';
let currentConfirmed = false;
let _lastSignalsSig = '';
let _lastBmSig = '';
let currentStrategy = 'all';
let currentTimeframe = 'D';
let autoEnabled = false;
let sortField = 'quality_score';
let sortDir = 'desc';
let selectedRow = null;

// ==================== STRATEGY TAG CSS ====================
function stratTagClass(name) {
  const n = name.toLowerCase();
  if (n.includes('index range')) return 'indexrb';
  if (n.includes('index support')) return 'indexsr';
  if (n.includes('med channel')) return 'medch';
  if (n.includes('range')) return 'range';
  if (n.includes('channel')) return 'channel';
  if (n.includes('early')) return 'early';
  if (n.includes('52w')) return 'high52w';
  if (n.includes('candle')) return 'candle';
  if (n.includes('volume')) return 'volume';
  if (n.includes('watchlist')) return 'watchlist';
  if (n.includes('retracement')) return 'retracement';
  if (n.includes('channel breakout') && n.includes('trendline')) return 'channel';
  if (n.includes('momentum')) return 'momentum';
  return '';
}

function qualityChip(s) {
  const qs = s.quality_score;
  if (qs === undefined || qs === null) return '<span class="quality-chip" style="opacity:0.35">—</span>';
  const tier = s.quality_tier || 'MODERATE';
  const colors = {'VERY HIGH':'#22c55e','HIGH':'#4ade80','MODERATE':'#eab308','LOW':'#f97316','VERY LOW':'#ef4444'};
  const emojis = {'VERY HIGH':'🔥','HIGH':'✅','MODERATE':'⚡','LOW':'⚠️','VERY LOW':'🚫'};
  const c = colors[tier] || '#94a3b8';
  const e = emojis[tier] || '';
  return `<span class="quality-chip" style="background:${c}18;color:${c};border:1px solid ${c}44;font-weight:700;cursor:default" title="Quality: ${qs}/100 (${tier})">
    ${e} ${qs}
  </span>`;
}

function strengthBadgeClass(strength) {
  const s = (strength || '').toLowerCase().replace(/ /g, '-');
  return 'strength-' + s;
}

function symbolColor(name) {
  const colors = ['#3b82f6','#22c55e','#a855f7','#f97316','#06b6d4','#eab308','#ec4899','#ef4444','#14b8a6','#8b5cf6'];
  let hash = 0;
  for (let i = 0; i < name.length; i++) hash = name.charCodeAt(i) + ((hash << 5) - hash);
  return colors[Math.abs(hash) % colors.length];
}

// ==================== TIMEFRAME ====================
function setTimeframe(tf) {
  currentTimeframe = tf;
  document.querySelectorAll('[id^="tf-"]').forEach(b => b.classList.remove('active'));
  const id = tf === 'D' ? 'tf-daily' : tf === '15min' ? 'tf-15' : 'tf-5';
  document.getElementById(id).classList.add('active');
}

// ==================== FILTERS ====================
function filterType(type) {
  currentType = type;
  document.querySelectorAll('#f-all,#f-buy,#f-sell').forEach(b => {
    b.classList.remove('active', 'active-green', 'active-red');
  });
  if (type === 'all') document.getElementById('f-all').classList.add('active');
  else if (type === 'BUY') document.getElementById('f-buy').classList.add('active', 'active-green');
  else document.getElementById('f-sell').classList.add('active', 'active-red');
  applyFilters();
}

function filterStrategy(strat) {
  currentStrategy = strat;
  document.querySelectorAll('[id^="fs-"]').forEach(b => b.classList.remove('active'));
  const map = {
    'all': 'fs-all', 'range': 'fs-range',
    'Channel Consolidation Breakout': 'fs-ch', 'Early Breakout': 'fs-early',
    '52W High Support Buy': 'fs-high', 'Candlestick Pattern': 'fs-candle',
    'Volume Shocker': 'fs-vol',
    'Med Channel Breakout': 'fs-medch',
    'Watchlist Range Breakout': 'fs-wl',
    'Buy on Retracement': 'fs-retrace',
    'Trendline Channel Breakout': 'fs-chbrk',
    'Momentum Breakout': 'fs-momentum'
  };
  const el = document.getElementById(map[strat] || 'fs-all');
  if (el) el.classList.add('active');
  applyFilters();
  updateFilterInfo();
}

// Range breakout = one filter covering 9D / 15D / 21D / 60D
const STRATEGY_GROUPS = {
  'range': ['Range Breakout 9D', 'Range Breakout 15D', 'Range Breakout 21D', 'Range Breakout 60D']
};

function debounce(fn, ms) { let t; return function() { clearTimeout(t); t = setTimeout(fn, ms); }; }
const debouncedApplyFilters = debounce(applyFilters, 200);
function applyFilters() {
  const search = document.getElementById('search-input').value.toLowerCase().trim();
  filteredSignals = allSignals.filter(s => {
    if (currentType !== 'all' && s.signal_type !== currentType) return false;
    if (currentStrategy !== 'all') {
      const strats = s.strategies || [s.strategy];
      const group = STRATEGY_GROUPS[currentStrategy];
      if (group) {
        if (!strats.some(st => group.includes(st))) return false;
      } else if (!strats.includes(currentStrategy)) {
        return false;
      }
    }
    if (currentConfirmed && !s.confirmed) return false;
    if (search) {
      const name = (s.symbol_name || '').toLowerCase();
      const symbol = (s.symbol || '').toLowerCase();
      if (!name.includes(search) && !symbol.includes(search)) return false;
    }
    return true;
  });
  sortSignals();
  renderTable();
  updateCounts();
}

function toggleConfirmed() {
  currentConfirmed = !currentConfirmed;
  const btn = document.getElementById('fs-confirmed');
  if (btn) btn.classList.toggle('active', currentConfirmed);
  applyFilters();
  updateFilterInfo();
}

// ==================== THEMES (background / colour) ====================
const THEMES = {
  dark: {
    '--bg-primary': '#0a0e1a', '--bg-secondary': '#111827', '--bg-card': '#1a1f35',
    '--bg-card-hover': '#222842', '--bg-table-row': '#0f1424', '--bg-table-hover': '#1a2040',
    '--border': '#1e293b', '--border-light': '#2d3a52', '--thead-bg': '#151b30',
    '--text-primary': '#f1f5f9', '--text-secondary': '#94a3b8', '--text-muted': '#64748b',
    '--shadow': '0 4px 24px rgba(0,0,0,0.4)', '--shadow-lg': '0 8px 40px rgba(0,0,0,0.5)'
  },
  light: {
    '--bg-primary': '#eef2f7', '--bg-secondary': '#e2e8f0', '--bg-card': '#ffffff',
    '--bg-card-hover': '#f8fafc', '--bg-table-row': '#f8fafc', '--bg-table-hover': '#eef2f7',
    '--border': '#cbd5e1', '--border-light': '#94a3b8', '--thead-bg': '#e2e8f0',
    '--text-primary': '#0f172a', '--text-secondary': '#334155', '--text-muted': '#64748b',
    '--shadow': '0 4px 24px rgba(15,23,42,0.08)', '--shadow-lg': '0 8px 40px rgba(15,23,42,0.12)'
  },
  midnight: {
    '--bg-primary': '#05070d', '--bg-secondary': '#0b0f1a', '--bg-card': '#0f1524',
    '--bg-card-hover': '#141b2e', '--bg-table-row': '#0a0f1a', '--bg-table-hover': '#111827',
    '--border': '#1a2236', '--border-light': '#243149', '--thead-bg': '#0b101d',
    '--text-primary': '#e2e8f0', '--text-secondary': '#8b98ab', '--text-muted': '#5b6675',
    '--shadow': '0 4px 24px rgba(0,0,0,0.6)', '--shadow-lg': '0 8px 40px rgba(0,0,0,0.7)'
  }
};
const THEME_ORDER = ['dark', 'light', 'midnight'];

function setTheme(name) {
  const vars = THEMES[name] || THEMES['dark'];
  Object.keys(vars).forEach(k => document.documentElement.style.setProperty(k, vars[k]));
  try { localStorage.setItem('fyers_theme', name); } catch(e) {}
  const btn = document.getElementById('theme-btn');
  if (btn) btn.title = 'Theme: ' + name;
}

function cycleTheme() {
  const cur = (() => { try { return localStorage.getItem('fyers_theme'); } catch(e) { return null; } })() || 'dark';
  const next = THEME_ORDER[(THEME_ORDER.indexOf(cur) + 1) % THEME_ORDER.length];
  setTheme(next);
}

// ==================== TABS (Stocks / Indices) ====================
let activeTab = 'stocks';

function setupTabs() {
  const stocksTab = document.getElementById('stocks-tab');
  const indicesTab = document.getElementById('indices-tab');
  if (!stocksTab || !indicesTab) return;
  // Stock content into the Stocks tab (summary cards, toolbar, info, main table + big money)
  ['summary-cards', 'toolbar', 'filter-info'].forEach(id => {
    const el = document.getElementById(id);
    if (el) stocksTab.appendChild(el);
  });
  const mainDiv = document.querySelector('div.main');
  if (mainDiv) stocksTab.appendChild(mainDiv);
  // Index content into the Indices tab
  ['index-section', 'chartstrategy-section'].forEach(id => {
    const el = document.getElementById(id);
    if (el) indicesTab.appendChild(el);
  });
}

function switchTab(name) {
  activeTab = name;
  const tabs = ['stocks', 'indices', 'movers', 'backtest', 'sectors'];
  const ids = { stocks: 'stocks-tab', indices: 'indices-tab', movers: 'movers-tab', backtest: 'backtest-tab', sectors: 'sectors-tab' };
  const btns = { stocks: 'tab-stocks-btn', indices: 'tab-indices-btn', movers: 'tab-movers-btn', backtest: 'tab-backtest-btn', sectors: 'tab-sectors-btn' };
  tabs.forEach(t => {
    const el = document.getElementById(ids[t]); if (el) el.style.display = 'none';
    const btn = document.getElementById(btns[t]); if (btn) btn.classList.remove('active');
  });
  const el = document.getElementById(ids[name]); if (el) el.style.display = 'block';
  const btn = document.getElementById(btns[name]); if (btn) btn.classList.add('active');
  if (name === 'stocks') updateFilterInfo();
  else if (name === 'movers') fetchMovers();
  else if (name === 'backtest') loadBacktest();
  else if (name === 'indices') fetchChartStrategy();
  else if (name === 'sectors') loadSectors();
}

// ==================== STRATEGY NARRATION ====================
const STRATEGY_INFO = {
  'all': { t: 'All Strategies', d: 'Showing signals from every strategy. Click a strategy filter to focus on one setup.' },
  'range': { t: 'Range Breakout (9/15/21/60D)', d: 'BUY when the close breaks above the range high with a 3-day rising volume trend. 4 lookback periods catch different timing: 9D (fast), 15D (medium), 21D (medium-slow), 60D (major trend change). SL = 1.5 ATR below the range high. Target = +3.5% short-term.' },
  'Channel Consolidation Breakout': { t: 'Channel Consolidation Breakout', d: 'Price squeezed in a tight Bollinger Band (low volatility), then breaks out with volume and RSI confirmation. SL = middle band. Target = band width (~3.5%).' },
  'Early Breakout': { t: 'Early Breakout', d: 'Catches the move BEFORE the breakout: price near the range high + 2 consecutive higher closes + volume. Enters early, so SL is tighter (1 ATR below range high).' },
  '52W High Support Buy': { t: '52W High Support Buy', d: 'Stock within 7% of its 52-week high that pulled back to EMA20/50 support, with RSI > 40, price above EMA50, healthy volume. Target = 52W-high area / +3.5%.' },
  'Candlestick Pattern': { t: 'Candlestick Pattern', d: 'Reversal/continuation candles (Engulfing, Hammer, Morning Star, Marubozu...) near the range high/low. Low-confidence patterns are filtered out; SELLs only when strongly bearish (conf ≥ 0.7).' },
  'Volume Shocker': { t: 'Volume Shocker', d: 'Volume ≥ 2x average with a strong green candle near the day high, above EMA20. Big money participating. Target = +3.5%, SL below the day low.' },
  'Med Channel Breakout': { t: 'Med Channel Breakout', d: '30-day channel + squeeze + candlestick confirmation (needs pattern score ≥ 2). SL = 0.5 ATR below channel. Target = channel width, capped 3.5%.' },
  'Watchlist Range Breakout': { t: 'Watchlist Range Breakout', d: 'FRESH close above the recent 15-day high (yesterday at/below the high). Support = max(9 DMA, 21 DMA, swing low). SL = support. Target = swing high + 4.8%. Ignored if close falls below support.' },
  'Buy on Retracement': { t: 'Buy on Retracement', d: 'BUY the dip in an uptrend: price above EMA20/50 (rising), pulled back ≥ 2% from swing high, tapped EMA20 support, bullish reversal candle + RSI ≥ 45 + quiet volume. Target = swing high + 4.8%. Breakeven trail: SL → cost−1% once +1% in profit.' },
  'Trendline Channel Breakout': { t: 'Trendline Channel Breakout', d: 'Detects a parallel-line channel from swing highs/lows. ASCENDING (both lines up) → close above upper line = BUY. DESCENDING (both lines down) → close below lower line = SELL. HORIZONTAL (flat = square block) → above resistance = BUY / below support = SELL. SL = opposite channel line, target = channel height capped 3.5%.' },
  'Momentum Breakout': { t: '🚀 Momentum Breakout with Confirmation', d: 'Momentum rider: (1) Primary trend EMA20 vs EMA50, (2) strong prior move, (3) 3-5 bar tight consolidation, (4) breakout bar with ≥1.5x average volume, (5) price on the right side of VWAP. BUY ATM option. SL = below breakout candle low. TP1 = 1:2 R:R, TP2 = 1:3 R:R.' },
  'confirmed': { t: '✅ Confirmed Setups', d: 'ONLY signals passing ALL 3 confirmation rules: VOL (volume ≥ 1.5x avg), HOLD (close above stop, no weak close), PB (entry within 2.5 ATR of EMA20 — not chasing). Highest-quality entries.' },
  'bigmoney': { t: '💎 F&O Big Money', d: 'Unusual activity in stock options (indices excluded): OI jumps + volume bursts + premium moves. fresh_buying (OI↑ + premium↑) = big player expecting a move. Expiry day & day before are skipped.' },
  'regime': { t: '📈 Index Regime', d: 'Market state per index: BULLISH → buy CALL, BEARISH → buy PUT, RANGE → no directional / sell strangle. Confidence < 60 = wait. Run python -m scanner.regime to refresh.' }
};

function updateFilterInfo() {
  const el = document.getElementById('filter-info');
  if (!el) return;
  let info = null;
  if (bigMoneyView) {
    info = STRATEGY_INFO['bigmoney'];
  } else if (currentConfirmed) {
    info = STRATEGY_INFO['confirmed'];
  } else if (currentStrategy !== 'all') {
    info = STRATEGY_INFO[currentStrategy];
  }
  if (!info) { el.style.display = 'none'; return; }
  document.getElementById('filter-info-title').textContent = info.t;
  document.getElementById('filter-info-text').textContent = info.d;
  el.style.display = 'block';
}

function closeFilterInfo() {
  document.getElementById('filter-info').style.display = 'none';
}

// ==================== SORTING ====================
function sortBy(field) {
  if (sortField === field) {
    sortDir = sortDir === 'asc' ? 'desc' : 'asc';
  } else {
    sortField = field;
    sortDir = field === 'symbol_name' ? 'asc' : 'desc';
  }
  document.querySelectorAll('thead th').forEach(th => th.classList.remove('sorted-asc', 'sorted-desc'));
  const th = document.getElementById('th-' + (field === 'strategy_count' ? 'strength' : field === 'confidence' ? 'conf' : field === 'symbol_name' ? 'symbol' : field));
  if (th) th.classList.add(sortDir === 'asc' ? 'sorted-asc' : 'sorted-desc');
  sortSignals();
  renderTable();
}

function sortSignals() {
  filteredSignals.sort((a, b) => {
    let va, vb;
    if (sortField === 'symbol_name') {
      va = a.symbol_name || '';
      vb = b.symbol_name || '';
      return sortDir === 'asc' ? va.localeCompare(vb) : vb.localeCompare(va);
    }
    va = a[sortField] || 0;
    vb = b[sortField] || 0;
    return sortDir === 'asc' ? va - vb : vb - va;
  });
}

// ==================== RENDER ====================
function renderTable() {
  const tbody = document.getElementById('signals-body');
  const empty = document.getElementById('empty-state');

  if (filteredSignals.length === 0) {
    tbody.innerHTML = '';
    empty.style.display = 'block';
    document.getElementById('result-count').textContent = '';
    return;
  }
  empty.style.display = 'none';
  document.getElementById('result-count').textContent = `Showing ${filteredSignals.length} of ${allSignals.length}`;

  tbody.innerHTML = filteredSignals.map((s, idx) => {
    const strats = s.strategies || [s.strategy || ''];
    const stratTags = strats.map(name => {
      const cls = stratTagClass(name);
      const shortName = name.replace('Range Breakout ', 'R').replace('Consolidation Breakout', 'Consolidation').replace(' Support Buy', '').replace(' Pattern', '');
      return `<span class="strat-tag ${cls}">${shortName}</span>`;
    }).join('');

    const rr = s.price && s.stop_loss && s.target ?
      ((s.target - s.price) / Math.max(0.01, s.price - s.stop_loss)).toFixed(1) : '—';

    const badgeClass = strengthBadgeClass(s.strength || s.signal_type);
    const emoji = s.emoji || (s.signal_type === 'BUY' ? '🟢' : '🔴');
    const color = symbolColor(s.symbol_name || '');

    const confPct = ((s.confidence || 0) * 100).toFixed(0);
    const confColor = confPct >= 80 ? '#22c55e' : confPct >= 60 ? '#eab308' : '#f97316';

    return `
    <tr onclick="toggleExpand(${idx})" class="${selectedRow === idx ? 'selected' : ''}" data-idx="${idx}">
      <td>
        <div class="symbol-cell">
          <div class="symbol-icon" style="background:${color}">${(s.symbol_name||'?').substring(0,2)}</div>
          <div class="symbol-info">
            <span class="symbol-name">${s.symbol_name}${s.confirmed ? ' <span class="confirmed-badge" title="Confirmed: ' + (s.conf_rules || []).join(' + ') + '">✅</span>' : ''}</span>
            <span class="symbol-tag">${s.symbol}</span>
          </div>
          <button class="add-wl-btn ${_watchlistSymbols.has(s.symbol)?'added':''}" onclick="event.stopPropagation();toggleStockWatchlist('${s.symbol}','${s.symbol_name}')" title="Add to watchlist">${_watchlistSymbols.has(s.symbol)?'✓':'⭐'}</button>
        </div>
      </td>
      <td><span class="strength-badge ${badgeClass}">${emoji} ${s.strength || s.signal_type}</span></td>
      <td><div class="strat-tags">${stratTags}</div></td>
      <td>${qualityChip(s)}</td>
      <td class="price-cell" data-price="${s.symbol}" data-entry="${s.price}">₹${Number(s.price).toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2})}</td>
      <td class="sl-cell">₹${Number(s.stop_loss).toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2})}</td>
      <td class="target-cell">₹${Number(s.target).toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2})}</td>
      <td class="confidence-cell" style="color:${confColor}">${confPct}%</td>
      <td><span class="risk-reward">1:${rr}</span></td>
      <td style="font-size:11px;color:var(--text-muted);max-width:250px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${(s.reasons || [s.reason] || []).join(' | ')}">${(s.reasons || [s.reason] || []).join(' | ').substring(0, 60)}</td>
    </tr>`;
  }).join('');
}

let _lastToggleRow = null;
let _lastToggleTime = 0;
function buildExpandHTML(idx) {
  const s = filteredSignals[idx];
  if (!s) return '';
  const strats = s.strategies || [s.strategy || ''];
  const rr = s.price && s.stop_loss && s.target ?
    ((s.target - s.price) / Math.max(0.01, s.price - s.stop_loss)).toFixed(1) : '—';
  const confPct = ((s.confidence || 0) * 100).toFixed(0);
  const confColor = confPct >= 80 ? '#22c55e' : confPct >= 60 ? '#eab308' : '#f97316';
  return `<tr class="expand-row open" id="expand-${idx}"><td colspan="10"><div class="expand-content"><div class="expand-grid">
    <div class="expand-item"><div class="expand-label">Current Price</div><div class="expand-value" data-live-price="${s.symbol}" data-live-entry="${s.price}" style="font-size:18px;font-weight:700;color:var(--text-primary)">₹${Number(s.price).toLocaleString('en-IN',{minimumFractionDigits:2})}</div></div>
    <div class="expand-item"><div class="expand-label">Stop Loss</div><div class="expand-value" style="color:var(--accent-red)">₹${Number(s.stop_loss).toLocaleString('en-IN',{minimumFractionDigits:2})}</div></div>
    <div class="expand-item"><div class="expand-label">Target</div><div class="expand-value" style="color:var(--accent-green)">₹${Number(s.target).toLocaleString('en-IN',{minimumFractionDigits:2})}</div></div>
    <div class="expand-item"><div class="expand-label">Risk:Reward</div><div class="expand-value" style="font-weight:700">1:${rr}</div></div>
    <div class="expand-item"><div class="expand-label">Confidence</div><div class="expand-value" style="color:${confColor};font-weight:700">${confPct}%</div></div>
    <div class="expand-item"><div class="expand-label">Timeframe</div><div class="expand-value">${s.timeframe}</div></div>
    <div class="expand-item"><div class="expand-label">Strategies (${strats.length})</div><div class="expand-value">${strats.join(', ')}</div></div>
  </div><div class="sparkline-container" id="chart-${idx}"><div class="sparkline-loading"><span class="spinner"></span> Loading 30-day chart...</div></div>
  <div class="expand-reasons"><div class="expand-reasons-title">📋 Signal Analysis</div>${(s.reasons||[s.reason]||[]).map(r=>`<div class="reason-item">${r}</div>`).join('')}</div></div></td></tr>`;
}
function toggleExpand(idx) {
  const now = Date.now();
  if (idx === _lastToggleRow && now - _lastToggleTime < 350) return;
  _lastToggleRow = idx; _lastToggleTime = now;
  document.querySelectorAll('.expand-row.open').forEach(r => r.remove());
  document.querySelectorAll('tbody tr[data-idx]').forEach(tr => tr.classList.remove('selected'));
  const mainRow = document.querySelector(`tr[data-idx="${idx}"]`);
  if (selectedRow === idx) { selectedRow = null; return; }
  selectedRow = idx;
  if (mainRow) {
    mainRow.classList.add('selected');
    mainRow.insertAdjacentHTML('afterend', buildExpandHTML(idx));
    loadChart(idx, 30);
  }
}

// ==================== CHART (candlesticks + EMAs + S/R + signal levels) ====================
const _chartCache = {};

async function loadChart(idx, days = 30) {
  const s = filteredSignals[idx];
  if (!s) return;
  const container = document.getElementById('chart-' + idx);
  if (!container) return;
  container.style.position = 'relative';
  const key = s.symbol + '_' + days;
  if (_chartCache[key]) {
    drawChart(container, _chartCache[key], s, days, idx);
    return;
  }
  try {
    const resp = await fetch(`/api/history?symbol=${encodeURIComponent(s.symbol)}&days=${days}`);
    const data = await resp.json();
    if (data.candles && data.candles.length > 0) {
      _chartCache[key] = data;
      drawChart(container, data, s, days, idx);
    } else {
      container.innerHTML = '<div class="sparkline-loading">No chart data available</div>';
    }
  } catch(e) {
    container.innerHTML = '<div class="sparkline-loading">Chart load failed</div>';
  }
}

function chartRangeButtons(idx, days, isIndex) {
  const ranges = [[30,'1M'],[90,'3M'],[180,'6M'],[365,'1Y']];
  const loader = isIndex ? 'loadIndexChart' : 'loadChart';
  return '<div class="chart-range"><span class="chart-range-label">Range:</span>' +
    ranges.map(([d,label]) => `<button class="chart-range-btn ${d===days?'active':''}" onclick="event.stopPropagation();${loader}(${idx},${d})">${label}</button>`).join('') +
    '</div>';
}

function drawChart(container, data, signal, days, idx, isIndex) {
  const candles = data.candles || [];
  if (!candles.length) { container.innerHTML = '<div class="sparkline-loading">No chart data</div>'; return; }
  const ema9 = data.ema9 || [], ema21 = data.ema21 || [], ema50 = data.ema50 || [];
  const rsi = data.rsi || [];
  const support = data.support, resistance = data.resistance;
  const high52 = data.high_52w, low52 = data.low_52w;

  const W = 960, PADL = 56, PADR = 14, PADT = 18, PRICE_H = 280, VOL_H = 64, RSI_H = 70, XLABEL_H = 18;
  const H = PADT + PRICE_H + VOL_H + RSI_H + XLABEL_H;
  const chartW = W - PADL - PADR;
  const n = candles.length;
  const step = chartW / n;
  const bw = Math.max(2, step * 0.62);
  const x = i => PADL + i * step + step / 2;
  const yVolBase = PADT + PRICE_H;
  const yRsiBase = yVolBase + VOL_H;
  const rsiY = v => yRsiBase + (1 - v / 100) * RSI_H;

  const highs = candles.map(c => c.high), lows = candles.map(c => c.low);
  let minP = Math.min(...lows), maxP = Math.max(...highs);
  [ema9, ema21, ema50].forEach(a => a.forEach(v => {
    if (v != null && !isNaN(v)) { if (v < minP) minP = v; if (v > maxP) maxP = v; }
  }));
  [support, resistance, high52, low52, signal && signal.price, signal && signal.stop_loss, signal && signal.target]
    .forEach(v => { if (v != null && v > 0) { if (v < minP) minP = v; if (v > maxP) maxP = v; } });
  const pad = (maxP - minP) * 0.07 || maxP * 0.02;
  minP -= pad; maxP += pad;
  const range = maxP - minP || 1;
  const y = v => PADT + (1 - (v - minP) / range) * PRICE_H;

  const maxVol = Math.max(...candles.map(c => c.volume)) || 1;
  let o = '';

  // grid + y labels
  for (let i = 0; i <= 5; i++) {
    const val = minP + range * i / 5, yy = y(val);
    o += `<line x1="${PADL}" y1="${yy}" x2="${W-PADR}" y2="${yy}" stroke="#1e293b" stroke-width="0.5" stroke-dasharray="3,3"/>`;
    o += `<text x="${PADL-6}" y="${yy+3}" fill="#64748b" font-size="9" text-anchor="end">${val.toFixed(0)}</text>`;
  }

  // volume bars
  candles.forEach((c, i) => {
    const bh = (c.volume / maxVol) * (VOL_H - 4);
    const col = c.close >= c.open ? 'rgba(34,197,94,0.4)' : 'rgba(239,68,68,0.4)';
    o += `<rect x="${x(i)-bw/2}" y="${yVolBase+VOL_H-2-bh}" width="${bw}" height="${bh}" fill="${col}" rx="1"/>`;
  });

  // EMA lines
  const emaPath = (arr, color) => {
    let p = '', started = false;
    for (let i = 0; i < arr.length; i++) {
      const v = arr[i];
      if (v == null || isNaN(v)) continue;
      p += (started ? 'L' : 'M') + x(i).toFixed(1) + ',' + y(v).toFixed(1);
      started = true;
    }
    return started ? `<path d="${p}" fill="none" stroke="${color}" stroke-width="1.3" stroke-linejoin="round" opacity="0.9"/>` : '';
  };
  o += emaPath(ema9, '#06b6d4');
  o += emaPath(ema21, '#eab308');
  o += emaPath(ema50, '#a855f7');

  // RSI subpanel
  for (const lv of [70, 50, 30]) {
    const yy = rsiY(lv);
    o += `<line x1="${PADL}" y1="${yy}" x2="${W-PADR}" y2="${yy}" stroke="#334155" stroke-width="0.5" stroke-dasharray="3,3"/>`;
    o += `<text x="${PADL-6}" y="${yy+3}" fill="#64748b" font-size="9" text-anchor="end">${lv}</text>`;
  }
  o += `<text x="${W-PADR-4}" y="${rsiY(86)}" fill="#64748b" font-size="9" text-anchor="end">RSI</text>`;
  let rsiP = '', rsiStart = false;
  for (let i = 0; i < rsi.length; i++) {
    const v = rsi[i];
    if (v == null || isNaN(v)) continue;
    rsiP += (rsiStart ? 'L' : 'M') + x(i).toFixed(1) + ',' + rsiY(Math.max(0, Math.min(100, v))).toFixed(1);
    rsiStart = true;
  }
  const rsiLast = rsi[rsi.length - 1];
  const rsiColor = (rsiLast == null || isNaN(rsiLast)) ? '#64748b' : (rsiLast >= 50 ? '#22c55e' : '#ef4444');
  if (rsiStart) o += `<path d="${rsiP}" fill="none" stroke="${rsiColor}" stroke-width="1.3" stroke-linejoin="round" opacity="0.9"/>`;
  const rsiLastVal = rsiLast != null && !isNaN(rsiLast) ? rsiLast.toFixed(1) : '—';
  o += `<text x="${PADL+4}" y="${rsiY(14)}" fill="${rsiColor}" font-size="10" font-weight="700">RSI ${rsiLastVal}</text>`;

  // support / resistance / signal levels
  const level = (val, color, label, dash) => {
    if (val == null || val <= 0 || isNaN(val)) return;
    const yy = y(val);
    o += `<line x1="${PADL}" y1="${yy}" x2="${W-PADR}" y2="${yy}" stroke="${color}" stroke-width="1.1" stroke-dasharray="${dash}" opacity="0.85"/>`;
    o += `<text x="${PADL+3}" y="${yy-3}" fill="${color}" font-size="9" font-weight="700">${label} ${Number(val).toFixed(2)}</text>`;
  };
  level(high52, '#64748b', '52W-H', '4,3');
  level(resistance, '#f97316', 'RES', '6,4');
  level(support, '#22c55e', 'SUP', '6,4');
  level(low52, '#64748b', '52W-L', '4,3');
  if (signal && signal.price) {
    level(signal.price, '#3b82f6', 'ENTRY', '2,3');
    level(signal.stop_loss, '#ef4444', 'SL', '6,3');
    level(signal.target, '#22c55e', 'TGT', '6,3');
  }

  // candlesticks
  candles.forEach((c, i) => {
    const bull = c.close >= c.open;
    const col = bull ? '#22c55e' : '#ef4444';
    o += `<line x1="${x(i)}" y1="${y(c.high)}" x2="${x(i)}" y2="${y(c.low)}" stroke="${col}" stroke-width="1"/>`;
    const by1 = y(Math.max(c.open, c.close)), by2 = y(Math.min(c.open, c.close));
    o += `<rect x="${x(i)-bw/2}" y="${by1}" width="${bw}" height="${Math.max(1, by2-by1)}" fill="${col}" rx="1"/>`;
  });

  // x date labels
  const stepIdx = Math.max(1, Math.floor(n / 7));
  for (let i = 0; i < n; i += stepIdx) {
    const d = new Date(candles[i].date * 1000);
    o += `<text x="${x(i)}" y="${H-4}" fill="#64748b" font-size="9" text-anchor="middle">${d.toLocaleDateString('en-IN',{day:'2-digit',month:'short'})}</text>`;
  }

  // last price marker
  const lastC = candles[n-1];
  const lastCol = lastC.close >= lastC.open ? '#22c55e' : '#ef4444';
  o += `<circle cx="${x(n-1)}" cy="${y(lastC.close)}" r="3.5" fill="${lastCol}" stroke="#0c1020" stroke-width="1.5"/>`;
  o += `<text x="${x(n-1)+6}" y="${y(lastC.close)+3}" fill="${lastCol}" font-size="10" font-weight="700">${lastC.close.toFixed(2)}</text>`;

  // stats
  const change = ((lastC.close - candles[0].close) / candles[0].close * 100);
  const chCls = change >= 0 ? 'up' : 'down';
  const chSign = change >= 0 ? '+' : '';
  const avgVol = (candles.reduce((a,c)=>a+c.volume,0)/n/100000).toFixed(1);
  const rr = (signal && signal.price && signal.stop_loss && signal.target) ?
    ((signal.target - signal.price)/Math.max(0.01, signal.price - signal.stop_loss)).toFixed(1) : '—';
  const nm = (signal && signal.symbol_name) || (data.symbol || '');
  const tipId = 'tip-' + (signal ? signal.symbol.replace(/[^A-Z0-9]/gi,'') : 'x') + '-' + days;

  container.innerHTML = `
    <div class="chart-head">
      <div>
        <span class="sparkline-title">📈 ${nm} — Daily</span>
        <div class="sparkline-stats">
          <span class="sparkline-stat">${days}D H/L: <strong>${Math.max(...highs).toFixed(2)} / ${Math.min(...lows).toFixed(2)}</strong></span>
          <span class="sparkline-stat">Change: <strong class="${chCls}">${chSign}${change.toFixed(2)}%</strong></span>
          <span class="sparkline-stat">Avg Vol: <strong>${avgVol}L</strong></span>
          <span class="sparkline-stat">R:R: <strong>1:${rr}</strong></span>
        </div>
      </div>
      ${chartRangeButtons(idx, days, isIndex)}
    </div>
    <div class="chart-legend">
      <span style="color:#06b6d4">— EMA9</span>
      <span style="color:#eab308">— EMA21</span>
      <span style="color:#a855f7">— EMA50</span>
      <span style="color:#f97316">RES ${resistance != null ? resistance : '—'}</span>
      <span style="color:#22c55e">SUP ${support != null ? support : '—'}</span>
      <span style="color:#3b82f6">ENTRY ${signal && signal.price ? signal.price : '—'}</span>
      <span style="color:#ef4444">SL ${signal && signal.stop_loss ? signal.stop_loss : '—'}</span>
      <span style="color:#22c55e">TGT ${signal && signal.target ? signal.target : '—'}</span>
    </div>
    <div class="chart-wrap" style="position:relative">
      <svg class="sparkline-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
        ${o}
        <line id="${tipId}-vx" x1="0" y1="${PADT}" x2="0" y2="${yRsiBase+RSI_H}" stroke="#64748b" stroke-width="0.8" stroke-dasharray="3,3" opacity="0.65" style="display:none"/>
        <line id="${tipId}-hx" x1="${PADL}" y1="0" x2="${W-PADR}" y2="0" stroke="#64748b" stroke-width="0.8" stroke-dasharray="3,3" opacity="0.65" style="display:none"/>
      </svg>
      <div class="chart-tooltip" id="${tipId}" style="display:none"></div>
    </div>`;

  // interactive hover tooltip + crosshair
  const wrap = container.querySelector('.chart-wrap');
  const svg = wrap.querySelector('svg');
  const vx = wrap.querySelector('#' + tipId + '-vx');
  const hx = wrap.querySelector('#' + tipId + '-hx');
  svg.addEventListener('mousemove', e => {
    const rect = svg.getBoundingClientRect();
    const scaleX = W / rect.width;
    const scaleY = H / rect.height;
    const px = (e.clientX - rect.left) * scaleX;
    const py = (e.clientY - rect.top) * scaleY;
    let cidx = Math.floor((px - PADL) / step);
    if (cidx < 0) cidx = 0;
    if (cidx >= n) cidx = n - 1;
    const c = candles[cidx];
    const d = new Date(c.date * 1000);
    const t = document.getElementById(tipId);
    if (!t) return;

    // Crosshair lines
    const cvx = x(cidx);
    if (vx) { vx.setAttribute('x1', cvx); vx.setAttribute('x2', cvx); vx.style.display = 'block'; }
    if (hx) { hx.setAttribute('y1', py); hx.setAttribute('y2', py); hx.style.display = 'block'; }

    // Candle details
    const prevC = candles[cidx - 1];
    let chg = '';
    if (prevC) {
      const p = ((c.close - prevC.close) / prevC.close) * 100;
      chg = ` | Δ <span style="color:${p>=0?'#22c55e':'#ef4444'}">${p>=0?'+':''}${p.toFixed(2)}%</span>`;
    }
    const emaVals = [['E9', ema9[cidx]], ['E21', ema21[cidx]], ['E50', ema50[cidx]]]
      .map(([k, v]) => v != null && !isNaN(v) ? `${k}:${v.toFixed(0)}` : '').filter(Boolean).join(' ');
    const rsiV = rsi[cidx];
    const rsiStr = rsiV != null && !isNaN(rsiV) ? ` | RSI ${rsiV.toFixed(1)}` : '';
    t.innerHTML = `<b>${d.toLocaleDateString('en-IN', {day:'2-digit', month:'short', year:'numeric'})}</b> ${c.close>=c.open?'▲':'▼'}<br>` +
      `O:${c.open.toFixed(2)} H:${c.high.toFixed(2)} L:${c.low.toFixed(2)} C:${c.close.toFixed(2)}<br>` +
      `V:${(c.volume/100000).toFixed(1)}L ${chg}<br>${emaVals}${rsiStr}`;

    const tRect = t.getBoundingClientRect();
    const svgRect = svg.getBoundingClientRect();
    const tipX = (cvx / W) * svgRect.width;
    const tipY = (py / H) * svgRect.height;
    t.style.display = 'block';
    // Flip tooltip near right/bottom edges so it stays on screen
    t.style.left = Math.min(tipX + 10, Math.max(0, svgRect.width - tRect.width - 4)) + 'px';
    t.style.top = Math.min(tipY + 10, Math.max(0, svgRect.height - tRect.height - 4)) + 'px';
  });
  svg.addEventListener('mouseleave', () => {
    const t = document.getElementById(tipId);
    if (t) t.style.display = 'none';
    if (vx) vx.style.display = 'none';
    if (hx) hx.style.display = 'none';
  });
}

// ==================== SUMMARY CARDS ====================
function updateSummaryCards() {
  const total = allSignals.length;
  const buy = allSignals.filter(s => s.signal_type === 'BUY').length;
  const sell = allSignals.filter(s => s.signal_type === 'SELL').length;
  const veryStrong = allSignals.filter(s => (s.strength || '').includes('VERY STRONG')).length;
  const breakout = allSignals.filter(s => {
    const strats = s.strategies || [];
    return strats.some(st => st.includes('Range') || st.includes('Channel') || st.includes('Momentum'));
  }).length;
  const early = allSignals.filter(s => {
    const strats = s.strategies || [];
    return strats.some(st => st.includes('Early') || st.includes('52W'));
  }).length;

  const retrace = allSignals.filter(s => {
    const strats = s.strategies || [];
    return strats.some(st => st.includes('Retracement'));
  }).length;

  document.getElementById('card-total').textContent = total;
  document.getElementById('card-buy').textContent = buy;
  document.getElementById('card-sell').textContent = sell;
  document.getElementById('card-strong').textContent = veryStrong;
  document.getElementById('card-range').textContent = breakout;
  document.getElementById('card-early').textContent = early;
  document.getElementById('card-retrace').textContent = retrace;
}

function updateCounts() {
  document.getElementById('signal-count').textContent = allSignals.length;
  document.getElementById('card-total').textContent = allSignals.length;
}

// ==================== SCAN ====================
async function triggerScan() {
  const btn = document.getElementById('scan-btn');
  btn.innerHTML = '<span class="spinner"></span> Scanning...';
  btn.disabled = true;
  document.getElementById('status-text').innerHTML = '<span class="spinner"></span> Scanning all stocks...';
  document.getElementById('status-dot').className = 'pulse-dot yellow';
  document.getElementById('progress-bar').classList.add('active');

  await fetch('/api/scan?timeframe=' + currentTimeframe);

  // Poll until scan complete
  let attempts = 0;
  const poll = setInterval(async () => {
    const resp = await fetch('/api/status');
    const st = await resp.json();
    if (!st.scanning || attempts > 120) {
      clearInterval(poll);
      await fetchSignals();
      btn.innerHTML = '🔍 Scan Now';
      btn.disabled = false;
      document.getElementById('progress-bar').classList.remove('active');
      showToast('Scan complete! Found ' + allSignals.length + ' signals');
    }
    attempts++;
  }, 2000);
}

async function fetchSignals() {
  const resp = await fetch('/api/signals');
  const data = await resp.json();
  allSignals = data.signals || [];
  allIndexSignals = data.index_signals || [];
  document.getElementById('last-scan').textContent = data.last_scan ? new Date(data.last_scan).toLocaleTimeString() : 'Never';
  if (!data.scanning) {
    document.getElementById('status-text').textContent = 'Ready';
    document.getElementById('status-dot').className = 'pulse-dot green';
  }
  // Only re-render if the data actually changed — otherwise keep expanded
  // rows (charts / big-money details) open so nothing auto-closes.
  const sig = JSON.stringify(allSignals.map(s => s.symbol + s.signal_type + s.price + s.target))
    + '|' + JSON.stringify(allIndexSignals.map(s => s.symbol + s.signal_type + s.price + s.target));
  if (sig !== _lastSignalsSig) {
    _lastSignalsSig = sig;
    applyFilters();
    updateSummaryCards();
    renderIndexTable();
  }
  Promise.allSettled([fetchQuality(), fetchBigMoney(), fetchChartStrategy()]);
}

// ==================== SIGNAL QUALITY ====================
async function fetchQuality() {
  try {
    const resp = await fetch('/api/quality');
    const data = await resp.json();
    qualityStats = data.strategies || {};
  } catch(e) {}
}

// ==================== INDEX CHART STRATEGY ====================
let _lastIcsSig = '';
// ==================== SECTORS ====================
let _sectorsData = null;

async function loadSectors() {
  try {
    const resp = await fetch('/api/signal-groups');
    const data = await resp.json();
    if (data.error) return;
    _sectorsData = data;
    renderSectors(data);
  } catch(e) {}
}

function renderSectors(data) {
  const grid = document.getElementById('sectors-grid');
  const empty = document.getElementById('sectors-empty');
  const status = document.getElementById('sectors-status');
  const summary = document.getElementById('sectors-summary');
  if (!grid) return;
  const sectors = data.sectors || [];
  if (sectors.length === 0) { grid.innerHTML = ''; empty.style.display = 'block'; return; }
  empty.style.display = 'none';
  status.textContent = new Date().toLocaleTimeString();

  // Summary
  const total = sectors.reduce((s, g) => s + g.count, 0);
  summary.innerHTML = (data.summary || []).map(s =>
    `<span class="summary-pill"><span class="sp-emoji">${s.emoji}</span>${s.text}</span>`
  ).join('');

  // Grid
  grid.innerHTML = sectors.map(g => {
    const stocks = (g.signals || []).slice(0, 8).map(s => {
      const cls = s.signal_type === 'BUY' ? 'buy' : 'sell';
      const score = s.quality_score ? ` <span style="font-size:10px;color:${s.quality_score>=60?'#22c55e':s.quality_score>=40?'#eab308':'#f97316'}">${s.quality_score}</span>` : '';
      return `<span class="gc-stock ${cls}" title="${s.symbol_name} - ${s.strategy || ''}${score}">${s.symbol_name || s.symbol}${score}</span>`;
    }).join('');
    const more = g.count > 8 ? `<span class="gc-stock">+${g.count - 8} more</span>` : '';
    const buys = (g.signals || []).filter(s => s.signal_type === 'BUY').length;
    const sells = g.count - buys;
    return `<div class="group-card">
      <div class="gc-header">
        <span class="gc-emoji">${g.emoji}</span>
        <span class="gc-name">${g.group_name}</span>
        <span class="gc-count">${g.count} <span style="font-size:10px;font-weight:400;opacity:0.7">(${buys}B/${sells}S)</span></span>
      </div>
      <div class="gc-stocks">${stocks}${more}</div>
    </div>`;
  }).join('');
}

async function fetchChartStrategy() {
  try {
    const resp = await fetch('/api/chartstrategy');
    const data = await resp.json();
    const sig = JSON.stringify((data.indices || []).map(i => i.name + i.regime + i.regime_confidence + (i.entry ? i.entry.type : '')));
    if (sig !== _lastIcsSig) {
      _lastIcsSig = sig;
      renderChartStrategy(data.indices || []);
    }
  } catch(e) {}
}

let icsRegimeFilter = 'all';
let icsEntryFilter = 'all';
function filterIcs(kind, value) {
  if (kind === 'regime') icsRegimeFilter = value;
  else icsEntryFilter = value;
  document.querySelectorAll('#chartstrategy-filters .filter-btn').forEach(b => b.classList.remove('active'));
  const id = kind === 'regime' ? 'icf-r-' + value : 'icf-e-' + value;
  const el = document.getElementById(id);
  if (el) el.classList.add('active');
  renderChartStrategy(window._icsIndices || []);
}

function renderChartStrategy(indices) {
  const section = document.getElementById('chartstrategy-section');
  const tbody = document.getElementById('chartstrategy-body');
  const empty = document.getElementById('chartstrategy-empty');
  document.getElementById('chartstrategy-count').textContent = `(${indices.length})`;
  if (!indices.length) { section.style.display = 'none'; return; }
  section.style.display = 'block';
  empty.style.display = 'none';
  window._icsIndices = indices;

  const filtered = indices.filter(i => {
    if (icsRegimeFilter !== 'all' && i.regime !== icsRegimeFilter) return false;
    const etype = i.entry ? i.entry.type : 'WAIT';
    if (icsEntryFilter !== 'all' && etype !== icsEntryFilter) return false;
    return true;
  });
  window._icsFiltered = filtered;

  tbody.innerHTML = filtered.map((i, idx) => {
    const e = i.entry;
    const plan = i.plan;
    const drv = i.drivers || {};
    const bull = i.regime === 'BULLISH';
    const bear = i.regime === 'BEARISH';
    const rng = i.regime === 'RANGE';
    const rc = bull ? '#22c55e' : bear ? '#ef4444' : '#facc15';
    const regimeTag = bull ? '🟢 BULLISH' : bear ? '🔴 BEARISH' : '🟡 RANGE';
    const hasTrade = !!plan;
    const etype = e ? e.type : '—';
    const etColor = etype === 'BREAKOUT' || etype === 'BREAKDOWN' ? '#22c55e' : etype === 'PULLBACK' ? '#38bdf8' : etype === 'CANDLE' ? '#eab308' : '#94a3b8';
    const tradeCls = hasTrade ? (plan.instrument.includes('CALL') ? 'up' : 'down') : 'muted';
    const chartId = 'ics-chart-' + i.name.replace(/[^A-Z0-9]/gi, '');
    return `
    <tr onclick="toggleIcsExpand(${idx})" data-idx="ics-${idx}" style="cursor:pointer">
      <td>
        <div class="symbol-cell">
          <div class="symbol-icon" style="background:${symbolColor(i.name)}">${i.name.substring(0, 2)}</div>
          <div class="symbol-info">
            <span class="symbol-name">${i.name}</span>
            <span class="symbol-tag">Spot ${Number(i.spot).toLocaleString('en-IN', {maximumFractionDigits:1})}</span>
          </div>
        </div>
      </td>
      <td><span style="color:${rc};font-weight:700">${regimeTag}</span></td>
      <td>${i.regime_confidence}%</td>
      <td>${drv.adx != null ? drv.adx : '—'}</td>
      <td>${drv.rsi != null ? drv.rsi : '—'}</td>
      <td>${drv.pos_in_range_pct != null ? drv.pos_in_range_pct + '%' : '—'}</td>
      <td><span class="ics-entry-type" style="border-color:${etColor};color:${etColor}">${etype}</span></td>
      <td>${e ? e.score : '—'}</td>
      <td style="font-size:11px;color:var(--text-secondary)">${hasTrade ? `<b class="${tradeCls}">${plan.instrument}</b> · E ${Number(plan.entry).toLocaleString('en-IN', {maximumFractionDigits:1})} · SL ${Number(plan.stop).toLocaleString('en-IN', {maximumFractionDigits:1})} · TGT ${Number(plan.target).toLocaleString('en-IN', {maximumFractionDigits:1})}` : '<b class="muted">WAIT</b> — no qualifying entry'}</td>
    </tr>
    <tr class="expand-row" id="ics-expand-${idx}">
      <td colspan="9">
        <div class="expand-content">
          <div id="${chartId}" class="ics-chart"></div>
        </div>
      </td>
    </tr>`;
  }).join('');
}

let _lastIcsToggle = null;
let _lastIcsTime = 0;
function toggleIcsExpand(idx) {
  const now = Date.now();
  if (idx === _lastIcsToggle && now - _lastIcsTime < 350) return;
  _lastIcsToggle = idx;
  _lastIcsTime = now;
  const row = document.getElementById('ics-expand-' + idx);
  if (!row) return;
  document.querySelectorAll('#chartstrategy-body .expand-row').forEach(r => { if (r.id !== row.id) r.classList.remove('open'); });
  document.querySelectorAll('#chartstrategy-body tr[data-idx]').forEach(tr => tr.classList.remove('selected'));
  row.classList.toggle('open');
  if (row.classList.contains('open')) {
    document.querySelector(`#chartstrategy-body tr[data-idx="ics-${idx}"]`).classList.add('selected');
    loadIcsChart(idx);
  }
}

async function loadIcsChart(idx) {
  const filtered = window._icsFiltered || window._icsIndices || [];
  const i = filtered[idx];
  const el = document.getElementById('ics-chart-' + i.name.replace(/[^A-Z0-9]/gi, ''));
  if (!el) return;
  el.innerHTML = '<div class="sparkline-loading">Loading 15-min chart...</div>';
  try {
    const resp = await fetch(`/api/history?symbol=${encodeURIComponent(i.symbol)}&days=5&resolution=15`);
    const data = await resp.json();
    if (data.candles && data.candles.length) {
      drawChart(el, data, null, 5, 0, true);
    } else {
      el.innerHTML = '<div class="sparkline-loading">No 15-min data</div>';
    }
  } catch(e) {
    el.innerHTML = '<div class="sparkline-loading">Chart load failed</div>';
  }
}

// ==================== INDEX REGIME ====================
// ==================== BIG MONEY (unusual stock options) ====================
let bigMoneyView = false;
function toggleBigMoney() {
  bigMoneyView = !bigMoneyView;
  const bm = document.getElementById('bigmoney-section');
  const main = document.getElementById('signals-table-container');
  const idx = document.getElementById('index-section');
  const cards = document.getElementById('summary-cards');
  const btn = document.getElementById('bm-toggle');
  if (bigMoneyView) {
    bm.style.display = 'block';
    main.style.display = 'none';
    idx.style.display = 'none';
    cards.style.display = 'none';
    btn.classList.add('active');
    fetchBigMoney();
    updateFilterInfo();
  } else {
    bm.style.display = 'none';
    main.style.display = '';
    cards.style.display = '';
    idx.style.display = allIndexSignals.length ? '' : 'none';
    btn.classList.remove('active');
    updateFilterInfo();
  }
}
async function fetchBigMoney() {
  try {
    const resp = await fetch('/api/bigmoney');
    const data = await resp.json();
    const sig = JSON.stringify((data.signals || []).map(s => s.symbol + s.strike + s.score));
    if (sig !== _lastBmSig) {
      _lastBmSig = sig;
      renderBigMoney(data.signals || []);
    }
  } catch(e) {}
}

function bmNarration(s) {
  const level = s.score >= 80 ? 'Very unusual — likely a large / institutional order'
    : s.score >= 65 ? 'Unusual — notable big-player positioning'
    : 'Mildly unusual — above normal activity';
  const actNarr = {
    'fresh_buying': 'Big player OPENED new positions (OI up) and premium rose — expecting a move in this direction. Strongest setup.',
    'fresh_writing': 'Big player SOLD / wrote options (OI up, premium down) — supply pressure / collecting premium. Avoid chasing; often bearish or range-bound.',
    'short_covering': 'Shorts buying back positions (OI down, premium up) — mild bullish.',
    'long_unwinding': 'Longs selling out (OI down, premium down) — mild bearish.',
    'mixed': 'Mixed signals — no clear direction.'
  }[s.activity] || '';
  return `${level}. ${actNarr} Direction: ${s.signal_type}. Score ${s.score}/100.`;
}

function renderBigMoney(signals) {
  const tbody = document.getElementById('bigmoney-body');
  const empty = document.getElementById('bigmoney-empty');
  document.getElementById('bigmoney-count').textContent = `(${signals.length} strikes)`;
  empty.style.display = signals.length ? 'none' : 'block';

  // Group by stock — ONE ROW per share
  const groups = {};
  signals.forEach(s => {
    const g = (groups[s.symbol_name] = groups[s.symbol_name] || []);
    g.push(s);
  });

  const stockRows = Object.keys(groups).map(name => {
    const sigs = groups[name];
    const best = sigs.reduce((a, b) => (b.score > a.score ? b : a));
    // ATM strike for this stock (from signal details, else median strike)
    const atm = (best.details && best.details.atm_strike) || median(sigs.map(s => s.strike));
    // Pick up to 3 CONSECUTIVE strikes closest to ATM, plus keep any OTHERS
    const uniqueStrikes = [...new Set(sigs.map(s => s.strike))].sort((a, b) => a - b);
    const sortedByDist = uniqueStrikes
      .map(st => ({ st, d: Math.abs(st - atm) }))
      .sort((a, b) => a.d - b.d);
    const nearStrikes = sortedByDist.slice(0, 3).map(x => x.st).sort((a, b) => a - b);
    const otherStrikes = sortedByDist.slice(3).map(x => x.st).sort((a, b) => a - b);
    const shown = sigs.filter(s => nearStrikes.includes(s.strike));
    const shownOthers = sigs.filter(s => otherStrikes.includes(s.strike));

    const bull = best.signal_type === 'BULLISH';
    const bear = best.signal_type === 'BEARISH';
    const sigColor = bull ? '#22c55e' : bear ? '#ef4444' : '#facc15';
    const sigTag = bull ? '🟢 ' + best.signal_type : bear ? '🔴 ' + best.signal_type : '⚖️ ' + best.signal_type;
    const scoreCls = best.score >= 80 ? 'q-good' : best.score >= 65 ? 'q-mid' : 'q-bad';
    const single = best.mode === 'single_order' ? '⚡' : best.mode === '15min_burst' ? '⏱15m' : 'daily';
    const id = 'bm-exp-' + name.replace(/[^A-Z0-9]/gi, '');

    const strikeRowHtml = s => {
      const b2 = s.signal_type === 'BULLISH';
      const c2 = b2 ? '#22c55e' : s.signal_type === 'BEARISH' ? '#ef4444' : '#facc15';
      const oiCls2 = (s.oi_change || 0) >= 0 ? 'up' : 'down';
      const pct = s.vol_delta_ratio ? (s.vol_delta_ratio * 100).toFixed(0) + '%' : (s.oi_change_pct >= 0 ? '+' : '') + (s.oi_change_pct || 0).toFixed(1) + '%';
      return `
      <tr>
        <td class="price-cell">${Number(s.strike).toLocaleString('en-IN')}</td>
        <td><span class="strength-badge ${s.option_type === 'CE' ? 'card-buy' : 'card-sell'}">${s.option_type}</span></td>
        <td><span style="color:${c2};font-weight:700;cursor:help" title="${bmNarration(s)}">${b2 ? '🟢 ' : s.signal_type === 'BEARISH' ? '🔴 ' : '⚖️ '}${s.signal_type}</span></td>
        <td style="font-size:12px;color:var(--text-secondary);cursor:help" title="${bmNarration(s)}">${s.activity.replace(/_/g, ' ')}</td>
        <td><span class="quality-chip ${s.score >= 80 ? 'q-good' : s.score >= 65 ? 'q-mid' : 'q-bad'}" style="cursor:help" title="${bmNarration(s)}">${s.score}</span></td>
        <td class="${oiCls2}">${s.oi_change >= 0 ? '+' : ''}${Number(s.oi_change || s.vol_delta || 0).toLocaleString('en-IN')}</td>
        <td class="${oiCls2}">${pct}</td>
        <td class="${s.premium_change_pct >= 0 ? 'up' : 'down'}">${s.premium_change_pct >= 0 ? '+' : ''}${s.premium_change_pct.toFixed(1)}%</td>
      </tr>`;
    };
    const strikeRows = shown.map(strikeRowHtml).join('');
    const otherRows = shownOthers.map(strikeRowHtml).join('');
    const otherSection = otherRows ? `
      <div style="font-size:12px;font-weight:700;color:var(--accent-orange);margin:14px 0 8px">Other unusual strikes (beyond nearest 3): ${otherStrikes.map(x => Number(x).toLocaleString('en-IN')).join(' / ')}</div>
      <div class="table-container">
        <table>
          <thead><tr>
            <th>Strike</th><th>Type</th><th>Signal</th><th>Activity</th><th>Score</th><th>OI Δ</th><th>OI %</th><th>Premium %</th>
          </tr></thead>
          <tbody>${otherRows}</tbody>
        </table>
      </div>` : '';

    return `
    <tr class="bm-stock-row" data-bm="${name}" onclick="toggleBmExpand('${name}')">
      <td colspan="6">
        <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;width:100%">
          <div class="symbol-icon" style="background:#8b5cf6">${name.substring(0, 2)}</div>
          <div class="symbol-info" style="min-width:110px">
            <span class="symbol-name">${name}</span>
            <span class="symbol-tag">${sigs.length} strike(s)</span>
          </div>
          <span style="color:${sigColor};font-weight:700;cursor:help" title="${bmNarration(best)}">${sigTag}</span>
          <span style="font-size:11px;color:var(--text-muted);cursor:help" title="${bmNarration(best)}">${best.activity.replace(/_/g, ' ')}</span>
          <span class="quality-chip ${scoreCls}" style="cursor:help" title="${bmNarration(best)}">${best.score}</span>
          <span style="font-size:11px;color:var(--text-muted)">${single}</span>
          <span style="font-size:11px;color:var(--text-secondary)">ATM ${Number(atm).toLocaleString('en-IN')} · ${nearStrikes.map(x => Number(x).toLocaleString('en-IN')).join(' / ')}${otherStrikes.length ? ' +' + otherStrikes.length + ' more' : ''}</span>
        </div>
      </td>
    </tr>
    <tr class="bm-expand-row" id="${id}">
      <td colspan="6">
        <div class="expand-content">
          <div style="font-size:12px;font-weight:700;color:var(--text-secondary);margin-bottom:8px">${name} — nearest ${nearStrikes.length} strikes to ATM</div>
          <div class="table-container">
            <table>
              <thead><tr>
                <th>Strike</th><th>Type</th><th>Signal</th><th>Activity</th><th>Score</th><th>OI Δ</th><th>OI %</th><th>Premium %</th>
              </tr></thead>
              <tbody>${strikeRows}</tbody>
            </table>
          </div>
          ${otherSection}
        </div>
      </td>
    </tr>`;
  }).join('');

  tbody.innerHTML = stockRows;
}

function median(arr) {
  if (!arr.length) return 0;
  const s = [...arr].sort((a, b) => a - b);
  return s[Math.floor(s.length / 2)];
}

let _lastBmRow = null;
let _lastBmTime = 0;
function toggleBmExpand(name) {
  const now = Date.now();
  if (name === _lastBmRow && now - _lastBmTime < 350) return;
  _lastBmRow = name;
  _lastBmTime = now;
  const id = 'bm-exp-' + name.replace(/[^A-Z0-9]/gi, '');
  const row = document.getElementById(id);
  if (!row) return;
  document.querySelectorAll('.bm-expand-row').forEach(r => { if (r.id !== id) r.classList.remove('open'); });
  row.classList.toggle('open');
}

// ==================== F&O MOVERS ====================
let _moversSig = '';
let _moversModalData = null;

// ==================== BACKTEST ====================
let _btRunning = false;

async function runBacktest() {
  if (_btRunning) return;
  _btRunning = true;
  const btn = document.getElementById('bt-run-btn');
  btn.innerHTML = '⏳ Running...';
  btn.disabled = true;
  document.getElementById('bt-status').innerHTML = '🚀 Running backtest on ' + document.getElementById('bt-max-symbols').value + ' stocks (252 days)... This takes 2-5 minutes.';
  document.getElementById('bt-empty').style.display = 'none';

  const maxSym = document.getElementById('bt-max-symbols').value;
  const hold = document.getElementById('bt-hold-days').value;
  await fetch('/api/backtest?action=run&max=' + maxSym + '&hold=' + hold);

  // Poll until done
  let attempts = 0;
  const poll = setInterval(async () => {
    const resp = await fetch('/api/backtest?action=status');
    const st = await resp.json();
    attempts++;
    if (!st.running || attempts > 120) {
      clearInterval(poll);
      _btRunning = false;
      btn.innerHTML = '▶ Run Backtest';
      btn.disabled = false;
      loadBacktest();
    }
  }, 3000);
}

async function loadBacktest() {
  try {
    const resp = await fetch('/api/backtest?action=load');
    const data = await resp.json();
    if (!data.strategies || Object.keys(data.strategies).length === 0) {
      document.getElementById('bt-empty').style.display = 'block';
      document.getElementById('bt-status').innerHTML = 'No results yet. Click "Run Backtest" to start.';
      return;
    }
    document.getElementById('bt-empty').style.display = 'none';
    document.getElementById('bt-status').innerHTML = 'Generated: ' + new Date(data.generated).toLocaleString();
    renderBacktest(data.strategies);
  } catch(e) {}
}

function renderBacktest(strats) {
  const entries = Object.entries(strats)
    .filter(([_, r]) => r.total_trades > 0)
    .sort((a, b) => {
      const gradeOrder = {'A++':12,'A+':11,'A':10,'A-':9,'B+':8,'B':7,'B-':6,'C+':5,'C':4,'C-':3,'D+':2,'D':1,'F':0};
      return (gradeOrder[b[1].grade]||0) - (gradeOrder[a[1].grade]||0);
    });

  // Summary cards
  const totalTrades = entries.reduce((s,[_,r]) => s + r.total_trades, 0);
  const avgWR = entries.reduce((s,[_,r]) => s + r.win_rate, 0) / entries.length;
  const avgPF = entries.reduce((s,[_,r]) => s + r.profit_factor, 0) / entries.length;
  const profitable = entries.filter(([_,r]) => r.total_pnl_pct > 0).length;
  document.getElementById('bt-summary').innerHTML = `
    <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:16px;text-align:center"><div style="font-size:11px;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.5px">Strategies</div><div style="font-size:28px;font-weight:800;margin-top:4px">${entries.length}</div></div>
    <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:16px;text-align:center"><div style="font-size:11px;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.5px">Total Trades</div><div style="font-size:28px;font-weight:800;margin-top:4px">${totalTrades}</div></div>
    <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:16px;text-align:center"><div style="font-size:11px;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.5px">Avg Win Rate</div><div style="font-size:28px;font-weight:800;margin-top:4px;color:${avgWR>=45?'#22c55e':avgWR>=35?'#eab308':'#f97316'}">${avgWR.toFixed(1)}%</div></div>
    <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:16px;text-align:center"><div style="font-size:11px;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.5px">Avg PF</div><div style="font-size:28px;font-weight:800;margin-top:4px;color:${avgPF>=1.5?'#22c55e':avgPF>=1.0?'#eab308':'#f97316'}">${avgPF.toFixed(2)}</div></div>
    <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:16px;text-align:center"><div style="font-size:11px;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.5px">Profitable</div><div style="font-size:28px;font-weight:800;margin-top:4px;color:${profitable>entries.length/2?'#22c55e':'#f97316'}">${profitable}/${entries.length}</div></div>
  `;

  // Table rows
  const tbody = document.getElementById('bt-body');
  tbody.innerHTML = entries.map(([name, r]) => {
    const gradeColor = {'A++':'#22c55e','A+':'#22c55e','A':'#22c55e','A-':'#4ade80','B+':'#60a5fa','B':'#60a5fa','B-':'#93c5fd','C+':'#eab308','C':'#eab308','C-':'#fbbf24','D+':'#f97316','D':'#f97316','F':'#ef4444'}[r.grade] || '#94a3b8';
    const pnlColor = r.total_pnl_pct >= 0 ? '#22c55e' : '#ef4444';
    const expColor = r.expectancy >= 0 ? '#22c55e' : '#ef4444';
    const wrColor = r.win_rate >= 45 ? '#22c55e' : r.win_rate >= 35 ? '#eab308' : '#ef4444';
    return `<tr style="border-bottom:1px solid var(--border);transition:background 0.15s" onmouseover="this.style.background='var(--bg-table-hover)'" onmouseout="this.style.background=''">
      <td style="padding:12px 16px;font-weight:600;font-size:13px">${name}</td>
      <td style="padding:12px;text-align:center"><span style="display:inline-block;padding:4px 12px;border-radius:20px;font-size:12px;font-weight:800;background:${gradeColor}22;color:${gradeColor};border:1px solid ${gradeColor}44">${r.grade}</span></td>
      <td style="padding:12px;text-align:right;font-size:13px">${r.total_trades}</td>
      <td style="padding:12px;text-align:right;font-weight:700;color:${wrColor}">${r.win_rate}%</td>
      <td style="padding:12px;text-align:right;font-weight:700;color:${r.profit_factor>=1.5?'#22c55e':r.profit_factor>=1.0?'#eab308':'#ef4444'}">${r.profit_factor}</td>
      <td style="padding:12px;text-align:right;font-weight:700;color:${expColor}">${r.expectancy>=0?'+':''}${r.expectancy}%</td>
      <td style="padding:12px;text-align:right;color:${r.avg_pnl_pct>=0?'#22c55e':'#ef4444'}">${r.avg_pnl_pct>=0?'+':''}${r.avg_pnl_pct}%</td>
      <td style="padding:12px;text-align:right;font-weight:700;color:${pnlColor}">${r.total_pnl_pct>=0?'+':''}${r.total_pnl_pct}%</td>
      <td style="padding:12px;text-align:right;color:#ef4444">-${r.max_drawdown_pct}%</td>
      <td style="padding:12px;text-align:right">${r.sharpe_ratio}</td>
      <td style="padding:12px;text-align:right;color:#22c55e">+${r.avg_win_pct}%</td>
      <td style="padding:12px;text-align:right;color:#ef4444">${r.avg_loss_pct}%</td>
    </tr>`;
  }).join('');
}

async function fetchMovers() {
  try {
    const resp = await fetch('/api/movers');
    const data = await resp.json();
    const sig = JSON.stringify((data.gainers || []).map(r => r.symbol + r.ltp) + '|' + (data.losers || []).map(r => r.symbol + r.ltp));
    if (sig !== _moversSig) {
      _moversSig = sig;
      renderMovers(data);
    }
  } catch(e) {}
}

function mvFmt(n, d=2) {
  if (n === null || n === undefined || isNaN(n)) return '—';
  return Number(n).toLocaleString('en-IN', {minimumFractionDigits:d, maximumFractionDigits:d});
}
function mvFmtInt(n) {
  if (n === null || n === undefined || isNaN(n)) return '—';
  return Number(n).toLocaleString('en-IN');
}

function moversRowHTML(r, rank) {
  const up = r.change_pct >= 0;
  const cls = up ? 'up' : 'down';
  const arrow = up ? '▲' : '▼';
  const effCls = r.efficiency >= 65 ? 'up' : r.efficiency >= 45 ? 'muted' : 'down';
  const sigIsBuy = r.signal && r.signal.includes('BUY');
  const sigCls = sigIsBuy ? 'sig-buy' : 'sig-sell';
  const sig = r.signal ? `<span class="sig ${sigCls}">${r.signal}</span>` : '—';
  const sigRowCls = sigIsBuy ? 'signal-row' : r.signal ? 'signal-row sell-row' : '';
  const trk = r.open_gap_pct != null && Math.abs(r.open_gap_pct) >= 2
    ? `<span class="trk ${r.open_gap_pct >= 0 ? 'trk-buy' : 'trk-sell'}" title="Opened ${mvFmt(r.open_gap_pct)}% vs prev close — tracking mode">TRK ${mvFmt(r.open_gap_pct)}%</span>`
    : '';
  const effBar = r.efficiency >= 0 ? ` <span class="effbar" style="width:${Math.min(100, r.efficiency)}px"><i style="width:${r.efficiency}%"></i></span>` : '';
  return `<tr class="${sigRowCls}" ondblclick="openMoversAnalysis('${r.symbol}')" title="Double-click for analysis">
    <td><span class="rank">${rank}</span><span class="sym">${r.name}</span>${trk}</td>
    <td>${mvFmt(r.ltp)}</td>
    <td class="${cls}">${arrow} ${mvFmt(r.change_pct)}%</td>
    <td class="${r.change_from_open >= 0 ? 'up' : 'down'}">${mvFmt(r.change_from_open)}%</td>
    <td>${mvFmt(r.high)}</td>
    <td>${mvFmt(r.low)}</td>
    <td class="${effCls}" title="Position efficiency 0-100">${r.efficiency}${effBar}</td>
    <td>${mvFmtInt(r.volume)}</td>
    <td>${sig}</td>
  </tr>`;
}

function renderMovers(data) {
  const g = data.gainers || [], l = data.losers || [];
  const gBody = document.getElementById('movers-gain-body');
  const lBody = document.getElementById('movers-lose-body');
  gBody.innerHTML = g.length ? g.map((r,i)=>moversRowHTML(r,i+1)).join('') : '<tr><td colspan="9" class="hint">No data</td></tr>';
  lBody.innerHTML = l.length ? l.map((r,i)=>moversRowHTML(r,i+1)).join('') : '<tr><td colspan="9" class="hint">No data</td></tr>';
  const gSig = g.filter(r=>r.signal && r.signal.includes('BUY')).length;
  const lSig = l.filter(r=>r.signal && r.signal.includes('SELL')).length;
  document.getElementById('movers-gain-count').textContent = gSig ? `— ${gSig} BUY signal` : '';
  document.getElementById('movers-lose-count').textContent = lSig ? `— ${lSig} SELL signal` : '';
  document.getElementById('movers-status').textContent =
    data.updated ? `· updated ${new Date(data.updated).toLocaleTimeString('en-IN')} · ${data.total} stocks` : ' · loading…';
}

let _mvOpen = false;
function openMoversAnalysis(symbol) {
  const overlay = document.getElementById('movers-modal-overlay');
  const box = document.getElementById('movers-modal');
  overlay.style.display = 'flex';
  box.style.display = 'flex';
  _mvOpen = true;
  document.getElementById('movers-modal-title').textContent = symbol.replace('NSE:','').replace('-EQ','').replace('-INDEX','');
  document.getElementById('movers-modal-body').innerHTML = '<div class="hint">Analyzing…</div>';
  fetch('/api/movers/analysis?symbol=' + encodeURIComponent(symbol))
    .then(r => r.json())
    .then(a => renderMoversAnalysis(a))
    .catch(e => { document.getElementById('movers-modal-body').innerHTML = '<div class="hint">Failed: ' + e + '</div>'; });
}

function closeMoversModal() {
  document.getElementById('movers-modal-overlay').style.display = 'none';
  document.getElementById('movers-modal').style.display = 'none';
  _mvOpen = false;
}

function mvTag(t) {
  const up = t.startsWith('UP') || t.startsWith('BULL');
  const down = t.startsWith('DOWN') || t.startsWith('BEAR');
  return up ? '<span class="tag bull">' + t + '</span>' : down ? '<span class="tag bear">' + t + '</span>' : '<span class="tag neutral">' + t + '</span>';
}

function renderMoversAnalysis(a) {
  const body = document.getElementById('movers-modal-body');
  if (a.error) { body.innerHTML = '<div class="hint">' + a.error + '</div>'; return; }
  const score = a.strength_score;
  const scoreColor = score >= 55 ? '#22c55e' : score >= 45 ? '#eab308' : '#ef4444';
  const intra = a.intraday || {};
  body.innerHTML = `
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:14px">
      <div class="metric"><div class="k">Last Close</div><div class="v">${mvFmt(a.last_close)}</div></div>
      <div class="metric"><div class="k">Trend</div><div class="v">${mvTag(a.trend)}</div></div>
      <div class="metric"><div class="k">Strength</div><div class="v">${score}/100</div>
        <div class="score-bar"><div class="score-fill" style="width:${score}%;background:${scoreColor}"></div></div></div>
      <div class="metric"><div class="k">Label</div><div class="v">${a.strength_label}</div></div>
    </div>
    <div class="section-title">Indicators</div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:14px">
      <div class="metric"><div class="k">EMA 9/21/50</div><div class="v" style="font-size:13px">${mvFmt(a.ema9)} / ${mvFmt(a.ema21)} / ${mvFmt(a.ema50)}</div></div>
      <div class="metric"><div class="k">RSI 14</div><div class="v">${mvFmt(a.rsi14)}</div></div>
      <div class="metric"><div class="k">ATR 14</div><div class="v">${mvFmt(a.atr14)}</div></div>
      <div class="metric"><div class="k">MACD / Sig</div><div class="v" style="font-size:13px">${mvFmt(a.macd)} / ${mvFmt(a.macd_signal)}</div></div>
    </div>
    <div class="section-title">Key Levels</div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:14px">
      <div class="metric"><div class="k">Support</div><div class="v">${mvFmt(a.support)}</div></div>
      <div class="metric"><div class="k">Resistance</div><div class="v">${mvFmt(a.resistance)}</div></div>
      <div class="metric"><div class="k">52W High</div><div class="v up">${mvFmt(a.high_52w)}</div></div>
      <div class="metric"><div class="k">52W Low</div><div class="v down">${mvFmt(a.low_52w)}</div></div>
    </div>
    <div class="section-title">Intraday (15-min)</div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:14px">
      <div class="metric"><div class="k">Session VWAP</div><div class="v">${mvFmt(intra.vwap)}</div></div>
      <div class="metric"><div class="k">Price vs VWAP</div><div class="v ${intra.price_vs_vwap >= 0 ? 'up' : 'down'}">${mvFmt(intra.price_vs_vwap)}%</div></div>
      <div class="metric"><div class="k">Range Pos</div><div class="v">${mvFmt(intra.range_pos_pct,1)}%</div></div>
      <div class="metric"><div class="k">Last Candle</div><div class="v">${intra.candle || '—'}</div></div>
    </div>
    <div class="hint">Double-click another row to switch · analysis cached 5 min</div>`;
}

async function pollLive() {
  try {
    const resp = await fetch('/api/live');
    const data = await resp.json();
    livePrices = data.prices || {};
    updateLiveCells();
  } catch(e) {}
}

function updateLiveCells() {
  document.querySelectorAll('[data-price]').forEach(td => {
    const q = livePrices[td.dataset.price];
    if (!q || !q.ltp) return;
    const entry = parseFloat(td.dataset.entry) || 0;
    const up = q.ltp >= entry;
    td.innerHTML = `₹${Number(q.ltp).toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2})}`;
    td.style.color = up ? '#22c55e' : '#ef4444';
  });
  document.querySelectorAll('[data-live-price]').forEach(el => {
    const q = livePrices[el.dataset.livePrice];
    if (!q || !q.ltp) return;
    const entry = parseFloat(el.dataset.liveEntry) || 0;
    const pnl = entry ? ((q.ltp - entry) / entry * 100) : 0;
    el.textContent = `₹${Number(q.ltp).toFixed(2)} (${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}%)`;
    el.style.color = pnl >= 0 ? '#22c55e' : '#ef4444';
  });
}

// ==================== INDEX TABLE (separate column) ====================
let indexStrategyFilter = 'all';
function filterIndexSignals(strat) {
  indexStrategyFilter = strat;
  document.querySelectorAll('#index-section .filter-btn').forEach(b => b.classList.remove('active'));
  const el = document.getElementById('ifs-' + (strat === 'all' ? 'all' : strat.replace(/[^A-Za-z0-9]/g, '').toLowerCase()));
  if (el) el.classList.add('active');
  renderIndexTable();
}

function renderIndexTable() {
  const section = document.getElementById('index-section');
  const tbody = document.getElementById('index-body');
  const empty = document.getElementById('index-empty');
  const filtered = indexStrategyFilter === 'all'
    ? allIndexSignals
    : allIndexSignals.filter(s => (s.strategies || [s.strategy || '']).includes(indexStrategyFilter));
  document.getElementById('index-count').textContent = `(${filtered.length}/${allIndexSignals.length})`;

  if (filtered.length === 0) {
    section.style.display = 'block';
    empty.style.display = 'block';
    tbody.innerHTML = '';
    return;
  }
  section.style.display = 'block';
  empty.style.display = 'none';

  tbody.innerHTML = filtered.map((s, idx) => {
    const strats = s.strategies || [s.strategy || ''];
    const stratTags = strats.map(name => {
      const cls = stratTagClass(name);
      const shortName = name.replace('Range Breakout ', 'R').replace('Consolidation Breakout', 'Consolidation');
      return `<span class="strat-tag ${cls}">${shortName}</span>`;
    }).join('');
    const rr = s.price && s.stop_loss && s.target ?
      ((s.target - s.price) / Math.max(0.01, s.price - s.stop_loss)).toFixed(1) : '—';
    const badgeClass = strengthBadgeClass(s.strength || s.signal_type);
    const emoji = s.emoji || (s.signal_type === 'BUY' ? '🟢' : '🔴');
    const confPct = ((s.confidence || 0) * 100).toFixed(0);
    return `
    <tr onclick="toggleIndexExpand(${idx})" data-idx="idx-${idx}" style="cursor:pointer">
      <td>
        <div class="symbol-cell">
          <div class="symbol-icon" style="background:${symbolColor(s.symbol_name)}">${(s.symbol_name||'?').substring(0,2)}</div>
          <div class="symbol-info">
            <span class="symbol-name">${s.symbol_name}</span>
            <span class="symbol-tag">${s.symbol}</span>
          </div>
        </div>
      </td>
      <td><span class="strength-badge ${badgeClass}">${emoji} ${s.strength || s.signal_type}</span></td>
      <td><div class="strat-tags">${stratTags}</div></td>
      <td class="price-cell" data-price="${s.symbol}" data-entry="${s.price}">₹${Number(s.price).toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2})}</td>
      <td class="sl-cell">₹${Number(s.stop_loss).toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2})}</td>
      <td class="target-cell">₹${Number(s.target).toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2})}</td>
      <td class="confidence-cell">${confPct}%</td>
      <td><span class="risk-reward">1:${rr}</span></td>
    </tr>
    <tr class="expand-row" id="index-expand-${idx}">
      <td colspan="8">
        <div class="expand-content">
          <div id="index-chart-${idx}" class="index-chart-wrap"></div>
        </div>
      </td>
    </tr>`;
  }).join('');
}

// ==================== INDEX CHART (same analysis as stocks) ====================
let _lastIdxToggleRow = null;
let _lastIdxToggleTime = 0;
function toggleIndexExpand(idx) {
  const now = Date.now();
  if (idx === _lastIdxToggleRow && now - _lastIdxToggleTime < 350) return;
  _lastIdxToggleRow = idx;
  _lastIdxToggleTime = now;
  const row = document.getElementById('index-expand-' + idx);
  if (!row) return;
  document.querySelectorAll('#index-body .expand-row').forEach(r => { if (r.id !== row.id) r.classList.remove('open'); });
  document.querySelectorAll('#index-body tr[data-idx]').forEach(tr => tr.classList.remove('selected'));
  row.classList.toggle('open');
  if (row.classList.contains('open')) {
    document.querySelector(`#index-body tr[data-idx="idx-${idx}"]`).classList.add('selected');
    loadIndexChart(idx, 30);
  }
}

async function loadIndexChart(idx, days = 30) {
  const s = allIndexSignals[idx];
  if (!s) return;
  const container = document.getElementById('index-chart-' + idx);
  if (!container) return;
  container.style.position = 'relative';
  const key = 'idx_' + s.symbol + '_' + days;
  if (_chartCache[key]) {
    drawChart(container, _chartCache[key], s, days, idx, true);
    return;
  }
  try {
    const resp = await fetch(`/api/history?symbol=${encodeURIComponent(s.symbol)}&days=${days}`);
    const data = await resp.json();
    if (data.candles && data.candles.length > 0) {
      _chartCache[key] = data;
      drawChart(container, data, s, days, idx, true);
    } else {
      container.innerHTML = '<div class="sparkline-loading">No chart data available</div>';
    }
  } catch(e) {
    container.innerHTML = '<div class="sparkline-loading">Chart load failed</div>';
  }
}

// ==================== AUTO ====================
async function toggleAuto() {
  autoEnabled = !autoEnabled;
  const btn = document.getElementById('auto-btn');
  if (autoEnabled) {
    btn.textContent = '⏸ Stop';
    btn.className = 'btn btn-danger';
    await fetch('/api/auto?enable=true');
    showToast('Auto-scan enabled (every 5 min during market hours)');
  } else {
    btn.textContent = '▶ Auto';
    btn.className = 'btn btn-success';
    await fetch('/api/auto?enable=false');
    showToast('Auto-scan disabled');
  }
}

// ==================== EXPORT CSV ====================
function testTelegram() {
  const btn = document.getElementById('tg-btn');
  btn.innerHTML = '⏳';
  btn.disabled = true;
  fetch('/api/telegram?action=test')
    .then(r => r.json())
    .then(d => {
      if (d.success) {
        showToast('✅ Telegram connected! Check your chat.');
        btn.innerHTML = '📱';
      } else {
        showToast('❌ Telegram failed: ' + (d.message || 'Not configured'));
        btn.innerHTML = '📱';
      }
      btn.disabled = false;
    })
    .catch(e => {
      showToast('❌ Error: ' + e.message);
      btn.innerHTML = '📱';
      btn.disabled = false;
    });
}

function exportCSV() {
  if (filteredSignals.length === 0) { showToast('No data to export'); return; }
  const headers = ['Symbol', 'Name', 'Strength', 'Strategies', 'Price', 'Stop Loss', 'Target', 'Risk:Reward', 'Confidence', 'Timeframe', 'Reasons'];
  const rows = filteredSignals.map(s => {
    const rr = s.price && s.stop_loss && s.target ? ((s.target - s.price) / Math.max(0.01, s.price - s.stop_loss)).toFixed(1) : '';
    return [
      s.symbol, s.symbol_name, s.strength || s.signal_type,
      (s.strategies || []).join('; '),
      s.price, s.stop_loss, s.target, rr,
      ((s.confidence || 0) * 100).toFixed(0) + '%',
      s.timeframe,
      (s.reasons || [s.reason] || []).join('; ')
    ].map(v => '"' + String(v).replace(/"/g, '""') + '"').join(',');
  });
  const csv = [headers.join(','), ...rows].join('\n');
  const blob = new Blob([csv], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `scanner_signals_${new Date().toISOString().slice(0,10)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
  showToast('CSV exported!');
}

// ==================== TOAST ====================
function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.display = 'block';
  setTimeout(() => t.style.display = 'none', 3500);
}

// ==================== CLOCK ====================
function updateClock() {
  const now = new Date();
  const ist = new Date(now.getTime() + (5.5 * 60 - now.getTimezoneOffset()) * 60000);
  const h = ist.getHours(), m = ist.getMinutes(), s = ist.getSeconds();
  const pad = n => String(n).padStart(2, '0');
  const marketOpen = (h === 9 && m >= 15) || (h >= 10 && h <= 14) || (h === 15 && m <= 30);
  const status = marketOpen ? '🟢 Market Open' : '🔴 Market Closed';
  document.getElementById('market-clock').innerHTML = `IST ${pad(h)}:${pad(m)}:${pad(s)} · ${status}`;
}
setInterval(updateClock, 30000);
updateClock();

// ==================== KEYBOARD SHORTCUTS ====================
document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT') {
    if (e.key === 'Escape') { e.target.blur(); document.getElementById('search-input').value = ''; applyFilters(); }
    return;
  }
  if (e.key === 's' || e.key === 'S') { e.preventDefault(); triggerScan(); }
  if (e.key === '/' || e.key === 'f' || e.key === 'F') { e.preventDefault(); document.getElementById('search-input').focus(); }
  if (e.key === 'Escape' && selectedRow !== null) {
    document.querySelectorAll('.expand-row').forEach(r => r.classList.remove('open'));
    document.querySelectorAll('tbody tr[data-idx]').forEach(tr => tr.classList.remove('selected'));
    selectedRow = null;
  }
});

// ==================== WATCHLIST ====================
let _watchlistData = [];
let _watchlistSymbols = new Set();

async function loadWatchlist() {
  try {
    const resp = await fetch('/api/watchlist');
    const data = await resp.json();
    _watchlistData = data.watchlist || [];
    _watchlistSymbols = new Set(_watchlistData.map(w => w.symbol));
    document.getElementById('wl-count').textContent = _watchlistData.length;
    renderWatchlist();
  } catch(e) {}
}

function renderWatchlist() {
  const list = document.getElementById('wl-list');
  if (_watchlistData.length === 0) {
    list.innerHTML = '<div class="wl-empty"><div class="icon">⭐</div><p>No stocks in watchlist yet.<br>Add stocks from the scanner or type a symbol above.</p></div>';
    return;
  }
  list.innerHTML = _watchlistData.map(w => {
    const color = symbolColor(w.name || w.symbol);
    const added = w.added_at ? new Date(w.added_at).toLocaleDateString('en-IN') : '';
    return `
    <div class="wl-item">
      <div class="wl-item-icon" style="background:${color}">${(w.name||'?').substring(0,2)}</div>
      <div class="wl-item-info">
        <div class="wl-item-name">${w.name}</div>
        <div class="wl-item-symbol">${w.symbol}</div>
        ${w.notes ? `<div class="wl-item-notes">📝 ${w.notes}</div>` : ''}
      </div>
      <button class="wl-item-remove" onclick="removeFromWatchlist('${w.symbol}')" title="Remove">✕</button>
    </div>`;
  }).join('');
}

function toggleWatchlist() {
  const panel = document.getElementById('wl-panel');
  const overlay = document.getElementById('wl-overlay');
  const isOpen = panel.classList.contains('open');
  if (isOpen) {
    panel.classList.remove('open');
    overlay.classList.remove('open');
  } else {
    panel.classList.add('open');
    overlay.classList.add('open');
    loadWatchlist();
  }
}

async function addToWatchlist() {
  const symbolInput = document.getElementById('wl-add-symbol');
  const notesInput = document.getElementById('wl-add-notes');
  let symbol = symbolInput.value.trim().toUpperCase();
  const notes = notesInput.value.trim();

  if (!symbol) { showToast('Enter a stock symbol'); return; }

  // Normalize symbol format
  if (!symbol.includes(':')) {
    symbol = 'NSE:' + symbol + '-EQ';
  }

  const name = symbol.split(':').pop().replace('-EQ', '');

  try {
    const resp = await fetch('/api/watchlist/add', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ symbol, name, notes })
    });
    const data = await resp.json();
    if (data.status === 'already_exists') {
      showToast(name + ' already in watchlist');
    } else {
      showToast(name + ' added to watchlist! ⭐');
    }
    _watchlistData = data.watchlist || [];
    _watchlistSymbols = new Set(_watchlistData.map(w => w.symbol));
    document.getElementById('wl-count').textContent = _watchlistData.length;
    renderWatchlist();
    renderTable(); // Update add buttons in table
    symbolInput.value = '';
    notesInput.value = '';
  } catch(e) {
    showToast('Error adding to watchlist');
  }
}

async function removeFromWatchlist(symbol) {
  try {
    const resp = await fetch('/api/watchlist/remove', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ symbol })
    });
    const data = await resp.json();
    _watchlistData = data.watchlist || [];
    _watchlistSymbols = new Set(_watchlistData.map(w => w.symbol));
    document.getElementById('wl-count').textContent = _watchlistData.length;
    renderWatchlist();
    renderTable(); // Update add buttons in table
    showToast(symbol.split(':').pop().replace('-EQ','') + ' removed');
  } catch(e) {}
}

async function toggleStockWatchlist(symbol, name) {
  if (_watchlistSymbols.has(symbol)) {
    await removeFromWatchlist(symbol);
  } else {
    try {
      const resp = await fetch('/api/watchlist/add', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ symbol, name, notes: '' })
      });
      const data = await resp.json();
      _watchlistData = data.watchlist || [];
      _watchlistSymbols = new Set(_watchlistData.map(w => w.symbol));
      document.getElementById('wl-count').textContent = _watchlistData.length;
      renderTable();
      showToast(name + ' added to watchlist! ⭐');
    } catch(e) {}
  }
}

// Load watchlist on init
(function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem('fyers_theme'); } catch(e) {}
  if (saved && THEMES[saved]) setTheme(saved);
})();
setupTabs();
loadWatchlist();
fetchQuality();
fetchBigMoney();
fetchChartStrategy();
fetchMovers();
setInterval(fetchMovers, 30000);
pollLive();
setInterval(pollLive, 10000);

// ==================== INIT ====================
fetchSignals();
setInterval(fetchSignals, 30000);
</script>
</body>
</html>"""


def main():
    global _auto_scan_thread
    parser = argparse.ArgumentParser(description="Stock Scanner Web Dashboard v2")
    parser.add_argument("--port", type=int, default=5001, help="Port to serve on")
    args = parser.parse_args()

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", args.port))
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Dashboard v2 running at http://{host}:{port}")
    print("Press Ctrl+C to stop")
    # Start live Fyers WebSocket quotes (background thread)
    _ensure_live_quotes()
    # Start F&O Movers background refresh thread
    _start_movers_thread(120)
    # Start auto-scan loop (5-min interval during market hours)
    if _auto_scan_thread is None or not _auto_scan_thread.is_alive():
        _auto_scan_thread = threading.Thread(target=_auto_scan_loop, daemon=True)
        _auto_scan_thread.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        _movers_stop = True
        server.server_close()


if __name__ == "__main__":
    main()
