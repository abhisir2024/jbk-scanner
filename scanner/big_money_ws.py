"""
Big Money Live WebSocket Scanner — real-time large single-order detection
==========================================================================
Streams the full F&O option chain (full-mode Fyers data socket) for tracked
stocks and detects large single-order bursts in near real time — no REST
polling of option chains (except a one-time subscription build + periodic
expiry refresh).

Expanded criteria:
  - Universe   : all 211 F&O stocks by default (--stocks 0 = all, or N)
  - Coverage   : adaptive strike selection per stock under a global socket
                 symbol budget (~5000 max) — always keeps ATM + nearest
                 strikes where institutional orders land.
  - Sensitivity: lower min-volume / OI / score thresholds (--min-vol, --min-oi,
                 --score).
  - New criteria: moneyness, minimum in-window premium move, fresh OTM
                 call/put buying bonus, premium vs prev-close move.

Per tick we keep: cumulative volume, OI, premium, prev close. Every evaluation
window we diff against the previous window to find:
  - vol_delta  : contracts traded in the window (>= threshold = big player)
  - oi_delta   : OI change in the window (positive = fresh positions)
  - premium %  : premium move in the window
  - direction  : OI x Premium matrix (fresh_buying / fresh_writing / ...)

Writes bursts to data/big_money_live.json for the dashboard (mode=single_order).

REQUIRES the small fyers_apiv3 patch in data_ws.py that keeps the OI field
in tick callbacks (applied to this machine; re-apply after SDK upgrades).

Usage:
    python -m scanner.big_money_ws                 # all 211 stocks, 60s window
    python -m scanner.big_money_ws --stocks 50     # first 50 stocks
    python -m scanner.big_money_ws --once --stocks 10   # test run, exit
"""

import os
import sys
import csv
import json
import re
import time
import threading
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth.login import _load_saved_token, load_env, FYERS_LOG_DIR
from scanner.rate_limiter import get_limiter

_MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
           "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}

# NSE F&O lot sizes are loaded at runtime from the Dhan CSV (authoritative,
# user-maintained). This table is only a fallback for symbols missing from it.
DEFAULT_LOT_SIZE = 500
LOT_CSV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "Dhan - Nse Fno Lot Size.csv")
PUNCH_HISTORY_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  "data", "big_money_punch_history.json")
LOT_SIZES = {
    "RELIANCE": 250, "TCS": 150, "INFY": 300, "HDFCBANK": 250, "ICICIBANK": 550,
    "SBIN": 300, "KOTAKBANK": 400, "AXISBANK": 600, "MARUTI": 100, "BAJFINANCE": 125,
    "BAJAJFINSV": 125, "HINDUNILVR": 200, "ASIANPAINT": 200, "ULTRACEMCO": 125,
    "TITAN": 150, "GRASIM": 250, "JSWSTEEL": 500, "TATASTEEL": 500, "TATAMOTORS": 350,
    "M&M": 500, "BAJAJ-AUTO": 200, "EICHERMOT": 200, "HEROMOTOCO": 400,
    "SUNPHARMA": 250, "DRREDDY": 125, "CIPLA": 500, "DIVISLAB": 250, "LUPIN": 500,
    "ZYDUSLIFE": 500, "BHARTIARTL": 900, "HCLTECH": 500, "TECHM": 500, "WIPRO": 1000,
    "LT": 250, "DLF": 1000, "ITC": 2000, "NTPC": 2000, "POWERGRID": 2000,
    "ONGC": 1500, "GAIL": 2000, "IOC": 1500, "BPCL": 1000, "HINDPETRO": 1500,
    "COALINDIA": 1500, "ADANIENT": 250, "ADANIPORTS": 400, "AMBUJACEM": 1000,
    "TATAPOWER": 2000, "JUBLFOOD": 250, "BHEL": 2000, "BEL": 500, "HAL": 250,
    "RVNL": 5000, "IRFC": 3000, "IREDA": 3000, "NMDC": 2000, "SAIL": 2000,
    "NHPC": 5000, "RECLTD": 1500, "PFC": 1500, "LICI": 500, "PAGEIND": 125,
    "TATAELXSI": 125, "PERSISTENT": 250, "MPHASIS": 250, "COFORGE": 250,
    "DIXON": 125, "HDFCAMC": 250, "TRENT": 200, "ABB": 125, "SIEMENS": 125,
    "BOSCHLTD": 100, "VOLTAS": 500, "HAVELLS": 250, "POLYCAB": 250, "ASTRAL": 200,
    "JINDALSTEL": 1000, "NATIONALUM": 2000, "TORNTPHARM": 200, "ALKEM": 125,
    "BIOCON": 1000, "MANKIND": 250, "CHOLAFIN": 400, "MUTHOOTFIN": 400,
    "SHRIRAMFIN": 400, "LTF": 1000, "INDUSINDBK": 400, "AUBANK": 600,
    "FEDERALBNK": 1000, "IDFCFIRSTB": 1000, "RBLBANK": 1000, "BANKBARODA": 1000,
    "CANBK": 1000, "BANKINDIA": 2000, "PNB": 1000, "YESBANK": 10000,
}


