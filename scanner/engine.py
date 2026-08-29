"""
Scanner Engine — fetches data from Fyers and runs all strategies.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from auth.login import get_fyers_client, load_env
from scanner.strategies import (
    RangeBreakout, EarlyBreakout, HighSupportBuy,
    ChannelConsolidationBreakout, VolumeShocker, MedChannelBreakout, WatchlistBreakout,
    BuyOnRetracement, ChannelBreakout, IndexRangeBreakout, IndexSupportResistance,
    MomentumBreakout,
    Signal, SignalType, RANGE_BREAKOUT_PERIODS, _atr, _ema, _short_target,
)
from scanner.candlesticks import scan_candlesticks, CandlestickSignal
from scanner.rate_limiter import get_limiter


# ---------------------------------------------------------------------------
# Trend Filter — EMA50 slope-based filtering
# ---------------------------------------------------------------------------
def _trend_filter(closes: list[float], signals: list[Signal]) -> list[Signal]:
    """
    Filter signals based on EMA50 slope trend direction.

    Rules:
    - BUY signals: only keep if EMA50 is RISING (uptrend) or price > EMA50
    - SELL signals: only keep if EMA50 is FALLING (downtrend) or price < EMA50
    - EMA50 slope = (EMA50_today - EMA50_5days_ago) / EMA50_5days_ago * 100
    - Rising = slope > 0.05% ( gentle uptrend)
    - Falling = slope < -0.05% (gentle downtrend)
    - Flat = between -0.05% and +0.05% (sideways, allow both)
    """
    if len(closes) < 55:  # Need enough data for EMA50 + slope
        return signals

    ema50 = _ema(closes, 50)
    if not ema50[-1] or not ema50[-5]:
        return signals

    ema50_now = ema50[-1]
    ema50_5ago = ema50[-5]
    current_close = closes[-1]

    # EMA50 slope (5-day change)
    slope_pct = (ema50_now - ema50_5ago) / ema50_5ago * 100

    # Trend classification
    if slope_pct > 0.05:
        trend = "UPTREND"
    elif slope_pct < -0.05:
        trend = "DOWNTREND"
    else:
        trend = "SIDEWAYS"

    filtered = []
    for sig in signals:
        keep = True
        filter_reason = ""

        if sig.signal_type == SignalType.BUY:
            # BUY only in uptrend or sideways
            if trend == "DOWNTREND":
                # Exception: allow if price is above EMA50 (strong relative strength)
                if current_close > ema50_now:
                    filter_reason = f"Downtrend but price above EMA50 ({current_close:.2f} > {ema50_now:.2f}) — kept"
                else:
                    keep = False
                    filter_reason = f"Filtered: BUY in downtrend (EMA50 slope: {slope_pct:+.2f}%)"
            elif trend == "UPTREND":
                filter_reason = f"Uptrend confirmed (EMA50 slope: {slope_pct:+.2f}%)"
            else:
                filter_reason = f"Sideways (EMA50 slope: {slope_pct:+.2f}%) — allowed"

        elif sig.signal_type == SignalType.SELL:
            # SELL only in downtrend or sideways
            if trend == "UPTREND":
                # Exception: allow if price is below EMA50
                if current_close < ema50_now:
                    filter_reason = f"Uptrend but price below EMA50 ({current_close:.2f} < {ema50_now:.2f}) — kept"
                else:
                    keep = False
                    filter_reason = f"Filtered: SELL in uptrend (EMA50 slope: {slope_pct:+.2f}%)"
            elif trend == "DOWNTREND":
                filter_reason = f"Downtrend confirmed (EMA50 slope: {slope_pct:+.2f}%)"
            else:
                filter_reason = f"Sideways (EMA50 slope: {slope_pct:+.2f}%) — allowed"

        if keep:
            # Add trend info to reason and details
            sig.reason += f" | Trend: {trend} (EMA50 slope: {slope_pct:+.2f}%"
            if filter_reason and "Filtered" not in filter_reason:
                sig.reason += f", {filter_reason.split('(')[-1].split(')')[0]}"
            sig.reason += ")"
            sig.details["trend"] = trend
            sig.details["ema50_slope"] = round(slope_pct, 3)
            sig.details["ema50"] = round(ema50_now, 2)
            filtered.append(sig)
        else:
            print(f"    [TREND FILTER] {sig.strategy.value} {sig.signal_type.value} — {filter_reason}")

    return filtered
from scanner.aggregator import (
    aggregate_signals, format_aggregated_table,
    aggregated_to_dict, AggregatedSignal,
)
from scanner.universe import ALL_SYMBOLS, FNO_STOCKS, INDICES, get_symbol_name


class StockScanner:
    """Main scanner that fetches data and runs all signal strategies."""

    def __init__(self, symbols: list[str] | None = None):
        load_env()
        self.fyers = get_fyers_client()
        self.symbols = symbols or ALL_SYMBOLS
        self.strategies = [
            # Multi-period Range Breakout: 9, 15, 21, 60 days
            *[RangeBreakout(lookback=p, volume_mult=1.5) for p in RANGE_BREAKOUT_PERIODS],
            # Channel Consolidation Breakout (Bollinger squeeze)
            ChannelConsolidationBreakout(),
            # Early Breakout
            EarlyBreakout(lookback=15, proximity_pct=2.0, volume_mult=1.5),
            # 52-Week High Support Buy (UPGRADED: EMA50 slope + MACD + accumulation filters)
            HighSupportBuy(proximity_pct=5.0, vol_max_mult=3.0, rsi_min=45.0, ema50_slope_min=0.0),
            # Volume Shocker
            VolumeShocker(volume_mult=2.0),
            # Medium Term Channel Breakout (candle confirmed)
            MedChannelBreakout(lookback=30, channel_pct=15.0, volume_mult=0.8),
            # Watchlist Range Breakout — buy on fresh recent-high/range breakout,
            # ignore when price closes below the configured support level
            WatchlistBreakout(config=self._load_watchlist_rules()),
            # Buy on Retracement — buy the dip in an uptrend at EMA support
            BuyOnRetracement(),
            # Trendline Channel Breakout — ascending/descending/horizontal
            ChannelBreakout(),
            # Index Range Breakout — 15-min consolidation breakout for options
            IndexRangeBreakout(),
            # Index Support/Resistance — choppy market buy/support sell/resistance
            IndexSupportResistance(),
            # Momentum Breakout with Confirmation — trend + consolidation +
            # volume breakout + VWAP filter, suggests ATM option strike
            MomentumBreakout(),
        ]

    def _load_watchlist_rules(self) -> dict:
        """Load manual watchlist breakout/support rules from watchlist_rules.json."""
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "watchlist_rules.json")
        try:
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"  Error loading watchlist_rules.json: {e}")
        return {}

    def _fetch_candles(self, symbol: str, timeframe: str = "D", count: int = 300) -> dict | None:
        """Fetch OHLCV candles from Fyers API."""
        end_date = datetime.now()
        if timeframe == "D":
            # Request 365 days — Fyers API max is ~365 days for daily candles
            # This gives ~248 candles, enough for proper 52W high calculation
            start_date = end_date - timedelta(days=365)
        else:
            start_date = end_date - timedelta(days=count // 10 + 5)

        data = {
            "symbol": symbol,
            "resolution": timeframe,
            "date_format": 1,
            "range_from": start_date.strftime("%Y-%m-%d"),
            "range_to": end_date.strftime("%Y-%m-%d"),
            "cont_flag": 1,
        }

        limiter = get_limiter()
        response = limiter.retry_call(self.fyers.history, data=data)
        if response and response.get("s") == "ok" and response.get("candles"):
            return response
        return None

    def _extract_ohlcv(self, candles: list) -> tuple:
        """Extract opens, highs, lows, closes, volumes from candle data."""
        opens = [c[1] for c in candles]
        highs = [c[2] for c in candles]
        lows = [c[3] for c in candles]
        closes = [c[4] for c in candles]
        volumes = [c[5] for c in candles]
        return opens, highs, lows, closes, volumes

    def scan_symbol(self, symbol: str, timeframe: str = "D") -> list[Signal]:
        """Scan a single symbol with all strategies."""
        # Normalize timeframe: "daily" -> "D", "15min" -> "15", etc.
        tf = timeframe.lower()
        if tf in ("d", "daily"):
            resolution = "D"
            count = 300
        elif tf in ("15", "15min"):
            resolution = "15"
            count = 80
        else:
            resolution = "5"
            count = 80

        response = self._fetch_candles(symbol, resolution, count)
        if not response or not response.get("candles"):
            return []

        candles = response["candles"]
        if len(candles) < 20:
            return []

        opens, highs, lows, closes, volumes = self._extract_ohlcv(candles)
        signals = []

        for strategy in self.strategies:
            try:
                found = strategy.scan(symbol, opens, highs, lows, closes, volumes, timeframe)
                signals.extend(found)
            except Exception as e:
                print(f"  Strategy error on {symbol}: {e}")

        # Scan for candlestick patterns
        try:
            candle_patterns = scan_candlesticks(opens, highs, lows, closes, volumes)
            for pattern in candle_patterns:
                # Convert candlestick signal to main Signal format
                from scanner.strategies import StrategyName, SignalType
                
                atr_vals = _atr(highs, lows, closes)
                atr = atr_vals[-1] if atr_vals[-1] else closes[-1] * 0.02
                
                signals.append(Signal(
                    symbol=symbol,
                    strategy=StrategyName.CANDLESTICK,
                    signal_type=SignalType.BUY if pattern.pattern_type.value == "BULLISH" else SignalType.SELL,
                    price=pattern.price,
                    stop_loss=round(pattern.price - atr * 1.5 if pattern.pattern_type.value == "BULLISH" else pattern.price + atr * 1.5, 2),
                    target=round(_short_target(pattern.price, pattern.price + atr * 3, is_buy=True) if pattern.pattern_type.value == "BULLISH" else _short_target(pattern.price, pattern.price - atr * 3, is_buy=False), 2),
                    confidence=pattern.confidence,
                    reason=pattern.reason,
                    timeframe=timeframe,
                    details={"candlestick_pattern": pattern.pattern.value, **pattern.details},
                ))
        except Exception as e:
            pass  # Candlestick errors are non-critical

        # Apply trend filter — remove signals against the trend
        if len(closes) >= 55:
            signals = _trend_filter(closes, signals)

        # Apply confirmation filter — tag signals with volume gate / hold /
        # pullback rules (only confirmed signals are high-quality entries)
        try:
            from scanner.confirmation import ConfirmationFilter
            ConfirmationFilter().filter(signals, opens, highs, lows, closes, volumes)
        except Exception:
            pass

        return signals

    def scan_all(self, timeframe: str = "D") -> list[Signal]:
        """Scan all symbols and return all signals found."""
        all_signals: list[Signal] = []
        total = len(self.symbols)
        limiter = get_limiter()

        # Check for checkpoint to resume
        checkpoint = limiter.load_checkpoint()
        start_idx = 0
        if checkpoint and checkpoint.get("position", 0) > 0:
            start_idx = checkpoint["position"]
            print(f"  [INFO] Resuming from symbol {start_idx + 1}/{total}")

        print(f"\nScanning {total} symbols ({timeframe})...")
        print(f"  Rate limit: {limiter.max_per_minute}/min, burst: {limiter.burst_per_second}/s")
        print()

        for i, symbol in enumerate(self.symbols):
            if i < start_idx:
                continue  # Skip already-scanned symbols

            name = get_symbol_name(symbol)
            print(f"  [{i+1}/{total}] {name}...", end=" ", flush=True)

            signals = self.scan_symbol(symbol, timeframe)
            if signals:
                print(f"-> {len(signals)} signal(s)")
                all_signals.extend(signals)
            else:
                print("no signal")

            # Save checkpoint every N symbols
            if (i + 1) % limiter.checkpoint_interval == 0:
                limiter.save_checkpoint(i + 1, total, all_signals)
                stats = limiter.get_stats()
                print(f"  [INFO] Checkpoint saved ({i+1}/{total}) | {stats['calls_per_minute']} calls/min | {stats['total_retries']} retries")

        # Clear checkpoint on successful completion
        limiter.clear_checkpoint()
        stats = limiter.get_stats()
        print(f"\n[OK] Scan complete! {stats['total_calls']} API calls in {stats['elapsed_seconds']}s")
        if stats['total_retries'] > 0:
            print(f"   Retries: {stats['total_retries']} | Rate limited: {stats['total_rate_limited']} | Throttled: {stats['total_throttled']}s")

        return all_signals

    def scan_both_timeframes(self) -> dict[str, list[Signal]]:
        """Scan both daily and intraday timeframes."""
        results = {}
        results["daily"] = self.scan_all("daily")
        results["15min"] = self.scan_all("15min")
        return results

    def signals_to_dict(self, signals: list[Signal]) -> list[dict]:
        """Convert Signal objects to JSON-serializable dicts with quality scores."""
        from scanner.quality import score_signal
        result = []
        for s in signals:
            d = {
                "symbol": s.symbol,
                "symbol_name": get_symbol_name(s.symbol),
                "strategy": s.strategy.value,
                "signal_type": s.signal_type.value,
                "price": s.price,
                "stop_loss": s.stop_loss,
                "target": s.target,
                "confidence": s.confidence,
                "reason": s.reason,
                "timeframe": s.timeframe,
                "details": s.details,
            }
            # Compute quality score
            try:
                qs = score_signal(d)
                d["quality_score"] = qs.total
                d["quality_tier"] = qs.tier
                d["quality_breakdown"] = qs.breakdown
            except Exception:
                d["quality_score"] = 50
                d["quality_tier"] = "MODERATE"
                d["quality_breakdown"] = {}
            result.append(d)
        return result


def format_signals_table(signals: list[Signal]) -> str:
    """Format raw signals as a readable table."""
    if not signals:
        return "No signals found."

    lines = []
    lines.append(f"\n{'='*100}")
    lines.append(f"  SCANNER RESULTS — {len(signals)} signal(s) found")
    lines.append(f"{'='*100}")
    lines.append(
        f"  {'Symbol':<12} {'Type':<5} {'Strategy':<20} {'Price':>10} {'SL':>10} {'Target':>10} {'Conf':>6} {'TF':<8} Reason"
    )
    lines.append(f"  {'-'*96}")

    for s in signals:
        name = get_symbol_name(s.symbol)
        lines.append(
            f"  {name:<12} {s.signal_type.value:<5} {s.strategy.value:<20} "
            f"{s.price:>10.2f} {s.stop_loss:>10.2f} {s.target:>10.2f} "
            f"{s.confidence:>5.0%} {s.timeframe:<8} {s.reason[:50]}"
        )


def scan_aggregated(scanner: 'StockScanner', timeframe: str = 'D') -> list[AggregatedSignal]:
    """Run scanner and return aggregated signals with strength levels."""
    raw_signals = scanner.scan_all(timeframe)
    return aggregate_signals(raw_signals)

    lines.append(f"{'='*100}\n")
    return "\n".join(lines)
