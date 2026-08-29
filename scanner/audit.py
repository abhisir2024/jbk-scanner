"""
Strategy Audit Module
=====================
Backtests all strategies on historical data to determine:
- Win rate per strategy
- Average P&L per strategy
- Profit factor per strategy
- Best/worst performing strategies
- Confidence level analysis

Usage:
    python -m scanner.audit              # audit all strategies
    python -m scanner.audit --quick      # quick audit (50 stocks)
    python -m scanner.audit --strategy "Range Breakout 9D"  # audit specific strategy
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from auth.login import get_fyers_client, load_env
from scanner.strategies import (
    RangeBreakout, EarlyBreakout, HighSupportBuy,
    ChannelConsolidationBreakout, VolumeShocker, MedChannelBreakout, WatchlistBreakout,
    BuyOnRetracement, ChannelBreakout,
    Signal, SignalType, StrategyName, _ema, _atr, _short_target, RANGE_BREAKOUT_PERIODS,
)
from scanner.candlesticks import scan_candlesticks
from scanner.engine import _trend_filter  # same EMA50 trend filter the live scanner applies
from scanner.universe import ALL_SYMBOLS, get_symbol_name


# Round-trip transaction cost estimate (STT 0.1% sell + brokerage + stamp duty + slippage)
# ~0.2% (20 bps) is a reasonable India cash-equity assumption.
COST_BPS = 20.0


class _CandlestickStrategy:
    """Wraps the candlestick adapter so it exposes .scan() like other strategies."""

    def __init__(self, auditor):
        self.auditor = auditor

    def scan(self, symbol: str, opens, highs, lows, closes, volumes, timeframe: str = "daily"):
        return self.auditor._candlestick_adapter(symbol, opens, highs, lows, closes, volumes, timeframe)


@dataclass
class StrategyResult:
    """Results for a single strategy audit."""
    strategy_name: str
    total_signals: int = 0
    buy_signals: int = 0
    sell_signals: int = 0
    winners: int = 0
    losers: int = 0
    breakeven: int = 0
    total_pnl_pct: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    max_win_pct: float = 0.0
    max_loss_pct: float = 0.0
    avg_holding_days: float = 0.0
    # Per confidence level
    high_conf_signals: int = 0
    high_conf_winners: int = 0
    med_conf_signals: int = 0
    med_conf_winners: int = 0
    low_conf_signals: int = 0
    low_conf_winners: int = 0
    winning_trades: list = field(default_factory=list)
    losing_trades: list = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        return (self.winners / self.total_signals * 100) if self.total_signals > 0 else 0

    @property
    def profit_factor(self) -> float:
        """True Profit Factor = gross profit / gross loss (from trade counts + averages)."""
        if self.avg_loss_pct == 0:
            return float('inf') if self.avg_win_pct > 0 else 0
        gross_profit = self.winners * abs(self.avg_win_pct)
        gross_loss = self.losers * abs(self.avg_loss_pct)
        if gross_loss == 0:
            return float('inf') if gross_profit > 0 else 0
        return gross_profit / gross_loss

    @property
    def expectancy(self) -> float:
        """Expected P&L per trade."""
        if self.total_signals == 0:
            return 0
        return self.total_pnl_pct / self.total_signals

    @property
    def high_conf_win_rate(self) -> float:
        return (self.high_conf_winners / self.high_conf_signals * 100) if self.high_conf_signals > 0 else 0

    @property
    def med_conf_win_rate(self) -> float:
        return (self.med_conf_winners / self.med_conf_signals * 100) if self.med_conf_signals > 0 else 0

    @property
    def low_conf_win_rate(self) -> float:
        return (self.low_conf_winners / self.low_conf_signals * 100) if self.low_conf_signals > 0 else 0


class StrategyAuditor:
    """Backtests strategies on historical data to measure performance."""

    def __init__(self, symbols: list[str] | None = None, rr_override: float | None = None,
                 sl_pct: float | None = None, tgt_pct: float | None = None, trail_pct: float | None = None,
                 trail_breakeven: bool = False, trail_activate_pct: float = 1.0, trail_buffer_pct: float = 1.0):
        load_env()
        self.fyers = get_fyers_client()
        self.symbols = symbols or ALL_SYMBOLS[:100]  # Default: first 100 stocks
        # R:R override: recompute every target so target_dist = rr_override * sl_dist.
        # Tests whether fixed asymmetric exits fix the symmetric-payoff problem.
        self.rr_override = rr_override
        # Fixed % exits (audit experiments only): override every SL/target to a
        # constant % from entry, e.g. SL 3% / target 5%.
        self.sl_pct = sl_pct
        self.tgt_pct = tgt_pct
        # Trailing stop: ratchet SL below the highest high (BUY) / above the
        # lowest low (SELL) by `trail_pct`% of price. None = no trailing.
        self.trail_pct = trail_pct
        # Breakeven trail: once price moves >= trail_activate_pct% in our favour,
        # raise SL to entry * (1 - trail_buffer_pct%) — protect the trade.
        self.trail_breakeven = trail_breakeven
        self.trail_activate_pct = trail_activate_pct
        self.trail_buffer_pct = trail_buffer_pct

        # Initialize all strategies
        self.strategies = {
            "Range Breakout 9D": RangeBreakout(lookback=9, volume_mult=1.5),
            "Range Breakout 15D": RangeBreakout(lookback=15, volume_mult=1.5),
            "Range Breakout 21D": RangeBreakout(lookback=21, volume_mult=1.5),
            "Range Breakout 60D": RangeBreakout(lookback=60, volume_mult=1.5),
            "Channel Consolidation": ChannelConsolidationBreakout(),
            "Early Breakout": EarlyBreakout(lookback=15, proximity_pct=2.0, volume_mult=1.5),
            "52W High Support": HighSupportBuy(proximity_pct=7.0, vol_max_mult=2.0),
            "Volume Shocker": VolumeShocker(volume_mult=2.0),
            "Med Channel Breakout": MedChannelBreakout(lookback=30, channel_pct=15.0, volume_mult=0.8),
            "Watchlist Range Breakout": WatchlistBreakout(apply_to_all=True),
            "Buy on Retracement": BuyOnRetracement(),
            "Trendline Channel Breakout": ChannelBreakout(),
            "Candlestick Pattern": _CandlestickStrategy(self),
        }

        self.results: dict[str, StrategyResult] = {}
        for name in self.strategies:
            self.results[name] = StrategyResult(strategy_name=name)

    def _fetch_candles(self, symbol: str, days: int = 365) -> list | None:
        """Fetch daily candles from Fyers API."""
        end = datetime.now()
        start = end - timedelta(days=days)
        data = {
            "symbol": symbol,
            "resolution": "D",
            "date_format": 1,
            "range_from": start.strftime("%Y-%m-%d"),
            "range_to": end.strftime("%Y-%m-%d"),
            "cont_flag": 1,
        }
        try:
            resp = self.fyers.history(data=data)
            if resp.get("s") == "ok" and resp.get("candles"):
                return resp["candles"]
        except Exception:
            pass
        return None

    def _candlestick_adapter(self, symbol: str, opens, highs, lows, closes, volumes, timeframe: str = "daily") -> list[Signal]:
        """Mirror the engine's candlestick -> Signal conversion so the audit can test it."""
        signals = []
        patterns = scan_candlesticks(opens, highs, lows, closes, volumes)
        for p in patterns:
            atr_vals = _atr(highs, lows, closes)
            atr = atr_vals[-1] if atr_vals[-1] else closes[-1] * 0.02
            is_bull = p.pattern_type.value == "BULLISH"
            signals.append(Signal(
                symbol=symbol,
                strategy=StrategyName.CANDLESTICK,
                signal_type=SignalType.BUY if is_bull else SignalType.SELL,
                price=p.price,
                stop_loss=round(p.price - atr * 1.5 if is_bull else p.price + atr * 1.5, 2),
                target=round(_short_target(p.price, p.price + atr * 3, is_buy=True) if is_bull else _short_target(p.price, p.price - atr * 3, is_buy=False), 2),
                confidence=p.confidence,
                reason=p.reason,
                timeframe=timeframe,
                details={"candlestick_pattern": p.pattern.value, **p.details},
            ))
        return signals

    def _extract_ohlcv(self, candles: list) -> tuple:
        """Extract OHLCV arrays from candle data."""
        opens = [c[1] for c in candles]
        highs = [c[2] for c in candles]
        lows = [c[3] for c in candles]
        closes = [c[4] for c in candles]
        volumes = [c[5] for c in candles]
        return opens, highs, lows, closes, volumes

    def _evaluate_signal(self, signal: Signal, highs: list[float], lows: list[float], closes: list[float], signal_idx: int) -> dict | None:
        """
        Evaluate a signal by checking price movement after entry.

        Rules:
        - Check if SL was hit first (loser) — uses intraday low/high
        - Check if target was hit first (winner) — uses intraday high/low
        - Apply round-trip transaction costs (COST_BPS)
        - Otherwise, exit at close after max 10 trading days
        """
        n = len(closes)
        if signal_idx + 1 >= n:
            return None

        entry_price = signal.price
        sl = signal.stop_loss
        target = signal.target
        is_buy = signal.signal_type == SignalType.BUY

        # Fixed % exit overrides (audit experiments only)
        if self.sl_pct is not None:
            sl = entry_price * (1 - self.sl_pct / 100) if is_buy else entry_price * (1 + self.sl_pct / 100)
        if self.tgt_pct is not None:
            target = entry_price * (1 + self.tgt_pct / 100) if is_buy else entry_price * (1 - self.tgt_pct / 100)

                # Apply R:R override if set: replace target with fixed target = entry +/- rr * sl_distance.
        # Tests whether asymmetric exits (instead of measured-move targets) fix the payoff problem.
        if self.rr_override is not None and self.rr_override > 0:
            if is_buy:
                target = entry_price + self.rr_override * (entry_price - sl)
            else:
                target = entry_price - self.rr_override * (sl - entry_price)

        # Check next 10 days
        max_days = min(10, n - signal_idx - 1)
        hit_sl = False
        hit_target = False
        exit_day = max_days
        exit_price = entry_price

        # Trailing state
        trail_extreme = entry_price  # highest high (BUY) / lowest low (SELL) since entry

        # Per-signal trail config (from the strategy) overrides global settings
        trail_pct = self.trail_pct
        trail_be = self.trail_breakeven
        trail_activate = self.trail_activate_pct
        trail_buffer = self.trail_buffer_pct
        det = signal.details or {}
        if det.get("trail") and isinstance(det["trail"], dict):
            t = det["trail"]
            if t.get("type") == "breakeven":
                trail_pct = None
                trail_be = True
                trail_activate = t.get("activate_pct", trail_activate)
                trail_buffer = t.get("buffer_pct", trail_buffer)

        for day in range(1, max_days + 1):
            bar_high = highs[signal_idx + day]
            bar_low = lows[signal_idx + day]

            if is_buy:
                # Update trailing extreme with today's high
                trail_extreme = max(trail_extreme, bar_high)
                cur_sl = sl
                if trail_pct is not None:
                    cur_sl = max(sl, trail_extreme * (1 - trail_pct / 100))
                elif trail_be:
                    # Once price moves >= activate% above entry, raise SL to cost (or cost - buffer)
                    if bar_high >= entry_price * (1 + trail_activate / 100):
                        cur_sl = max(sl, entry_price * (1 - trail_buffer / 100))
                # Stop loss hit intraday (low touches SL / trailing SL)
                if bar_low <= cur_sl:
                    hit_sl = True
                    exit_day = day
                    exit_price = cur_sl
                    break
                # Target hit intraday (high reaches target)
                if bar_high >= target:
                    hit_target = True
                    exit_day = day
                    exit_price = target
                    break
            else:  # SELL
                trail_extreme = min(trail_extreme, bar_low)
                cur_sl = sl
                if trail_pct is not None:
                    cur_sl = min(sl, trail_extreme * (1 + trail_pct / 100))
                elif trail_be:
                    # Once price drops >= activate% below entry, lower SL to cost (or cost + buffer)
                    if bar_low <= entry_price * (1 - trail_activate / 100):
                        cur_sl = min(sl, entry_price * (1 + trail_buffer / 100))
                # Stop loss hit intraday (high touches SL / trailing SL)
                if bar_high >= cur_sl:
                    hit_sl = True
                    exit_day = day
                    exit_price = cur_sl
                    break
                # Target hit intraday (low reaches target)
                if bar_low <= target:
                    hit_target = True
                    exit_day = day
                    exit_price = target
                    break

        # If neither SL nor target hit, use close after max_days
        if not hit_sl and not hit_target:
            exit_price = closes[signal_idx + max_days]

        # Calculate P&L net of round-trip transaction costs
        if is_buy:
            pnl_pct = ((exit_price - entry_price) / entry_price) * 100
        else:
            pnl_pct = ((entry_price - exit_price) / entry_price) * 100

        pnl_pct -= COST_BPS / 100

        return {
            "pnl_pct": round(pnl_pct, 2),
            "exit_day": exit_day,
            "hit_sl": hit_sl,
            "hit_target": hit_target,
            "entry": entry_price,
            "exit": exit_price,
            "sl": sl,
            "target": target,
        }

    def _apply_trend_filter(self, closes: list[float], signals: list[Signal]) -> list[Signal]:
        """Apply EMA50 trend filter to signals."""
        if len(closes) < 55:
            return signals

        ema50 = _ema(closes, 50)
        if not ema50[-1] or not ema50[-5]:
            return signals

        ema50_now = ema50[-1]
        ema50_5ago = ema50[-5]
        slope = (ema50_now - ema50_5ago) / ema50_5ago * 100

        filtered = []
        for sig in signals:
            if sig.signal_type == SignalType.BUY and slope < -0.05:
                if closes[-1] <= ema50_now:
                    continue  # Filter BUY in downtrend
            elif sig.signal_type == SignalType.SELL and slope > 0.05:
                if closes[-1] >= ema50_now:
                    continue  # Filter SELL in uptrend
            filtered.append(sig)

        return filtered

    def audit_symbol(self, symbol: str, candles: list) -> dict[str, list]:
        """Audit all strategies on a single symbol's historical data.
        
        Returns dict of strategy_name -> list of (signal, evaluation_result) tuples.
        """
        opens, highs, lows, closes, volumes = self._extract_ohlcv(candles)
        n = len(closes)
        if n < 60:
            return {}

        symbol_signals = {}

        for strat_name, strategy in self.strategies.items():
            signals_found = []

            # Slide through history to find past signals
            for i in range(60, n - 10):  # Stop 10 days before end (need future data)
                window_opens = opens[:i + 1]
                window_highs = highs[:i + 1]
                window_lows = lows[:i + 1]
                window_closes = closes[:i + 1]
                window_volumes = volumes[:i + 1]

                try:
                    found = strategy.scan(symbol, window_opens, window_highs,
                                        window_lows, window_closes, window_volumes, "daily")
                    # Apply the same EMA50 trend filter the live scanner uses (parity)
                    found = _trend_filter(window_closes, found)
                    for sig in found:
                        # Evaluate this signal against future prices
                        eval_result = self._evaluate_signal(sig, highs, lows, closes, i)
                        if eval_result:
                            signals_found.append((sig, eval_result))
                except Exception:
                    continue

            if signals_found:
                symbol_signals[strat_name] = signals_found

        return symbol_signals

    def run_audit(self, progress_callback=None) -> dict[str, StrategyResult]:
        """Run full audit across all symbols."""
        total = len(self.symbols)
        all_signals = {name: [] for name in self.strategies}

        print(f"\n{'='*70}")
        print(f"  STRATEGY AUDIT — {total} stocks")
        print(f"{'='*70}\n")

        for idx, symbol in enumerate(self.symbols):
            name = get_symbol_name(symbol)
            if progress_callback:
                progress_callback(idx + 1, total, name)
            else:
                print(f"  [{idx+1}/{total}] {name}...", end=" ", flush=True)

            candles = self._fetch_candles(symbol)
            if not candles or len(candles) < 60:
                if not progress_callback:
                    print("skip")
                continue

            symbol_signals = self.audit_symbol(symbol, candles)

            for strat_name, signal_pairs in symbol_signals.items():
                all_signals[strat_name].extend(signal_pairs)

            if not progress_callback:
                count = sum(len(s) for s in symbol_signals.values())
                print(f"{count} signals" if count else "no signals")

            time.sleep(0.2)  # Rate limit

        # Evaluate all signals
        print(f"\n{'='*70}")
        print(f"  EVALUATING SIGNALS...")
        print(f"{'='*70}\n")

        for strat_name, signal_pairs in all_signals.items():
            result = self.results[strat_name]
            result.total_signals = len(signal_pairs)

            wins = []
            losses = []

            for sig, eval_result in signal_pairs:
                pnl = eval_result["pnl_pct"]

                if sig.signal_type == SignalType.BUY:
                    result.buy_signals += 1
                else:
                    result.sell_signals += 1

                # Track win/loss
                if pnl > 0.1:  # Winner (>0.1%)
                    result.winners += 1
                    wins.append(pnl)
                    if sig.confidence >= 0.7:
                        result.high_conf_winners += 1
                    elif sig.confidence >= 0.5:
                        result.med_conf_winners += 1
                    else:
                        result.low_conf_winners += 1
                elif pnl < -0.1:  # Loser (<-0.1%)
                    result.losers += 1
                    losses.append(pnl)
                else:  # Breakeven
                    result.breakeven += 1

                # Confidence buckets
                if sig.confidence >= 0.7:
                    result.high_conf_signals += 1
                elif sig.confidence >= 0.5:
                    result.med_conf_signals += 1
                else:
                    result.low_conf_signals += 1

                # P&L tracking
                result.total_pnl_pct += pnl

            # Calculate averages
            if wins:
                result.avg_win_pct = sum(wins) / len(wins)
                result.max_win_pct = max(wins)
            if losses:
                result.avg_loss_pct = sum(losses) / len(losses)
                result.max_loss_pct = min(losses)

            print(f"  {strat_name}: {result.total_signals} signals ({result.winners}W / {result.losers}L / {result.breakeven}BE)")

        return self.results

    def run_quick_audit(self, num_stocks: int = 30) -> dict[str, StrategyResult]:
        """Quick audit on a subset of stocks."""
        self.symbols = ALL_SYMBOLS[:num_stocks]
        return self.run_audit()

    # ------------------------------------------------------------------
    # Confluence audit — tests the aggregator premise: do multiple
    # strategies agreeing on the same symbol+direction outperform?
    # ------------------------------------------------------------------
    def _audit_symbol_confluence(self, symbol: str, candles: list, min_strategies: int = 2) -> list:
        """Slide through history; at each bar, group signals by direction and
        count unique strategies. If >= min_strategies agree, keep the highest-
        confidence signal and evaluate it against future prices."""
        opens, highs, lows, closes, volumes = self._extract_ohlcv(candles)
        n = len(closes)
        if n < 60:
            return []

        out = []
        strategies = list(self.strategies.values())

        for i in range(60, n - 10):
            window_opens = opens[:i + 1]
            window_highs = highs[:i + 1]
            window_lows = lows[:i + 1]
            window_closes = closes[:i + 1]
            window_volumes = volumes[:i + 1]

            all_sigs = []
            for strategy in strategies:
                try:
                    found = strategy.scan(symbol, window_opens, window_highs,
                                          window_lows, window_closes, window_volumes, "daily")
                    found = _trend_filter(window_closes, found)  # parity with live scanner
                    all_sigs.extend(found)
                except Exception:
                    continue

            if not all_sigs:
                continue

            for direction in (SignalType.BUY, SignalType.SELL):
                unique: dict[str, Signal] = {}
                for s in all_sigs:
                    if s.signal_type != direction:
                        continue
                    key = s.strategy.value
                    if key not in unique or s.confidence > unique[key].confidence:
                        unique[key] = s
                if len(unique) < min_strategies:
                    continue
                best = max(unique.values(), key=lambda s: s.confidence)
                eval_result = self._evaluate_signal(best, highs, lows, closes, i)
                if eval_result:
                    out.append((best, eval_result, len(unique)))

        return out

    def run_confluence_audit(self, thresholds=(2, 3)) -> dict:
        """Evaluate signals where >= N strategies agree. Returns stats per threshold."""
        total = len(self.symbols)
        print(f"\n{'='*70}")
        print(f"  CONFLUENCE AUDIT — {total} stocks")
        print(f"{'='*70}\n")

        results = {t: {"pairs": [], "total": 0, "winners": 0, "losers": 0,
                       "be": 0, "pnl_sum": 0.0, "high_conf_wins": 0, "high_conf_total": 0}
                   for t in thresholds}

        for idx, symbol in enumerate(self.symbols):
            name = get_symbol_name(symbol)
            print(f"  [{idx+1}/{total}] {name}...", end=" ", flush=True)

            candles = self._fetch_candles(symbol)
            if not candles or len(candles) < 60:
                print("skip")
                continue

            for t in thresholds:
                pairs = self._audit_symbol_confluence(symbol, candles, min_strategies=t)
                results[t]["pairs"].extend(pairs)
            print(f"{sum(len(results[t]['pairs']) for t in thresholds)} signals")

            time.sleep(0.2)

        # Compute stats per threshold
        stats = {}
        for t, r in results.items():
            total_sigs = 0
            winners = losers = be = 0
            pnl_sum = 0.0
            hc_wins = hc_total = 0
            for _, ev, strat_count in r["pairs"]:
                total_sigs += 1
                pnl = ev["pnl_pct"]
                pnl_sum += pnl
                if pnl > 0.1:
                    winners += 1
                elif pnl < -0.1:
                    losers += 1
                else:
                    be += 1
            stats[t] = {
                "total": total_sigs, "winners": winners, "losers": losers, "be": be,
                "win_rate": (winners / total_sigs * 100) if total_sigs else 0,
                "expectancy": (pnl_sum / total_sigs) if total_sigs else 0,
            }
            r["total"] = total_sigs
            r["winners"] = winners
            r["losers"] = losers
            r["be"] = be
            r["pnl_sum"] = pnl_sum

        print(f"\n{'='*90}")
        print(f"  CONFLUENCE AUDIT REPORT (net of {COST_BPS:.0f} bps costs)")
        print(f"{'='*90}")
        print(f"  {'Min Strategies':>14} {'Signals':>8} {'Win%':>7} {'Expect':>9} {'W/L':>10}")
        print(f"  {'-'*14} {'-'*8} {'-'*7} {'-'*9} {'-'*10}")
        for t in sorted(stats):
            s = stats[t]
            label = f">= {t} strategies"
            print(f"  {label:>14} {s['total']:>8} {s['win_rate']:>6.1f}% {s['expectancy']:>+8.2f}% "
                  f"{s['winners']}/{s['losers']}")
        print(f"{'='*90}\n")
        return stats

    def save_results(self, path: str | None = None) -> str:
        """Save audit results to a JSON file for comparison. Returns the path."""
        if path is None:
            path = os.path.join(
                os.path.dirname(os.path.dirname(__file__)),
                f"audit_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
            )
        data = {}
        for name, r in self.results.items():
            data[name] = {
                "total_signals": r.total_signals,
                "buy_signals": r.buy_signals,
                "sell_signals": r.sell_signals,
                "winners": r.winners,
                "losers": r.losers,
                "breakeven": r.breakeven,
                "win_rate": round(r.win_rate, 1),
                "expectancy": round(r.expectancy, 4),
                "profit_factor": round(r.profit_factor, 3),
                "avg_win_pct": round(r.avg_win_pct, 2),
                "avg_loss_pct": round(r.avg_loss_pct, 2),
                "max_win_pct": round(r.max_win_pct, 2),
                "max_loss_pct": round(r.max_loss_pct, 2),
                "high_conf_signals": r.high_conf_signals,
                "high_conf_win_rate": round(r.high_conf_win_rate, 1),
                "med_conf_signals": r.med_conf_signals,
                "med_conf_win_rate": round(r.med_conf_win_rate, 1),
                "low_conf_signals": r.low_conf_signals,
                "low_conf_win_rate": round(r.low_conf_win_rate, 1),
            }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "generated": datetime.now().isoformat(),
                    "stocks_analyzed": len(self.symbols),
                    "cost_bps": COST_BPS,
                    "strategies": data,
                },
                f, indent=2, ensure_ascii=False,
            )
        print(f"\nResults saved to {path}")
        return path

    def print_report(self):
        """Print formatted audit report."""
        print(f"\n{'='*90}")
        print(f"  STRATEGY AUDIT REPORT (net of {COST_BPS:.0f} bps round-trip costs)")
        print(f"  Generated: {datetime.now().strftime('%d-%b-%Y %H:%M')}")
        print(f"  Stocks analyzed: {len(self.symbols)}")
        print(f"{'='*90}\n")

        # Sort by win rate
        sorted_results = sorted(
            self.results.values(),
            key=lambda r: r.win_rate,
            reverse=True
        )

        # Summary table
        print(f"  {'Strategy':<25} {'Signals':>8} {'Win%':>7} {'Avg P&L':>9} {'PF':>6} {'Expect':>8} {'Grade':>6}")
        print(f"  {'-'*25} {'-'*8} {'-'*7} {'-'*9} {'-'*6} {'-'*8} {'-'*6}")

        for r in sorted_results:
            if r.total_signals == 0:
                continue

            # Grade based on win rate and profit factor
            if r.win_rate >= 60 and r.profit_factor >= 1.5:
                grade = "A+"
            elif r.win_rate >= 55 and r.profit_factor >= 1.2:
                grade = "A"
            elif r.win_rate >= 50 and r.profit_factor >= 1.0:
                grade = "B"
            elif r.win_rate >= 45:
                grade = "C"
            else:
                grade = "D"

            pf_str = f"{r.profit_factor:.1f}" if r.profit_factor != float('inf') else "∞"

            print(f"  {r.strategy_name:<25} {r.total_signals:>8} {r.win_rate:>6.1f}% {r.expectancy:>+8.2f}% {pf_str:>6} {r.expectancy:>+7.2f}% {grade:>6}")

        # Detailed breakdown
        print(f"\n{'='*90}")
        print(f"  DETAILED BREAKDOWN BY CONFIDENCE LEVEL")
        print(f"{'='*90}\n")

        for r in sorted_results:
            if r.total_signals == 0:
                continue

            print(f"  {r.strategy_name}")
            print(f"    Total: {r.total_signals} signals ({r.buy_signals} BUY, {r.sell_signals} SELL)")
            print(f"    Overall Win Rate: {r.win_rate:.1f}% ({r.winners}W / {r.losers}L)")
            print(f"    Profit Factor: {r.profit_factor:.2f}")
            print(f"    Expectancy: {r.expectancy:+.2f}% per trade")
            print(f"    Avg Win: +{r.avg_win_pct:.2f}% | Avg Loss: {r.avg_loss_pct:.2f}%")
            print(f"    Max Win: +{r.max_win_pct:.2f}% | Max Loss: {r.max_loss_pct:.2f}%")
            print(f"    By Confidence:")
            print(f"      High (>=70%): {r.high_conf_signals} signals, {r.high_conf_win_rate:.1f}% win rate")
            print(f"      Medium (50-70%): {r.med_conf_signals} signals, {r.med_conf_win_rate:.1f}% win rate")
            print(f"      Low (<50%): {r.low_conf_signals} signals, {r.low_conf_win_rate:.1f}% win rate")
            print()

        # Recommendations
        print(f"{'='*90}")
        print(f"  RECOMMENDATIONS")
        print(f"{'='*90}\n")

        profitable = [r for r in sorted_results if r.win_rate >= 50 and r.total_signals >= 5]
        if profitable:
            print(f"  PROFITABLE STRATEGIES (use these):")
            for r in profitable[:3]:
                print(f"    [OK] {r.strategy_name} -- {r.win_rate:.1f}% win rate, {r.expectancy:+.2f}% expectancy")
        else:
            print(f"  WARNING: No strategies with >50% win rate found on this sample.")

        unprofitable = [r for r in sorted_results if r.win_rate < 45 and r.total_signals >= 5]
        if unprofitable:
            print(f"\n  AVOID THESE STRATEGIES:")
            for r in unprofitable:
                print(f"    [X] {r.strategy_name} -- {r.win_rate:.1f}% win rate, {r.expectancy:+.2f}% expectancy")

        high_conf = [r for r in sorted_results if r.high_conf_win_rate >= 60 and r.high_conf_signals >= 3]
        if high_conf:
            print(f"\n  BEST HIGH-CONFIDENCE SIGNALS:")
            for r in high_conf[:3]:
                print(f"    [*] {r.strategy_name} -- {r.high_conf_win_rate:.1f}% win rate at >=70% confidence")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Strategy Audit")
    parser.add_argument("--quick", action="store_true", help="Quick audit (30 stocks)")
    parser.add_argument("--stocks", type=int, default=50, help="Number of stocks to audit")
    parser.add_argument("--strategy", type=str, help="Audit specific strategy only")
    parser.add_argument("--output", type=str, help="Save results to JSON file")
    parser.add_argument("--confluence", action="store_true", help="Run confluence audit (>=2 and >=3 strategies agreeing)")
    parser.add_argument("--rr", type=float, default=None, help="Override all targets to fixed R:R (e.g. 2.0 = target 2x SL distance)")
    parser.add_argument("--sl-pct", type=float, default=None, help="Audit experiment: force every stop loss to fixed %% from entry (e.g. 3.0)")
    parser.add_argument("--tgt-pct", type=float, default=None, help="Audit experiment: force every target to fixed %% from entry (e.g. 5.0)")
    parser.add_argument("--trail-pct", type=float, default=None, help="Trail stops %% below highest high (BUY) / above lowest low (SELL), e.g. 3.0")
    parser.add_argument("--trail-be", action="store_true", help="Breakeven trail: move SL to cost once price moves +activate%% (default 1%%) in our favour")
    parser.add_argument("--trail-activate", type=float, default=1.0, help="Activation %% for breakeven trail (default 1.0)")
    parser.add_argument("--trail-buffer", type=float, default=1.0, help="Buffer %% below cost for breakeven trail (0 = at cost, 1 = 1%% below cost)")
    args = parser.parse_args()

    auditor = StrategyAuditor(rr_override=args.rr, sl_pct=args.sl_pct, tgt_pct=args.tgt_pct, trail_pct=args.trail_pct,
                              trail_breakeven=args.trail_be, trail_activate_pct=args.trail_activate,
                              trail_buffer_pct=args.trail_buffer)

    if args.confluence:
        auditor.run_confluence_audit()
        return

    if args.quick:
        auditor.run_quick_audit(30)
    else:
        auditor.symbols = ALL_SYMBOLS[:args.stocks]
        auditor.run_audit()

    auditor.print_report()
    if args.output:
        auditor.save_results(args.output)


if __name__ == "__main__":
    main()