def load_lot_sizes_from_csv(path: str = LOT_CSV_PATH) -> dict:
    """Load {SYMBOL: lot_size} from the Dhan NSE FNO lot-size CSV.
    Uses the 'Lot Size' column for the next (>= current) month, falling back to
    the first such column. Returns {} if the file is missing/unreadable."""
    result: dict[str, int] = {}
    if not os.path.exists(path):
        print(f"  [lot] CSV not found: {path}")
        return result
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if not header:
                return result
            lot_cols = [i for i, h in enumerate(header) if h.strip().lower().startswith("lot size")]
            if not lot_cols:
                return result
            target = lot_cols[0]
            now = datetime.now()
            for i in lot_cols:
                m = re.search(r"\((\w+)\s+(\d{4})\)", header[i])
                if m:
                    mon = _MONTHS.get(m.group(1).upper())
                    yr = int(m.group(2))
                    if mon and (yr > now.year or (yr == now.year and mon >= now.month)):
                        target = i
                        break
            for row in reader:
                if len(row) < 4:
                    continue
                sym = row[2].strip()
                try:
                    lot = int(float(row[target].strip()))
                except (TypeError, ValueError):
                    continue
                if sym and lot > 0:
                    result[sym] = lot
    except Exception as e:
        print(f"  [lot] failed to read {path}: {e}")
    return result


def _short_name(symbol: str) -> str:
    return symbol.split(":")[-1].replace("-EQ", "").replace("-INDEX", "")


def _expiry_days_left(symbol: str) -> int | None:
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


def _parse_strike(symbol: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)(CE|PE)$", symbol)
    return float(m.group(1)) if m else None


def _expiry_base(symbol: str) -> str:
    """Symbol prefix WITHOUT the strike, e.g. NSE:RELIANCE26SEP680CE -> NSE:RELIANCE26SEP.
    (The naive split('CE')[0] keeps the strike and wrongly collapses the chain to one strike.)"""
    m = re.match(r"^(.*?)(\d+(?:\.\d+)?)(CE|PE)$", symbol)
    return m.group(1) if m else symbol.split("CE")[0].split("PE")[0]


class BigMoneyWS:
    def __init__(self, stocks: list[str] | None = None, band_pct: float = 6.0,
                 window_sec: int = 60, min_vol: int = 20000, min_oi: int = 10000,
                 min_score: float = 40.0, min_prem: float = 1.0,
                 refresh_min: int = 30, max_symbols: int = 4800,
                 min_lots: int = 200, atm_band: float = 5.0,
                 default_lot_size: int = DEFAULT_LOT_SIZE):
        load_env()
        from auth.login import get_fyers_client
        self.fyers = get_fyers_client()
        self.stocks = stocks or []
        self.band_pct = band_pct          # 0 => adaptive (budget-based), >0 => % band
        self.window_sec = window_sec
        self.min_vol = min_vol
        self.min_oi = min_oi
        self.min_score = min_score
        self.min_prem = min_prem          # minimum |premium move| % for a burst
        self.refresh_min = refresh_min
        self.max_symbols = max_symbols    # Fyers data socket hard cap is 5000
        self.min_lots = min_lots          # single-order punch: min volume in lots
        self.atm_band = atm_band          # near-ATM moneyness band (%) for punches
        self.default_lot_size = default_lot_size

        self.socket = None
        self.spot: dict[str, float] = {}          # stock symbol -> live spot
        self.lot_sizes: dict[str, int] = {}       # stock symbol -> shares per contract
        self.state: dict[str, dict] = {}          # option symbol -> {vol, oi, ltp, chp, t}
        self.baseline: dict[str, dict] = {}       # option symbol -> last evaluated {vol, oi, ltp}
        self.subscribed: set[str] = set()
        self.bursts: list[dict] = []
        self.history: list[dict] = self._load_history()   # persistent punch tracker
        self._lock = threading.Lock()
        self._stop = False

    def _load_history(self) -> list[dict]:
        """Load the persistent punch history so the tracker survives restarts."""
        try:
            with open(PUNCH_HISTORY_FILE, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _save_history(self):
        try:
            with open(PUNCH_HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(self.history, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def _record_history(self, hits: list[dict]):
        """Append new punches to the persistent tracker, deduped so the same
        (symbol, strike, type) is not re-logged within 10 minutes."""
        now = datetime.now().timestamp()
        for h in hits:
            key = (h["symbol"], h["strike"], h["option_type"])
            recent = any(self.history[i]["symbol"] == key[0]
                         and self.history[i]["strike"] == key[1]
                         and self.history[i]["option_type"] == key[2]
                         and (now - datetime.fromisoformat(self.history[i]["timestamp"]).timestamp()) < 600
                         for i in range(len(self.history) - 1, -1, -1))
            if not recent:
                self.history.insert(0, h)
        self.history = self.history[:500]
        self._save_history()

    # ------------------------------------------------------------------
    def _fetch_chain_symbols(self) -> tuple[list[str], dict]:
        """Fetch option chains for tracked stocks (REST, rate-limited) and pick
        the strikes to subscribe. Returns (option_symbols, stock_spot_map).

        Selection is adaptive: with `max_symbols` as the global budget we give
        each stock a share and take the strikes CLOSEST to ATM (CE+PE), which is
        exactly where institutional orders land. If `band_pct` > 0 we instead
        take the %-band but still cap by the per-stock budget."""
        n_stocks = max(len(self.stocks), 1)
        per_stock_symbols = max(4, self.max_symbols // n_stocks)
        strikes_per_stock = max(2, per_stock_symbols // 2)  # CE + PE per strike

        opt_syms: list[str] = []
        spot_map: dict[str, float] = {}
        limiter = get_limiter()
        for i, sym in enumerate(self.stocks, 1):
            try:
                resp = limiter.retry_call(self.fyers.optionchain, data={"symbol": sym})
                if not resp or resp.get("s") != "ok":
                    continue
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
                if spot:
                    spot_map[sym] = spot
                options = [o for o in chain if o.get("option_type") in ("CE", "PE")]
                if not options:
                    continue
                # nearest expiry only (group by expiry base, keep the closest)
                exp_groups: dict[str, list] = {}
                for o in options:
                    exp_groups.setdefault(_expiry_base(o.get("symbol", "")), []).append(o)
                best_key = min(exp_groups, key=lambda k: _expiry_days_left(k) if _expiry_days_left(k) is not None else 999)
                options = exp_groups[best_key]
                # Skip expiry day + the day before (theta decay noise)
                ed = _expiry_days_left(best_key)
                if ed is not None and ed <= 1:
                    continue

                # group by strike, sort by distance from spot
                by_strike: dict[float, dict] = {}
                for o in options:
                    st = o.get("strike_price") or 0
                    if st <= 0:
                        continue
                    if self.band_pct > 0 and spot and abs(st - spot) / spot * 100 > self.band_pct:
                        continue
                    by_strike[st] = {"symbol_ce": o["symbol"], "symbol_pe": None}
                for o in options:
                    st = o.get("strike_price") or 0
                    if st in by_strike and o.get("option_type") == "PE":
                        by_strike[st]["symbol_pe"] = o["symbol"]

                ranked = sorted(by_strike.items(), key=lambda kv: abs(kv[0] - spot) if spot else kv[0])
                picked = ranked[:strikes_per_stock]
                for st, pair in picked:
                    if pair.get("symbol_ce"):
                        opt_syms.append(pair["symbol_ce"])
                    if pair.get("symbol_pe"):
                        opt_syms.append(pair["symbol_pe"])
            except Exception as e:
                print(f"  [chain] {sym}: {e}")
        return opt_syms, spot_map

    # ------------------------------------------------------------------
    def _on_connect(self):
        """Called when the socket connects — subscribe to all symbols."""
        try:
            if not self.subscribed:
                return
            syms = list(self.subscribed)
            print(f"  [ws] connected, subscribing {len(syms)} symbols...")
            self.socket.subscribe(symbols=syms, data_type="SymbolUpdate")
            self.socket.keep_running()
        except Exception as e:
            print(f"  [ws] subscribe error: {e}")

    def _on_message(self, msg):
        if not isinstance(msg, dict) or not msg.get("symbol"):
            return
        symbol = msg["symbol"]
        if symbol in self.spot:
            # stock spot tick (NSE:XXX-EQ)
            ltp = msg.get("ltp")
            if ltp:
                self.spot[symbol] = ltp
            return
        if msg.get("type") != "sf":
            return
        vol = msg.get("vol_traded_today")
        oi = msg.get("OI")
        ltp = msg.get("ltp")
        if vol is None or oi is None or ltp is None:
            return
        with self._lock:
            self.state[symbol] = {
                "vol": int(vol), "oi": int(oi), "ltp": float(ltp),
                "chp": float(msg.get("chp") or 0.0),
                "last_qty": int(msg.get("last_traded_qty") or 0),
                "t": time.time(),
            }

    def _on_error(self, msg):
        pass

    def _on_close(self, msg):
        pass

    # ------------------------------------------------------------------
    def connect(self):
        from fyers_apiv3.FyersWebsocket import data_ws
        saved = _load_saved_token()
        if not saved:
            raise RuntimeError("No saved Fyers token — run daily_login first")
        token = f"{saved['client_id']}:{saved['access_token']}"
        # litemode=False => full tick stream with vol/OI (requires the OI patch)
        self.socket = data_ws.FyersDataSocket(
            access_token=token,
            log_path=FYERS_LOG_DIR,
            litemode=False,
            write_to_file=False,
            reconnect=True,
            on_connect=self._on_connect,
            on_close=self._on_close,
            on_error=self._on_error,
            on_message=self._on_message,
        )
        self.socket.connect()

    def _moneyness(self, option_sym: str, strike: float) -> float | None:
        """(strike - spot) / spot * 100 — negative = ITM, positive = OTM."""
        base = option_sym.split("CE")[0].split("PE")[0]
        spot = self.spot.get(base)
        if not spot or spot <= 0:
            return None
        return (strike - spot) / spot * 100

    def _is_atm(self, option_sym: str, strike: float) -> bool:
        m = self._moneyness(option_sym, strike)
        return m is not None and abs(m) <= 2.0

    # ------------------------------------------------------------------
    def evaluate(self) -> list[dict]:
        """Diff state against the last evaluation baseline and return bursts."""
        now = datetime.now().isoformat()
        with self._lock:
            state = {k: dict(v) for k, v in self.state.items()}
        hits: list[dict] = []
        for sym, cur in state.items():
            prev = self.baseline.get(sym)
            if prev is None:
                continue  # first eval = baseline
            vol_delta = cur["vol"] - prev["vol"]
            oi_delta = cur["oi"] - prev["oi"]
            if vol_delta < 0 or oi_delta < 0:
                continue  # expiry rollover / data reset
            if vol_delta == 0 and oi_delta == 0:
                continue
            ltp = cur["ltp"]
            prev_ltp = prev["ltp"]
            premium_pct = ((ltp - prev_ltp) / prev_ltp * 100) if prev_ltp > 0 else 0.0
            strike = _parse_strike(sym) or 0
            if strike <= 0:
                continue

            moneyness = self._moneyness(sym, strike)
            lot_size = self.lot_sizes.get(_short_name(sym), self.default_lot_size)
            vol_lots = vol_delta / max(lot_size, 1)
            last_qty = cur.get("last_qty", 0)
            last_qty_lots = last_qty / max(lot_size, 1)

            # A single large print (last_traded_qty >= 200 lots) is a confirmed punch.
            # Otherwise, the cumulative volume delta in the window must be >= 200 lots.
            if vol_lots < self.min_lots and last_qty_lots < self.min_lots:
                continue
            if moneyness is None or abs(moneyness) > self.atm_band:
                continue  # only report punches near the stock's strike

            confirmed_punch = last_qty_lots >= self.min_lots

            # Simple score = lots punched (capped at 100) — no complex weighting
            score = min(100.0, round(vol_lots, 1))

            # Simple direction: premium up = bullish, down = bearish, flat = neutral
            if premium_pct > 0:
                sig = "BULLISH"
            elif premium_pct < 0:
                sig = "BEARISH"
            else:
                sig = "NEUTRAL"

            hits.append({
                "symbol": sym,
                "symbol_name": _short_name(sym),
                "strike": strike,
                "option_type": "CE" if sym.endswith("CE") else "PE",
                "ltp": ltp,
                "premium_change_pct": round(premium_pct, 2),
                "moneyness_pct": round(moneyness, 2) if moneyness is not None else None,
                "oi_change": oi_delta,
                "vol_delta": vol_delta,
                "vol_lots": round(vol_lots, 0),
                "lot_size": lot_size,
                "last_qty_lots": round(last_qty_lots, 0),
                "confirmed_single_print": confirmed_punch,
                "signal_type": sig,
                "score": round(score, 1),
                "mode": "single_order",
                "timestamp": now,
                "details": {"window_sec": self.window_sec, "source": "websocket",
                            "moneyness_pct": round(moneyness, 2) if moneyness is not None else None,
                            "vol_lots": round(vol_lots, 0),
                            "confirmed_single_print": confirmed_punch},
            })

        # advance baseline
        with self._lock:
            for sym, cur in state.items():
                self.baseline[sym] = cur
        hits.sort(key=lambda h: h["score"], reverse=True)
        return hits

    @staticmethod
    def _score(vol_delta: int, oi_delta: int, premium_pct: float, is_atm: bool,
               vol_ratio: float, moneyness: float | None, chp: float) -> float:
        score = 0.0
        if vol_ratio >= 0.50: score += 40
        elif vol_ratio >= 0.30: score += 32
        elif vol_ratio >= 0.15: score += 24
        elif vol_ratio >= 0.08: score += 16
        elif vol_ratio >= 0.04: score += 8
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
        # New: near-money bonus (within ~5% of spot) even if not strictly ATM
        elif moneyness is not None and abs(moneyness) <= 5.0:
            score += 5

        # New: fresh OTM call/put buying = institutional directional bet
        if moneyness is not None and oi_delta > 0 and premium_pct > 0:
            if 0 < moneyness <= 5.0:      # OTM calls being bought
                score += 10
            elif -5.0 <= moneyness < 0:   # ITM puts being bought
                score += 10

        # New: premium strength vs prev close (confirms real money, not just size)
        if chp is not None and chp != 0:
            if abs(chp) >= 15: score += 8
            elif abs(chp) >= 5: score += 4

        return min(100.0, score)

    # ------------------------------------------------------------------
    def save(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "big_money_live.json")
        self.bursts = self.bursts[:100]
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "generated": datetime.now().isoformat(),
                "interval_min": self.window_sec / 60.0,
                "source": "websocket",
                "stocks": len(self.stocks),
                "symbols": len(self.subscribed),
                "bursts": self.bursts,
            }, f, indent=2, ensure_ascii=False)

    def print_bursts(self, bursts: list[dict]):
        if not bursts:
            print(f"  [{datetime.now().strftime('%H:%M:%S')}] no large-order punches this window")
            return
        print(f"\n  {'='*100}")
        print(f"  SINGLE ORDER PUNCHES  {datetime.now().strftime('%d-%b %H:%M:%S')}")
        print(f"  {'='*100}")
        print(f"  {'Stock':<11}{'Strike':>8}{'T':>4}{'Dir':>9}{'Lots':>7}{'SinglePrint':>12}"
              f"{'Moneyness':>10}{'Prem%':>8}")
        for b in bursts[:20]:
            mn = f"{b['moneyness_pct']:.1f}%" if b.get('moneyness_pct') is not None else "--"
            sp = "YES" if b.get("confirmed_single_print") else "no"
            print(f"  {b['symbol_name']:<11}{b['strike']:>8.0f}{b['option_type']:>4}"
                  f"{b['signal_type']:>9}{b['vol_lots']:>7.0f}{sp:>12}{mn:>10}"
                  f"{b['premium_change_pct']:>+7.1f}%")
        print(f"  {'='*100}\n")

    # ------------------------------------------------------------------
    def build_and_subscribe(self):
        """(Re)build the option symbol set and subscribe new symbols."""
        print(f"  [ws] building option universe for {len(self.stocks)} stocks "
              f"({'adaptive budget' if self.band_pct <= 0 else f'+/-{self.band_pct}% band'})...")
        opt_syms, spot_map = self._fetch_chain_symbols()
        csv_lots = load_lot_sizes_from_csv()
        with self._lock:
            self.spot.update(spot_map)
            for sym in self.stocks:
                name = _short_name(sym)
                # Dhan CSV is authoritative; static table + default are fallbacks
                self.lot_sizes[name] = csv_lots.get(name) or LOT_SIZES.get(name, self.default_lot_size)
            new_syms = [s for s in opt_syms if s not in self.subscribed]
            if new_syms:
                self.subscribed.update(new_syms)
        print(f"  [ws] {len(opt_syms)} option symbols (new: {len(new_syms)}), budget {self.max_symbols}")
        if self.socket and new_syms:
            try:
                self.socket.subscribe(symbols=new_syms, data_type="SymbolUpdate")
                self.socket.keep_running()
            except Exception as e:
                print(f"  [ws] incremental subscribe error: {e}")

    # ------------------------------------------------------------------
    def run(self, once: bool = False):
        from scanner.universe import FNO_STOCKS
        if not self.stocks:
            self.stocks = FNO_STOCKS[:30]
        print(f"\nSINGLE-ORDER PUNCH TRACKER")
        print(f"  Stocks: {len(self.stocks)} | window {self.window_sec}s | "
              f"strike band +/-{self.band_pct}% | punch >= {self.min_lots} lots within +/-{self.atm_band}% of spot")
        print(f"  Current expiry only | skip expiry day + day before | lot sizes from Dhan CSV")
        print(f"  Full-mode data socket (ltp + volume + last_traded_qty + OI) | zero chain polling after setup")
        print()

        self.build_and_subscribe()
        self.connect()

        # wait for initial snapshot ticks
        print("  [ws] waiting for initial snapshot feed...")
        time.sleep(10)

        last_refresh = time.time()
        last_eval = time.time()
        while not self._stop:
            try:
                now = time.time()
                if now - last_refresh >= self.refresh_min * 60:
                    last_refresh = now
                    self.build_and_subscribe()
                if now - last_eval >= self.window_sec:
                    last_eval = now
                    bursts = self.evaluate()
                    if bursts:
                        self.bursts = bursts + self.bursts
                        self._record_history(bursts)
                        self.print_bursts(bursts)
                        self.save()
                    if once:
                        self.save()
                        print("  [ws] --once complete")
                        break
                time.sleep(1)
            except KeyboardInterrupt:
                print("\n  Stopped.")
                break
            except Exception as e:
                print(f"  Error: {e}")
                time.sleep(10)


def main():
    parser = argparse.ArgumentParser(description="Big Money WebSocket single-order scanner")
    parser.add_argument("--stocks", type=int, default=0,
                        help="Track first N F&O stocks (0 = all 211)")
    parser.add_argument("--window", type=int, default=60, help="Evaluation window (seconds)")
    parser.add_argument("--min-vol", type=int, default=20000, help="Min volume burst per window")
    parser.add_argument("--min-oi", type=int, default=10000, help="Min OI delta per window")
    parser.add_argument("--score", type=float, default=40.0, help="Min score")
    parser.add_argument("--min-prem", type=float, default=1.0, help="Min |premium move| pct per window")
    parser.add_argument("--min-lots", type=int, default=200,
                        help="Single-order punch: min volume in lots")
    parser.add_argument("--atm-band", type=float, default=5.0,
                        help="Punch moneyness band pct around spot (near-strike only)")
    parser.add_argument("--lot-size", type=int, default=DEFAULT_LOT_SIZE,
                        help="Default shares per contract when stock not in LOT_SIZES table")
    parser.add_argument("--band", type=float, default=6.0,
                        help="Strike band pct around spot to subscribe (0 = adaptive budget-based)")
    parser.add_argument("--max-symbols", type=int, default=4800,
                        help="Global socket symbol budget (Fyers cap 5000)")
    parser.add_argument("--refresh", type=int, default=30, help="Symbol refresh interval (min)")
    parser.add_argument("--once", action="store_true", help="One evaluation then exit (test)")
    args = parser.parse_args()

    w = BigMoneyWS(stocks=None, band_pct=args.band, window_sec=args.window,
                   min_vol=args.min_vol, min_oi=args.min_oi, min_score=args.score,
                   min_prem=args.min_prem, refresh_min=args.refresh,
                   max_symbols=args.max_symbols, min_lots=args.min_lots,
                   atm_band=args.atm_band, default_lot_size=args.lot_size)
    if args.stocks == 0:
        from scanner.universe import FNO_STOCKS
        w.stocks = FNO_STOCKS
    else:
        from scanner.universe import FNO_STOCKS
        w.stocks = FNO_STOCKS[:args.stocks]
    w.run(once=args.once)


if __name__ == "__main__":
    main()
