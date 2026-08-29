"""
Trading Signal Strategies
=========================
1. Range Breakout (9/15/21/60-day) — price breaks consolidation range
2. Channel Consolidation Breakout — price breaks Bollinger/SMA channel after squeeze
3. Early Breakout — detects pre-breakout momentum with volume confirmation
4. High Support Buy — 52-week high retest + EMA support confluence
"""

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SignalType(Enum):
    BUY = "BUY"
    SELL = "SELL"


class StrategyName(Enum):
    RANGE_BREAKOUT_9D = "Range Breakout 9D"
    RANGE_BREAKOUT_15D = "Range Breakout 15D"
    RANGE_BREAKOUT_21D = "Range Breakout 21D"
    RANGE_BREAKOUT_60D = "Range Breakout 60D"
    CHANNEL_BREAKOUT = "Channel Consolidation Breakout"
    EARLY_BREAKOUT = "Early Breakout"
    HIGH_SUPPORT = "52W High Support Buy"
    VOLUME_SHOCKER = "Volume Shocker Buy"
    CANDLESTICK = "Candlestick Pattern"
    MED_CHANNEL_BREAKOUT = "Med Channel Breakout"
    WATCHLIST_BREAKOUT = "Watchlist Range Breakout"
    BUY_ON_RETRACEMENT = "Buy on Retracement"
    TRENDLINE_CHANNEL_BREAKOUT = "Trendline Channel Breakout"
    INDEX_RANGE_BREAKOUT = "Index Range Breakout"
    INDEX_SUPPORT_RESISTANCE = "Index Support/Resistance"
    MOMENTUM_BREAKOUT = "Momentum Breakout"


RANGE_BREAKOUT_PERIODS = [9, 15, 21, 60]

STRATEGY_NAME_MAP = {
    9: StrategyName.RANGE_BREAKOUT_9D,
    15: StrategyName.RANGE_BREAKOUT_15D,
    21: StrategyName.RANGE_BREAKOUT_21D,
    60: StrategyName.RANGE_BREAKOUT_60D,
}


@dataclass
class Signal:
    symbol: str
    strategy: StrategyName
    signal_type: SignalType
    price: float
    stop_loss: float
    target: float
    confidence: float  # 0.0 - 1.0
    reason: str
    timeframe: str  # "daily", "15min", "5min"
    details: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Technical indicator helpers
# ---------------------------------------------------------------------------

def _ema(data: list[float], period: int) -> list[Optional[float]]:
    """Exponential Moving Average."""
    result: list[Optional[float]] = [None] * len(data)
    if len(data) < period:
        return result
    sma = sum(data[:period]) / period
    result[period - 1] = sma
    k = 2 / (period + 1)
    prev = sma
    for i in range(period, len(data)):
        val = data[i] * k + prev * (1 - k)
        result[i] = val
        prev = val
    return result


def _sma(data: list[float], period: int) -> list[Optional[float]]:
    """Simple Moving Average."""
    result: list[Optional[float]] = [None] * len(data)
    if len(data) < period:
        return result
    window_sum = sum(data[:period])
    result[period - 1] = window_sum / period
    for i in range(period, len(data)):
        window_sum += data[i] - data[i - period]
        result[i] = window_sum / period
    return result


def _atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> list[Optional[float]]:
    """Average True Range."""
    n = len(closes)
    trs: list[float] = []
    for i in range(n):
        if i == 0:
            trs.append(highs[i] - lows[i])
        else:
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
            trs.append(tr)
    result: list[Optional[float]] = [None] * n
    if len(trs) < period:
        return result
    sma_val = sum(trs[:period]) / period
    result[period - 1] = sma_val
    for i in range(period, n):
        result[i] = (result[i - 1] * (period - 1) + trs[i]) / period
    return result


def _bollinger_bands(closes: list[float], period: int = 20, std_dev: float = 2.0) -> tuple[list[Optional[float]], list[Optional[float]], list[Optional[float]]]:
    """Bollinger Bands: (upper, middle, lower)."""
    n = len(closes)
    upper: list[Optional[float]] = [None] * n
    middle: list[Optional[float]] = [None] * n
    lower: list[Optional[float]] = [None] * n

    for i in range(period - 1, n):
        window = closes[i - period + 1: i + 1]
        sma = sum(window) / period
        variance = sum((x - sma) ** 2 for x in window) / period
        std = math.sqrt(variance)
        middle[i] = sma
        upper[i] = sma + std_dev * std
        lower[i] = sma - std_dev * std

    return upper, middle, lower


def _rsi(closes: list[float], period: int = 14) -> list[Optional[float]]:
    """Relative Strength Index."""
    n = len(closes)
    result: list[Optional[float]] = [None] * n
    if n < period + 1:
        return result

    gains = []
    losses = []
    for i in range(1, n):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    if avg_loss == 0:
        result[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        result[period] = 100 - (100 / (1 + rs))

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            result[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[i + 1] = 100 - (100 / (1 + rs))

    return result


def _vwap(highs: list[float], lows: list[float], closes: list[float], volumes: list[float]) -> list[Optional[float]]:
    """Volume Weighted Average Price — cumulative typical-price * volume / cumulative volume."""
    n = len(closes)
    result: list[Optional[float]] = [None] * n
    cum_pv = 0.0
    cum_vol = 0.0
    for i in range(n):
        typical = (highs[i] + lows[i] + closes[i]) / 3
        cum_pv += typical * volumes[i]
        cum_vol += volumes[i]
        if cum_vol > 0:
            result[i] = cum_pv / cum_vol
    return result


def _bb_width(upper: list, middle: list, lower: list) -> list[Optional[float]]:
    """Bollinger Band Width = (Upper - Lower) / Middle."""
    n = len(upper)
    result: list[Optional[float]] = [None] * n
    for i in range(n):
        if upper[i] and lower[i] and middle[i] and middle[i] > 0:
            result[i] = (upper[i] - lower[i]) / middle[i]
    return result


# ---------------------------------------------------------------------------
# Short-term target cap (options-friendly)
# ---------------------------------------------------------------------------
# Measured-move / 3x-ATR targets can be 6-15% — too far for short-term option
# trades in a choppy, range-bound market. Cap every displayed target at this
# % from entry so signals show achievable price levels.
DEFAULT_SHORT_TARGET_PCT = 3.5


def _short_target(entry: float, proposed: float, is_buy: bool = True, pct: float | None = None) -> float:
    """Cap a proposed target at +/- `pct`% from entry (default 3.5%).
    Returns the tighter of the two, so tight-range signals keep natural targets."""
    pct = DEFAULT_SHORT_TARGET_PCT if pct is None else pct
    if is_buy:
        return min(proposed, entry * (1 + pct / 100.0))
    return max(proposed, entry * (1 - pct / 100.0))


# ---------------------------------------------------------------------------
# Strategy 1: Range Breakout (multi-period: 9, 15, 21, 60 days)
# ---------------------------------------------------------------------------
class RangeBreakout:
    """
    Detects when price breaks out of a consolidation range.
    Runs for a specific lookback period. Instantiate multiple times for 9/15/21/60.
    - Computes the highest high and lowest low over the lookback window
    - BUY when close breaks above the range high with volume confirmation
    - SELL when close breaks below the range low
    """

    def __init__(self, lookback: int = 15, volume_mult: float = 1.5, volume_trend_days: int = 3, target_pct: float | None = None):
        self.lookback = lookback
        self.volume_mult = volume_mult
        self.volume_trend_days = volume_trend_days
        self.target_pct = target_pct

    @property
    def strategy_name(self) -> StrategyName:
        return STRATEGY_NAME_MAP.get(self.lookback, StrategyName.RANGE_BREAKOUT_15D)

    def scan(
        self,
        symbol: str,
        opens: list[float],
        highs: list[float],
        lows: list[float],
        closes: list[float],
        volumes: list[float],
        timeframe: str = "daily",
    ) -> list[Signal]:
        signals: list[Signal] = []
        n = len(closes)
        if n < self.lookback + 2:
            return signals

        current_close = closes[-1]
        current_volume = volumes[-1]
        avg_volume = sum(volumes[-self.lookback - 1:-1]) / self.lookback

        # Range boundaries from lookback period
        range_highs = highs[-(self.lookback + 1):-1]
        range_lows = lows[-(self.lookback + 1):-1]
        range_high = max(range_highs)
        range_low = min(range_lows)
        range_width = range_high - range_low
        if range_high == 0:
            return signals

        range_pct = (range_width / range_high) * 100

        # Max range width depends on period: shorter periods = tighter ranges
        # Increased limits for volatile stocks like MCX
        max_range = 25 if self.lookback <= 15 else 20 if self.lookback <= 21 else 15
        if range_pct > max_range:
            return signals

        atr_vals = _atr(highs, lows, closes)
        atr = atr_vals[-1] if atr_vals[-1] else range_width * 0.5

        volume_confirmed = current_volume >= avg_volume * self.volume_mult
        vol_ratio = current_volume / max(avg_volume, 1)

        # Volume trend filter: last N days volume must be increasing (confirmation of conviction)
        volume_trend_ok = True
        if self.volume_trend_days > 1 and n > self.volume_trend_days:
            vol_window = volumes[-self.volume_trend_days:]
            volume_trend_ok = all(vol_window[i] > vol_window[i - 1] for i in range(1, len(vol_window)))

        # Bullish breakout (lower volume threshold: 0.5x for high-vol stocks)
        if current_close > range_high and vol_ratio >= 0.5 and volume_trend_ok:
            confidence = min(1.0, 0.5 + (range_pct < 5) * 0.2 + (vol_ratio - self.volume_mult) * 0.1)
            # Wider stop loss (was 0.5 ATR) — avoids stop-outs before move completes
            sl = range_high - atr * 1.5
            target = _short_target(current_close, current_close + range_width, is_buy=True, pct=self.target_pct)  # short-term capped measured move
            signals.append(Signal(
                symbol=symbol,
                strategy=StrategyName.TRENDLINE_CHANNEL_BREAKOUT,
                signal_type=SignalType.BUY,
                price=current_close,
                stop_loss=round(sl, 2),
                target=round(target, 2),
                confidence=round(confidence, 2),
                reason=f"{self.lookback}-day range breakout above {range_high:.2f} with {vol_ratio:.1f}x volume, {self.volume_trend_days}-day rising volume | Range: {range_low:.2f}-{range_high:.2f} ({range_pct:.1f}%)",
                timeframe=timeframe,
                details={
                    "lookback": self.lookback,
                    "range_high": range_high,
                    "range_low": range_low,
                    "range_pct": round(range_pct, 2),
                    "volume_ratio": round(vol_ratio, 2),
                    "volume_trend_days": self.volume_trend_days,
                },
            ))

        # Bearish breakout (lower volume threshold: 0.5x)
        if current_close < range_low and vol_ratio >= 0.5 and volume_trend_ok:
            confidence = min(1.0, 0.5 + (range_pct < 5) * 0.2 + (vol_ratio - self.volume_mult) * 0.1)
            sl = range_low + atr * 1.5
            target = _short_target(current_close, current_close - range_width, is_buy=False, pct=self.target_pct)
            signals.append(Signal(
                symbol=symbol,
                strategy=StrategyName.TRENDLINE_CHANNEL_BREAKOUT,
                signal_type=SignalType.SELL,
                price=current_close,
                stop_loss=round(sl, 2),
                target=round(target, 2),
                confidence=round(confidence, 2),
                reason=f"{self.lookback}-day range breakdown below {range_low:.2f} with {vol_ratio:.1f}x volume, {self.volume_trend_days}-day rising volume | Range: {range_low:.2f}-{range_high:.2f} ({range_pct:.1f}%)",
                timeframe=timeframe,
                details={
                    "lookback": self.lookback,
                    "range_high": range_high,
                    "range_low": range_low,
                    "range_pct": round(range_pct, 2),
                    "volume_ratio": round(vol_ratio, 2),
                    "volume_trend_days": self.volume_trend_days,
                },
            ))

        return signals


# ---------------------------------------------------------------------------
# Strategy 2: Channel Consolidation Breakout
# ---------------------------------------------------------------------------
class ChannelConsolidationBreakout:
    """
    Detects breakout from a Bollinger Band squeeze / tight channel consolidation.

    Logic:
    - Bollinger Band Width narrows below a threshold (squeeze = consolidation)
    - Price was oscillating inside a tight SMA channel (20 SMA +/- 2-3%)
    - Volume expansion confirms breakout
    - RSI confirms momentum direction

    Squeeze detection:
    - BB Width < 25th percentile of last 100 bars = squeeze active
    - At least 5 of last 10 bars closed inside the tight channel
    - Breakout: close above/below upper/lower Bollinger Band
    """

    def __init__(
        self,
        bb_period: int = 20,
        bb_std: float = 2.0,
        squeeze_threshold: float = 0.06,
        channel_pct: float = 2.5,
        volume_mult: float = 1.5,
        rsi_period: int = 14,
        target_pct: float | None = None,
    ):
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.squeeze_threshold = squeeze_threshold
        self.channel_pct = channel_pct
        self.volume_mult = volume_mult
        self.rsi_period = rsi_period
        self.target_pct = target_pct

    def scan(
        self,
        symbol: str,
        opens: list[float],
        highs: list[float],
        lows: list[float],
        closes: list[float],
        volumes: list[float],
        timeframe: str = "daily",
    ) -> list[Signal]:
        signals: list[Signal] = []
        n = len(closes)
        if n < 50:  # Need enough data for BB + RSI
            return signals

        # Compute indicators
        upper_bb, mid_bb, lower_bb = _bollinger_bands(closes, self.bb_period, self.bb_std)
        bb_widths = _bb_width(upper_bb, mid_bb, lower_bb)
        rsi_vals = _rsi(closes, self.rsi_period)

        current_close = closes[-1]
        current_volume = volumes[-1]
        avg_volume = sum(volumes[-20:]) / min(20, n)
        vol_ratio = current_volume / max(avg_volume, 1)

        # Current indicator values
        cur_upper = upper_bb[-1]
        cur_mid = mid_bb[-1]
        cur_lower = lower_bb[-1]
        cur_width = bb_widths[-1]
        cur_rsi = rsi_vals[-1]

        if not all([cur_upper, cur_mid, cur_lower, cur_width, cur_rsi]):
            return signals

        # --- Squeeze detection: was BB width narrow recently? ---
        valid_widths = [w for w in bb_widths[-100:] if w is not None]
        if not valid_widths:
            return signals

        sorted_widths = sorted(valid_widths)
        pct_25 = sorted_widths[len(sorted_widths) // 4] if len(sorted_widths) >= 4 else sorted_widths[0]

        # Check if any of last 10 bars had squeeze-level BB width
        recent_widths = [w for w in bb_widths[-10:] if w is not None]
        was_squeezed = any(w <= pct_25 * 1.1 for w in recent_widths)

        if not was_squeezed:
            return signals

        # --- Channel consolidation check: price oscillated around mid BB ---
        tight_channel_count = 0
        for i in range(-10, 0):
            if i + n < 0:
                continue
            bar_close = closes[i]
            bar_mid = mid_bb[i]
            if bar_mid and bar_mid > 0:
                deviation = abs(bar_close - bar_mid) / bar_mid * 100
                if deviation <= self.channel_pct:
                    tight_channel_count += 1

        if tight_channel_count < 5:
            return signals

        atr_vals = _atr(highs, lows, closes)
        atr = atr_vals[-1] if atr_vals[-1] else (cur_upper - cur_lower) * 0.3

        # --- Breakout signals (lower volume threshold: 1.0x) ---
        # Bullish: close above upper BB, RSI > 50, volume expansion
        if current_close > cur_upper and cur_rsi > 50 and vol_ratio >= 1.0:
            # Extra confidence from squeeze duration
            squeeze_bars = sum(1 for w in recent_widths if w <= pct_25 * 1.1)
            confidence = min(1.0, 0.5 + squeeze_bars * 0.05 + (vol_ratio - self.volume_mult) * 0.05 + (cur_rsi - 50) / 200)
            sl = cur_mid  # Stop at middle band
            # Target: channel width projected from breakout (capped short-term)
            channel_width = cur_upper - cur_lower
            target = _short_target(current_close, current_close + channel_width, is_buy=True, pct=self.target_pct)

            signals.append(Signal(
                symbol=symbol,
                strategy=StrategyName.CHANNEL_BREAKOUT,
                signal_type=SignalType.BUY,
                price=current_close,
                stop_loss=round(sl, 2),
                target=round(target, 2),
                confidence=round(confidence, 2),
                reason=f"Channel squeeze breakout above BB upper ({cur_upper:.2f}) | BB Width was {pct_25:.4f}, now expanding | RSI: {cur_rsi:.1f} | {vol_ratio:.1f}x volume | Squeezed {squeeze_bars}/10 bars",
                timeframe=timeframe,
                details={
                    "bb_upper": round(cur_upper, 2),
                    "bb_mid": round(cur_mid, 2),
                    "bb_lower": round(cur_lower, 2),
                    "bb_width": round(cur_width, 4),
                    "squeeze_pct25": round(pct_25, 4),
                    "squeeze_bars": squeeze_bars,
                    "channel_bars": tight_channel_count,
                    "rsi": round(cur_rsi, 2),
                    "volume_ratio": round(vol_ratio, 2),
                },
            ))

        # Bearish: close below lower BB, RSI < 50, volume expansion
        if current_close < cur_lower and cur_rsi < 50 and vol_ratio >= 1.0:
            squeeze_bars = sum(1 for w in recent_widths if w <= pct_25 * 1.1)
            confidence = min(1.0, 0.5 + squeeze_bars * 0.05 + (self.volume_mult - vol_ratio) * 0.05 + (50 - cur_rsi) / 200)
            sl = cur_mid
            channel_width = cur_upper - cur_lower
            target = _short_target(current_close, current_close - channel_width, is_buy=False, pct=self.target_pct)

            signals.append(Signal(
                symbol=symbol,
                strategy=StrategyName.CHANNEL_BREAKOUT,
                signal_type=SignalType.SELL,
                price=current_close,
                stop_loss=round(sl, 2),
                target=round(target, 2),
                confidence=round(confidence, 2),
                reason=f"Channel squeeze breakdown below BB lower ({cur_lower:.2f}) | BB Width was {pct_25:.4f}, now expanding | RSI: {cur_rsi:.1f} | {vol_ratio:.1f}x volume | Squeezed {squeeze_bars}/10 bars",
                timeframe=timeframe,
                details={
                    "bb_upper": round(cur_upper, 2),
                    "bb_mid": round(cur_mid, 2),
                    "bb_lower": round(cur_lower, 2),
                    "bb_width": round(cur_width, 4),
                    "squeeze_pct25": round(pct_25, 4),
                    "squeeze_bars": squeeze_bars,
                    "channel_bars": tight_channel_count,
                    "rsi": round(cur_rsi, 2),
                    "volume_ratio": round(vol_ratio, 2),
                },
            ))

        return signals


# ---------------------------------------------------------------------------
# Strategy 3: Early Breakout Detection
# ---------------------------------------------------------------------------
class EarlyBreakout:
    """
    Detects pre-breakout momentum — price approaching range highs with
    increasing volume, before the actual breakout happens.
    - Price within 2% of range high
    - Volume increasing above average
    - Consecutive higher closes
    """

    def __init__(self, lookback: int = 15, proximity_pct: float = 2.0, volume_mult: float = 1.5, target_pct: float | None = None):
        self.lookback = lookback
        self.proximity_pct = proximity_pct
        self.volume_mult = volume_mult
        self.target_pct = target_pct

    def scan(
        self,
        symbol: str,
        opens: list[float],
        highs: list[float],
        lows: list[float],
        closes: list[float],
        volumes: list[float],
        timeframe: str = "daily",
    ) -> list[Signal]:
        signals: list[Signal] = []
        n = len(closes)
        if n < self.lookback + 3:
            return signals

        current_close = closes[-1]
        current_volume = volumes[-1]
        avg_volume = sum(volumes[-self.lookback - 1:-1]) / self.lookback
        vol_ratio = current_volume / max(avg_volume, 1)

        # Compute range
        range_highs = highs[-(self.lookback + 1):-1]
        range_lows = lows[-(self.lookback + 1):-1]
        range_high = max(range_highs)
        range_low = min(range_lows)
        if range_high == 0:
            return signals

        proximity = ((range_high - current_close) / range_high) * 100

        near_high = 0 < proximity <= self.proximity_pct
        volume_rising = vol_ratio > 0.5  # Very lenient for pre-breakout

        # Consecutive higher closes
        higher_closes = 0
        for i in range(n - 1, max(n - 4, 0), -1):
            if i > 0 and closes[i] > closes[i - 1]:
                higher_closes += 1
            else:
                break

        atr_vals = _atr(highs, lows, closes)
        atr = atr_vals[-1] if atr_vals[-1] else (range_high - range_low) * 0.5

        # Early Breakout: Low volume OK for pre-breakout signals
        if near_high and higher_closes >= 2:
            confidence = min(1.0, 0.4 + higher_closes * 0.15 + (vol_ratio - self.volume_mult) * 0.1)
            signals.append(Signal(
                symbol=symbol,
                strategy=StrategyName.EARLY_BREAKOUT,
                signal_type=SignalType.BUY,
                price=current_close,
                stop_loss=round(range_high - atr, 2),
                target=round(_short_target(current_close, range_high + (range_high - range_low), is_buy=True, pct=self.target_pct), 2),
                confidence=round(confidence, 2),
                reason=f"{self.lookback}-day range | Near high ({range_high:.2f}), {higher_closes} up closes, {vol_ratio:.1f}x volume | Range: {range_low:.2f}-{range_high:.2f}",
                timeframe=timeframe,
                details={
                    "lookback": self.lookback,
                    "range_high": range_high,
                    "range_low": range_low,
                    "proximity_pct": round(proximity, 2),
                    "volume_ratio": round(vol_ratio, 2),
                    "higher_closes": higher_closes,
                },
            ))

        # Early bearish
        proximity_low = ((current_close - range_low) / range_high) * 100
        near_low = 0 < proximity_low <= self.proximity_pct
        lower_closes = 0
        for i in range(n - 1, max(n - 4, 0), -1):
            if i > 0 and closes[i] < closes[i - 1]:
                lower_closes += 1
            else:
                break

        if near_low and volume_rising and lower_closes >= 2:
            confidence = min(1.0, 0.4 + lower_closes * 0.15 + (vol_ratio - self.volume_mult) * 0.1)
            signals.append(Signal(
                symbol=symbol,
                strategy=StrategyName.EARLY_BREAKOUT,
                signal_type=SignalType.SELL,
                price=current_close,
                stop_loss=round(range_low + atr, 2),
                target=round(_short_target(current_close, range_low - (range_high - range_low), is_buy=False, pct=self.target_pct), 2),
                confidence=round(confidence, 2),
                reason=f"{self.lookback}-day range | Near low ({range_low:.2f}), {lower_closes} down closes, {vol_ratio:.1f}x volume | Range: {range_low:.2f}-{range_high:.2f}",
                timeframe=timeframe,
                details={
                    "lookback": self.lookback,
                    "range_high": range_high,
                    "range_low": range_low,
                    "proximity_pct": round(proximity_low, 2),
                    "volume_ratio": round(vol_ratio, 2),
                    "lower_closes": lower_closes,
                },
            ))

        return signals


# ---------------------------------------------------------------------------
# Strategy 4: 52-Week High Support Buy
# ---------------------------------------------------------------------------
class HighSupportBuy:
    """
    UPGRADED: High-probability 52-week high retest strategy.

    Buys when a strong stock near its 52-week high pulls back to support,
    but ONLY when multiple confirmations align:

    1. TREND: EMA50 is rising (slope > 0) AND price > EMA50
    2. MOMENTUM: RSI > 45 (building, not oversold)
    3. MACD: Histogram positive (bullish momentum)
    4. PATTERN: Price at EMA20/EMA50/breakout retest support
    5. VOLUME: Current vol > 0.8x avg AND 3-day volume trend rising
    6. STRUCTURE: At least 2 higher lows in last 10 days (accumulation)
    7. PROXIMITY: Within 5% of 52W high (closer = stronger)

    Filters that KILL signals:
    - EMA50 falling (downtrend)
    - RSI < 45 (weak momentum)
    - MACD histogram negative (bearish)
    - Price below EMA50 (no trend support)
    - Excessive volume > 3x (panic selling)
    """

    def __init__(
        self,
        proximity_pct: float = 5.0,
        vol_max_mult: float = 3.0,
        rsi_min: float = 45.0,
        vol_min_mult: float = 0.8,
        ema_tolerance_pct: float = 0.5,
        retest_tolerance_pct: float = 1.0,
        target_pct: float | None = None,
        ema50_slope_min: float = 0.0,
    ):
        self.proximity_pct = proximity_pct
        self.vol_max_mult = vol_max_mult
        self.rsi_min = rsi_min
        self.vol_min_mult = vol_min_mult
        self.ema_tolerance_pct = ema_tolerance_pct
        self.retest_tolerance_pct = retest_tolerance_pct
        self.target_pct = target_pct
        self.ema50_slope_min = ema50_slope_min

    @staticmethod
    def _macd(closes: list[float]) -> tuple[float, float, float]:
        """MACD(12,26,9): returns (macd_line, signal_line, histogram)."""
        ema12 = _ema(closes, 12)
        ema26 = _ema(closes, 26)
        n = len(closes)
        macd_line = [None] * n
        for i in range(n):
            if ema12[i] is not None and ema26[i] is not None:
                macd_line[i] = ema12[i] - ema26[i]
        # Signal line = 9-period EMA of MACD line
        valid_macd = [v for v in macd_line if v is not None]
        if len(valid_macd) < 9:
            return 0.0, 0.0, 0.0
        signal = _ema(valid_macd, 9)
        macd_val = valid_macd[-1]
        signal_val = signal[-1] if signal[-1] is not None else macd_val
        histogram = macd_val - signal_val
        return macd_val, signal_val, histogram

    @staticmethod
    def _adx(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
        """Average Directional Index — measures trend strength."""
        n = len(closes)
        if n < period + 1:
            return 0.0
        plus_dm = []
        minus_dm = []
        tr_list = []
        for i in range(1, n):
            up = highs[i] - highs[i - 1]
            down = lows[i - 1] - lows[i]
            plus_dm.append(max(up, 0) if up > down else 0)
            minus_dm.append(max(down, 0) if down > up else 0)
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
            tr_list.append(tr)
        if len(tr_list) < period:
            return 0.0
        atr = sum(tr_list[:period]) / period
        plus_di_sum = sum(plus_dm[:period]) / period
        minus_di_sum = sum(minus_dm[:period]) / period
        dx_list = []
        for i in range(period, len(tr_list)):
            atr = (atr * (period - 1) + tr_list[i]) / period
            plus_di_sum = (plus_di_sum * (period - 1) + plus_dm[i]) / period
            minus_di_sum = (minus_di_sum * (period - 1) + minus_dm[i]) / period
            if atr == 0:
                continue
            pdi = (plus_di_sum / atr) * 100
            mdi = (minus_di_sum / atr) * 100
            di_sum = pdi + mdi
            if di_sum == 0:
                continue
            dx = abs(pdi - mdi) / di_sum * 100
            dx_list.append(dx)
        if len(dx_list) < period:
            return 0.0
        adx = sum(dx_list[:period]) / period
        for i in range(period, len(dx_list)):
            adx = (adx * (period - 1) + dx_list[i]) / period
        return adx

    def scan(
        self,
        symbol: str,
        opens: list[float],
        highs: list[float],
        lows: list[float],
        closes: list[float],
        volumes: list[float],
        timeframe: str = "daily",
    ) -> list[Signal]:
        signals: list[Signal] = []
        n = len(closes)
        if n < 60:
            return signals

        # Skip indices
        if "-INDEX" in symbol:
            return signals

        current_close = closes[-1]
        current_low = lows[-1]
        current_volume = volumes[-1]
        avg_volume = sum(volumes[-20:]) / min(20, n)
        vol_ratio = current_volume / max(avg_volume, 1)

        # --- 52-week high/low ---
        lookback = min(252, n - 1)
        high_52w = max(highs[-lookback:])
        low_52w = min(lows[-lookback:])
        if high_52w == 0:
            return signals

        dist_from_high = ((high_52w - current_close) / high_52w) * 100
        if dist_from_high > self.proximity_pct:
            return signals

        # --- Technical indicators ---
        ema20 = _ema(closes, 20)
        ema50 = _ema(closes, 50)
        ema20_val = ema20[-1] if ema20[-1] else 0
        ema50_val = ema50[-1] if ema50[-1] else 0

        rsi_vals = _rsi(closes, 14)
        rsi_val = rsi_vals[-1] if rsi_vals[-1] is not None else 50

        atr_vals = _atr(highs, lows, closes)
        atr = atr_vals[-1] if atr_vals[-1] else (high_52w - low_52w) * 0.02

        # --- FILTER 1: EMA50 slope (uptrend confirmation) ---
        ema50_slope = 0.0
        if n >= 55 and ema50[-6] and ema50[-1]:
            ema50_slope = (ema50[-1] - ema50[-6]) / ema50[-6] * 100  # 5-day % change
        ema50_rising = ema50_slope > self.ema50_slope_min
        trend_up = current_close > ema50_val and ema50_rising

        if not trend_up:
            return signals  # KILL: no trend support

        # --- FILTER 2: RSI momentum ---
        if rsi_val < self.rsi_min:
            return signals  # KILL: weak momentum

        # --- FILTER 3: MACD histogram positive ---
        macd_val, signal_val, histogram = self._macd(closes)
        macd_bullish = histogram > 0

        if not macd_bullish:
            return signals  # KILL: bearish momentum

        # --- FILTER 4: ADX trend strength (optional, >=15 = trending) ---
        adx_val = self._adx(highs, lows, closes)

        # --- FILTER 5: Support detection ---
        ema_tol = self.ema_tolerance_pct / 100.0
        at_ema20 = ema20_val > 0 and abs(current_low - ema20_val) / ema20_val < ema_tol
        at_ema50 = ema50_val > 0 and abs(current_low - ema50_val) / ema50_val < ema_tol

        lookback_60 = min(60, n - 20)
        prev_high_close = max(closes[-(lookback_60 + 20):-20]) if n > 80 else high_52w * 0.95
        at_breakout_retest = abs(current_low - prev_high_close) / prev_high_close < self.retest_tolerance_pct / 100.0

        at_support = at_ema20 or at_ema50 or at_breakout_retest
        if not at_support:
            return signals  # KILL: not at support level

        # --- FILTER 6: Volume conditions ---
        vol_ok_min = current_volume >= avg_volume * self.vol_min_mult
        vol_ok_max = current_volume < avg_volume * self.vol_max_mult
        vol_ok = vol_ok_min and vol_ok_max

        if not vol_ok:
            return signals  # KILL: volume too low or panic selling

        # 3-day volume trend: rising volume = accumulation
        vol_trend_ok = True
        if n >= 23:
            vol_3d_avg = sum(volumes[-3:]) / 3
            vol_prev3d_avg = sum(volumes[-6:-3]) / 3
            vol_trend_ok = vol_3d_avg >= vol_prev3d_avg * 0.9  # Allow slight dip

        # --- FILTER 7: Higher lows pattern (accumulation) ---
        higher_lows = 0
        for i in range(n - 1, max(n - 11, 1), -1):
            if i >= 2 and lows[i] > lows[i - 1]:
                higher_lows += 1
        has_accumulation = higher_lows >= 3  # At least 3 higher lows in 10 days

        # --- ALL FILTERS PASSED ---
        support_type = []
        if at_breakout_retest:
            support_type.append(f"breakout retest ({prev_high_close:.2f})")
        if at_ema20:
            support_type.append(f"20 EMA ({ema20_val:.2f})")
        if at_ema50:
            support_type.append(f"50 EMA ({ema50_val:.2f})")

        # --- Confidence scoring (0-100) ---
        conf = 40  # Base: passed all mandatory filters
        conf += 10 if dist_from_high < 3 else (5 if dist_from_high < 5 else 0)  # Proximity
        conf += 10 if len(support_type) >= 2 else 5  # Multiple support levels
        conf += 8 if vol_trend_ok else 0  # Volume trend
        conf += 8 if has_accumulation else 0  # Higher lows
        conf += 7 if adx_val > 20 else 0  # Trend strength
        conf += 7 if rsi_val > 55 else 0  # Strong momentum
        conf += 5 if macd_val > 0 else 0  # MACD above zero line
        conf += 5 if current_close > ema20_val else 0  # Above short-term EMA
        confidence = min(95, conf) / 100.0

        # --- Stop loss & target ---
        sl = current_close - atr * 1.5
        target = _short_target(current_close, high_52w, is_buy=True, pct=self.target_pct)

        # --- Build reason ---
        filters = []
        if vol_trend_ok:
            filters.append("vol rising")
        if has_accumulation:
            filters.append(f"{higher_lows} higher lows")
        if adx_val > 20:
            filters.append(f"ADX {adx_val:.0f}")
        if rsi_val > 55:
            filters.append(f"RSI strong {rsi_val:.0f}")

        signals.append(Signal(
            symbol=symbol,
            strategy=StrategyName.HIGH_SUPPORT,
            signal_type=SignalType.BUY,
            price=current_close,
            stop_loss=round(sl, 2),
            target=round(target, 2),
            confidence=round(confidence, 2),
            reason=(
                f"Near 52W high ({high_52w:.2f}) {dist_from_high:.1f}% away | "
                f"Support: {', '.join(support_type)} | "
                f"EMA50 slope: +{ema50_slope:.2f}% | RSI: {rsi_val:.0f} | "
                f"MACD histogram: {'+' if histogram > 0 else ''}{histogram:.2f} | "
                f"{vol_ratio:.1f}x vol | {', '.join(filters)}"
            ),
            timeframe=timeframe,
            details={
                "high_52w": high_52w,
                "low_52w": low_52w,
                "dist_from_high_pct": round(dist_from_high, 2),
                "ema20": round(ema20_val, 2),
                "ema50": round(ema50_val, 2),
                "ema50_slope": round(ema50_slope, 3),
                "rsi": round(rsi_val, 2),
                "macd": round(macd_val, 3),
                "macd_signal": round(signal_val, 3),
                "macd_histogram": round(histogram, 3),
                "adx": round(adx_val, 1),
                "volume_ratio": round(vol_ratio, 2),
                "vol_trend_rising": vol_trend_ok,
                "higher_lows_count": higher_lows,
                "support_type": support_type,
                "filters_passed": [
                    "trend_ok",
                    "rsi_ok",
                    "macd_ok",
                    "at_support",
                    "vol_ok",
                ] + (["vol_trend"] if vol_trend_ok else [])
                    + (["accumulation"] if has_accumulation else [])
                    + (["adx_trend"] if adx_val > 20 else []),
            },
        ))

        return signals



# ---------------------------------------------------------------------------

# Strategy 5: Volume Shocker Buy

# ---------------------------------------------------------------------------
class VolumeShocker:

    """

    Detects unusual volume spike with price confirmation.

    - Volume > 2x average (shocker volume)

    - Price closes near high (bullish) or breaks resistance

    - Often precedes big moves

    """



    def __init__(self, volume_mult: float = 2.0, target_pct: float | None = None):

        self.volume_mult = volume_mult

        self.target_pct = target_pct



    def scan(

        self,

        symbol: str,

        opens: list[float],

        highs: list[float],

        lows: list[float],

        closes: list[float],

        volumes: list[float],

        timeframe: str = "daily",

    ) -> list[Signal]:

        signals: list[Signal] = []

        n = len(closes)

        if n < 20:

            return signals



        current_close = closes[-1]

        current_open = opens[-1]

        current_high = highs[-1]

        current_low = lows[-1]

        current_volume = volumes[-1]

        avg_volume = sum(volumes[-20:-1]) / 19

        vol_ratio = current_volume / max(avg_volume, 1)



        # Volume must be >= 2x average

        if vol_ratio < self.volume_mult:

            return signals



        # Calculate candle metrics

        body = current_close - current_open

        upper_wick = current_high - max(current_close, current_open)

        lower_wick = min(current_close, current_open) - current_low

        candle_range = current_high - current_low



        if candle_range == 0:

            return signals



        # Bullish: close near high, green candle

        close_near_high = (current_high - current_close) / candle_range < 0.25

        is_green = current_close > current_open

        body_strength = abs(body) / candle_range



        # 20-day high breakout

        high_20d = max(highs[-20:-1])

        breakout = current_close > high_20d



        # EMA support

        ema20 = _ema(closes, 20)

        ema20_val = ema20[-1] if ema20[-1] else 0

        above_ema20 = current_close > ema20_val



        atr_vals = _atr(highs, lows, closes)

        atr = atr_vals[-1] if atr_vals[-1] else candle_range



        # Buy signal: volume shocker + bullish candle

        if is_green and close_near_high and above_ema20:

            confidence = min(1.0, 0.5 + (vol_ratio - 2.0) * 0.1 + body_strength * 0.2 + (0.1 if breakout else 0))

            sl = current_low - atr * 0.5

            target = _short_target(current_close, current_close + atr * 3, is_buy=True, pct=self.target_pct)  # capped from 3x ATR



            reason_parts = [f"Volume {vol_ratio:.1f}x average (shocker!)"]

            reason_parts.append(f"Green candle, close near high")

            if breakout:

                reason_parts.append(f"20-day high breakout")

            reason_parts.append(f"Above 20 EMA ({ema20_val:.2f})")



            signals.append(Signal(

                symbol=symbol,

                strategy=StrategyName.VOLUME_SHOCKER,

                signal_type=SignalType.BUY,

                price=current_close,

                stop_loss=round(sl, 2),

                target=round(target, 2),

                confidence=round(confidence, 2),

                reason=" | ".join(reason_parts),

                timeframe=timeframe,

                details={

                    "volume_ratio": round(vol_ratio, 2),

                    "avg_volume": round(avg_volume, 0),

                    "current_volume": round(current_volume, 0),

                    "body_strength": round(body_strength, 2),

                    "breakout_20d": breakout,

                    "ema20": round(ema20_val, 2),

                },

            ))



        # Bearish volume shocker

        is_red = current_close < current_open

        close_near_low = (current_close - current_low) / candle_range < 0.25

        low_20d = min(lows[-20:-1])

        breakdown = current_close < low_20d

        below_ema20 = current_close < ema20_val



        if is_red and close_near_low and below_ema20:

            confidence = min(1.0, 0.5 + (vol_ratio - 2.0) * 0.1 + body_strength * 0.2 + (0.1 if breakdown else 0))

            sl = current_high + atr * 0.5

            target = _short_target(current_close, current_close - atr * 3, is_buy=False, pct=self.target_pct)



            reason_parts = [f"Volume {vol_ratio:.1f}x average (shocker!)"]

            reason_parts.append(f"Red candle, close near low")

            if breakdown:

                reason_parts.append(f"20-day low breakdown")

            reason_parts.append(f"Below 20 EMA ({ema20_val:.2f})")



            signals.append(Signal(

                symbol=symbol,

                strategy=StrategyName.VOLUME_SHOCKER,

                signal_type=SignalType.SELL,

                price=current_close,

                stop_loss=round(sl, 2),

                target=round(target, 2),

                confidence=round(confidence, 2),

                reason=" | ".join(reason_parts),

                timeframe=timeframe,

                details={

                    "volume_ratio": round(vol_ratio, 2),

                    "avg_volume": round(avg_volume, 0),

                    "current_volume": round(current_volume, 0),

                    "body_strength": round(body_strength, 2),

                    "breakdown_20d": breakdown,

                    "ema20": round(ema20_val, 2),

                },

            ))



        return signals


# ---------------------------------------------------------------------------
# Strategy 7: Medium Term Channel Breakout (Candle Confirmed)
# ---------------------------------------------------------------------------
class MedChannelBreakout:
    """
    Medium-term channel breakout confirmed by candlestick patterns.

    Logic:
    1. Detect medium-term consolidation (20-60 day range)
    2. Price oscillating within a channel (high/low boundaries)
    3. Channel width narrows (squeeze/consolidation)
    4. Breakout above channel high WITH bullish candle confirmation
    5. Volume expansion confirms conviction

    Candle Confirmation (required):
    - Bullish Engulfing, Hammer, Morning Star, Three White Soldiers,
      Piercing Line, or strong green Marubozu

    This catches stocks that:
    - Consolidated for 1-3 months
    - Built energy in a tight channel
    - Breakout with candle pattern confirmation
    - Higher probability than pure breakout (candle adds confirmation)
    """

    def __init__(
        self,
        lookback: int = 30,
        channel_pct: float = 12.0,
        squeeze_pct: float = 6.0,
        volume_mult: float = 1.0,
        min_pattern_score: int = 2,
        target_pct: float | None = None,
    ):
        self.lookback = lookback  # 20-60 day medium term
        self.channel_pct = channel_pct  # max channel width %
        self.squeeze_pct = squeeze_pct  # squeeze threshold %
        self.volume_mult = volume_mult
        self.min_pattern_score = min_pattern_score
        self.target_pct = target_pct
        # Relaxed squeeze: allow 85% of channel width (was 70%)
        self.squeeze_ratio = 0.85

    def _detect_candle_patterns(self, opens, highs, lows, closes) -> list[str]:
        """Detect bullish/bearish candlestick patterns on the last 3 candles."""
        patterns = []
        n = len(closes)
        if n < 3:
            return patterns

        # Last 3 candles
        o1, h1, l1, c1 = opens[-3], highs[-3], lows[-3], closes[-3]
        o2, h2, l2, c2 = opens[-2], highs[-2], lows[-2], closes[-2]
        o3, h3, l3, c3 = opens[-1], highs[-1], lows[-1], closes[-1]

        body1 = abs(c1 - o1)
        body2 = abs(c2 - o2)
        body3 = abs(c3 - o3)
        range1 = h1 - l1 if h1 > l1 else 0.01
        range2 = h2 - l2 if h2 > l2 else 0.01
        range3 = h3 - l3 if h3 > l3 else 0.01

        # --- Bullish Patterns ---

        # Bullish Engulfing (candle 2)
        if c2 > o2 and c1 < o1:
            if c2 > o1 and o2 < c1 and body2 > body1 * 1.2:
                patterns.append("Bullish Engulfing")

        # Hammer (candle 3) - small body, long lower wick
        lower_wick3 = min(o3, c3) - l3
        upper_wick3 = h3 - max(o3, c3)
        if lower_wick3 > body3 * 2 and upper_wick3 < body3 * 0.5 and body3 > 0:
            patterns.append("Hammer")

        # Morning Star (3-candle reversal)
        if c1 < o1 and abs(c2 - o2) < body1 * 0.3 and c3 > o3:
            if c3 > (o1 + c1) / 2:  # close above midpoint of first candle
                patterns.append("Morning Star")

        # Three White Soldiers
        if (c1 > o1 and c2 > o2 and c3 > o3 and
            c2 > c1 and c3 > c2 and
            o2 > o1 and o3 > o2):
            patterns.append("Three White Soldiers")

        # Piercing Line
        if c1 < o1 and c2 > o2:
            midpoint = (o1 + c1) / 2
            if o2 < c1 and c2 > midpoint and c2 < o1:
                patterns.append("Piercing Line")

        # Strong Green Marubozu (last candle)
        if c3 > o3 and body3 > range3 * 0.85:
            patterns.append("Marubozu")

        # --- Bearish Patterns ---

        # Bearish Engulfing
        if c2 < o2 and c1 > o1:
            if c2 < o1 and o2 > c1 and body2 > body1 * 1.2:
                patterns.append("Bearish Engulfing")

        # Evening Star
        if c1 > o1 and abs(c2 - o2) < body1 * 0.3 and c3 < o3:
            if c3 < (o1 + c1) / 2:
                patterns.append("Evening Star")

        # Three Black Crows
        if (c1 < o1 and c2 < o2 and c3 < o3 and
            c2 < c1 and c3 < c2):
            patterns.append("Three Black Crows")

        return patterns

    def scan(
        self,
        symbol: str,
        opens: list[float],
        highs: list[float],
        lows: list[float],
        closes: list[float],
        volumes: list[float],
        timeframe: str = "daily",
    ) -> list[Signal]:
        signals: list[Signal] = []
        n = len(closes)
        if n < self.lookback + 10:
            return signals

        current_close = closes[-1]
        current_volume = volumes[-1]

        # --- Step 1: Compute medium-term channel ---
        range_start = -(self.lookback + 1)
        range_highs = highs[range_start:-1]
        range_lows = lows[range_start:-1]

        channel_high = max(range_highs)
        channel_low = min(range_lows)
        channel_width = channel_high - channel_low

        if channel_high == 0 or channel_width == 0:
            return signals

        channel_width_pct = (channel_width / channel_high) * 100

        # Max channel width for medium term
        if channel_width_pct > self.channel_pct:
            return signals

        # --- Step 2: Check for squeeze/consolidation ---
        recent_high = max(highs[-10:])
        recent_low = min(lows[-10:])
        recent_width_pct = ((recent_high - recent_low) / channel_high) * 100

        # Recent range should be narrower than full channel (consolidation)
        if recent_width_pct > channel_width_pct * self.squeeze_ratio:
            return signals

        # --- Step 3: Price oscillation check ---
        touched_upper = any(h >= channel_high * 0.98 for h in highs[-self.lookback:])
        touched_lower = any(l <= channel_low * 1.02 for l in lows[-self.lookback:])

        if not (touched_upper and touched_lower):
            return signals

        # --- Step 4: Breakout detection ---
        atr_vals = _atr(highs, lows, closes)
        atr = atr_vals[-1] if atr_vals[-1] else channel_width * 0.3

        avg_volume = sum(volumes[-self.lookback:]) / self.lookback
        vol_ratio = current_volume / max(avg_volume, 1)

        # --- Step 5: Candle pattern confirmation (strengthened) ---
        candle_patterns = self._detect_candle_patterns(opens, highs, lows, closes)
        bullish_patterns = [p for p in candle_patterns if p in [
            "Bullish Engulfing", "Hammer", "Morning Star",
            "Three White Soldiers", "Piercing Line", "Marubozu"
        ]]
        bearish_patterns = [p for p in candle_patterns if p in [
            "Bearish Engulfing", "Evening Star", "Three Black Crows"
        ]]

        # Pattern scoring: strong reversals count double — require stronger confirmation
        STRONG_BULLISH = {"Bullish Engulfing", "Morning Star", "Three White Soldiers", "Marubozu"}
        bullish_score = sum(2 if p in STRONG_BULLISH else 1 for p in bullish_patterns)
        bearish_score = len(bearish_patterns) * 2  # all bearish patterns are strong reversals

        # --- Step 6: Generate signals ---

        # BUY: Breakout above channel + strong bullish candle + volume
        if (current_close > channel_high and
            bullish_patterns and
            bullish_score >= self.min_pattern_score and
            vol_ratio >= self.volume_mult):

            confidence = min(1.0, 0.5 + min(bullish_score, 3) * 0.1 + (vol_ratio - 1.0) * 0.1)
            sl = channel_high - atr * 0.5
            target = _short_target(current_close, current_close + channel_width, is_buy=True, pct=self.target_pct)  # capped measured move

            signals.append(Signal(
                symbol=symbol,
                strategy=StrategyName.MED_CHANNEL_BREAKOUT,
                signal_type=SignalType.BUY,
                price=current_close,
                stop_loss=round(sl, 2),
                target=round(target, 2),
                confidence=round(confidence, 2),
                reason=f"{self.lookback}-day channel breakout above {channel_high:.2f} | Channel: {channel_low:.2f}-{channel_high:.2f} ({channel_width_pct:.1f}%) | Candle: {', '.join(bullish_patterns)} (score {bullish_score}) | {vol_ratio:.1f}x volume",
                timeframe=timeframe,
                details={
                    "lookback": self.lookback,
                    "channel_high": channel_high,
                    "channel_low": channel_low,
                    "channel_width_pct": round(channel_width_pct, 2),
                    "candle_patterns": bullish_patterns,
                    "pattern_score": bullish_score,
                    "volume_ratio": round(vol_ratio, 2),
                    "squeeze_width_pct": round(recent_width_pct, 2),
                },
            ))

        # SELL: Breakdown below channel + strong bearish candle + volume
        if (current_close < channel_low and
            bearish_patterns and
            bearish_score >= self.min_pattern_score and
            vol_ratio >= self.volume_mult):

            confidence = min(1.0, 0.5 + min(bearish_score, 3) * 0.1 + (vol_ratio - 1.0) * 0.1)
            sl = channel_low + atr * 0.5
            target = _short_target(current_close, current_close - channel_width, is_buy=False, pct=self.target_pct)

            signals.append(Signal(
                symbol=symbol,
                strategy=StrategyName.MED_CHANNEL_BREAKOUT,
                signal_type=SignalType.SELL,
                price=current_close,
                stop_loss=round(sl, 2),
                target=round(target, 2),
                confidence=round(confidence, 2),
                reason=f"{self.lookback}-day channel breakdown below {channel_low:.2f} | Channel: {channel_low:.2f}-{channel_high:.2f} ({channel_width_pct:.1f}%) | Candle: {', '.join(bearish_patterns)} (score {bearish_score}) | {vol_ratio:.1f}x volume",
                timeframe=timeframe,
                details={
                    "lookback": self.lookback,
                    "channel_high": channel_high,
                    "channel_low": channel_low,
                    "channel_width_pct": round(channel_width_pct, 2),
                    "candle_patterns": bearish_patterns,
                    "pattern_score": bearish_score,
                    "volume_ratio": round(vol_ratio, 2),
                    "squeeze_width_pct": round(recent_width_pct, 2),
                },
            ))

        return signals


# ---------------------------------------------------------------------------
# Strategy: Trendline Channel Breakout (ascending / descending / horizontal)
# ---------------------------------------------------------------------------
class ChannelBreakout:
    """
    Trendline channel breakout — detects a parallel-line channel from swing
    highs/lows, classifies it (ASCENDING / DESCENDING / HORIZONTAL = square
    block), and fires when price breaks OUT of the channel with volume.

    - ASCENDING  (both lines up)   : close above upper line  -> BUY
    - DESCENDING (both lines down) : close below lower line  -> SELL
    - HORIZONTAL (flat = rectangle): close above resistance  -> BUY
                                     close below support     -> SELL
    SL = opposite channel line. Target = channel height projected (capped 3.5%).
    """

    def __init__(
        self,
        lookback: int = 40,
        min_points: int = 3,
        min_width_pct: float = 0.4,
        max_width_pct: float = 10.0,
        slope_threshold: float = 0.05,
        volume_mult: float = 1.0,
        target_pct: float | None = None,
    ):
        self.lookback = lookback
        self.min_points = min_points
        self.min_width_pct = min_width_pct
        self.max_width_pct = max_width_pct
        self.slope_threshold = slope_threshold  # %/bar to call it trending
        self.volume_mult = volume_mult
        self.target_pct = target_pct

    @staticmethod
    def _linreg(points: list[tuple]) -> tuple[float, float]:
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        m = len(xs)
        sx, sy = sum(xs), sum(ys)
        sxx = sum(x * x for x in xs)
        sxy = sum(x * y for x, y in zip(xs, ys))
        denom = m * sxx - sx * sx
        if abs(denom) < 1e-9:
            return 0.0, sy / m
        slope = (m * sxy - sx * sy) / denom
        intercept = (sy - slope * sx) / m
        return slope, intercept

    def scan(
        self,
        symbol: str,
        opens: list[float],
        highs: list[float],
        lows: list[float],
        closes: list[float],
        volumes: list[float],
        timeframe: str = "daily",
    ) -> list[Signal]:
        signals = []
        n = len(closes)
        win = min(self.lookback, n - 5)
        if win < 20 or n < win + 5:
            return signals

        hs, ls, cs = highs[-(win + 1):], lows[-(win + 1):], closes[-(win + 1):]
        vols = volumes[-(win + 1):]

        # Swing highs / lows inside the window
        sw_hi = [(i, hs[i]) for i in range(2, win - 1) if hs[i] > hs[i - 1] and hs[i] >= hs[i + 1]]
        sw_lo = [(i, ls[i]) for i in range(2, win - 1) if ls[i] < ls[i - 1] and ls[i] <= ls[i + 1]]
        if len(sw_hi) < self.min_points or len(sw_lo) < self.min_points:
            return signals

        s_hi, i_hi = self._linreg(sw_hi)
        s_lo, i_lo = self._linreg(sw_lo)

        # Parallelism: the two trendlines must have similar slope
        if abs(s_hi - s_lo) > max(abs((s_hi + s_lo) / 2) * 0.8, 0.05):
            return signals

        def upper(x): return s_hi * x + i_hi
        def lower(x): return s_lo * x + i_lo

        up_now = upper(win)
        lo_now = lower(win)
        width = up_now - lo_now
        width_pct = width / cs[-1] * 100
        if width_pct < self.min_width_pct or width_pct > self.max_width_pct:
            return signals

        slope_pct = (s_hi + s_lo) / 2 / cs[-1] * 100
        if slope_pct > self.slope_threshold:
            ch_type = "ASCENDING"
        elif slope_pct < -self.slope_threshold:
            ch_type = "DESCENDING"
        else:
            ch_type = "HORIZONTAL"

        cur_close = cs[-1]
        avg_vol = sum(volumes[-min(20, n):-1]) / max(min(20, n) - 1, 1)
        vol_ratio = volumes[-1] / max(avg_vol, 1)
        atr_vals = _atr(highs, lows, closes)
        atr = atr_vals[-1] if atr_vals[-1] else cur_close * 0.02

        # BUY: break above upper line (ascending or horizontal)
        if cur_close > up_now and ch_type in ("ASCENDING", "HORIZONTAL") and vol_ratio >= self.volume_mult:
            sl = lo_now - atr * 0.5
            target = _short_target(cur_close, cur_close + width, is_buy=True, pct=self.target_pct)
            confidence = min(1.0, 0.55 + (vol_ratio > 1.5) * 0.15 + (ch_type == "ASCENDING") * 0.1)
            signals.append(Signal(
                symbol=symbol,
                strategy=StrategyName.CHANNEL_BREAKOUT,
                signal_type=SignalType.BUY,
                price=cur_close,
                stop_loss=round(sl, 2),
                target=round(target, 2),
                confidence=round(confidence, 2),
                reason=f"{ch_type} channel breakout above {up_now:.2f} (upper line) | lower line {lo_now:.2f} | {vol_ratio:.1f}x vol",
                timeframe=timeframe,
                details={
                    "channel_type": ch_type,
                    "upper_line": round(up_now, 2),
                    "lower_line": round(lo_now, 2),
                    "slope_pct_per_bar": round(slope_pct, 4),
                    "width_pct": round(width_pct, 2),
                    "volume_ratio": round(vol_ratio, 2),
                },
            ))

        # SELL: break below lower line (descending or horizontal)
        if cur_close < lo_now and ch_type in ("DESCENDING", "HORIZONTAL") and vol_ratio >= self.volume_mult:
            sl = up_now + atr * 0.5
            target = _short_target(cur_close, cur_close - width, is_buy=False, pct=self.target_pct)
            confidence = min(1.0, 0.55 + (vol_ratio > 1.5) * 0.15 + (ch_type == "DESCENDING") * 0.1)
            signals.append(Signal(
                symbol=symbol,
                strategy=StrategyName.CHANNEL_BREAKOUT,
                signal_type=SignalType.SELL,
                price=cur_close,
                stop_loss=round(sl, 2),
                target=round(target, 2),
                confidence=round(confidence, 2),
                reason=f"{ch_type} channel breakdown below {lo_now:.2f} (lower line) | upper line {up_now:.2f} | {vol_ratio:.1f}x vol",
                timeframe=timeframe,
                details={
                    "channel_type": ch_type,
                    "upper_line": round(up_now, 2),
                    "lower_line": round(lo_now, 2),
                    "slope_pct_per_bar": round(slope_pct, 4),
                    "width_pct": round(width_pct, 2),
                    "volume_ratio": round(vol_ratio, 2),
                },
            ))

        return signals


# ---------------------------------------------------------------------------
# Strategy: Watchlist Range Breakout (short-term options friendly)
# ---------------------------------------------------------------------------
class WatchlistBreakout:
    """
    Watchlist breakout rules for short-term options traders:
    - BUY when the close FRESHLY breaks above the recent N-day high
      (range breakout) while price is still above `support`.
    - IGNORE (no signal) once price closes BELOW `support` — trade thesis invalid.
    - Target is the short-term capped target (3.5% default) — options-friendly.

    Applies to ALL stocks scanned (indices excluded). Per-stock overrides live in
    watchlist_rules.json, e.g. {"NSE:CDSL-EQ": {"lookback": 15, "support_mode": "combo"}}.

    Support resolution (highest = nearest/strongest level):
      - "manual" : use the exact `support` value from config.
      - "combo"  : max(9 DMA, 21 DMA, recent swing low)  [default]
      - "dma"    : max(9 DMA, 21 DMA)
      - "swing"  : recent swing low (candlestick structure floor)
    """

    def __init__(self, config: dict | None = None, target_pct: float | None = None,
                 default_lookback: int = 15, apply_to_all: bool = True):
        self.config = config or {}
        self.target_pct = target_pct
        self.default_lookback = default_lookback
        self.apply_to_all = apply_to_all

    def _resolve_support(self, entry: dict, closes: list[float], lows: list[float], lookback: int) -> tuple[float, str]:
        """Return (support_level, mode_used). Manual value wins if set."""
        manual = entry.get("support") or 0
        if manual > 0:
            return manual, "manual"

        mode = entry.get("support_mode", "combo")
        sma9 = _sma(closes, 9)[-1]
        sma21 = _sma(closes, 21)[-1]
        swing_low = min(lows[-(lookback + 1):]) if len(lows) >= lookback + 1 else min(lows)

        cands: list[float] = []
        if mode in ("combo", "dma"):
            cands += [v for v in (sma9, sma21) if v and v > 0]
        if mode in ("combo", "swing"):
            if swing_low and swing_low > 0:
                cands.append(swing_low)

        if not cands:
            cands = [v for v in (sma9, sma21) if v and v > 0] or [swing_low]
        return max(cands), mode

    def scan(
        self,
        symbol: str,
        opens: list[float],
        highs: list[float],
        lows: list[float],
        closes: list[float],
        volumes: list[float],
        timeframe: str = "daily",
    ) -> list[Signal]:
        entry = self.config.get(symbol)
        if "-INDEX" in symbol:
            return []  # Indices cannot be traded directly — skip
        if entry is None and not self.apply_to_all:
            return []
        if entry is None:
            entry = {}
        if len(closes) < 3:
            return []

        lookback = entry.get("lookback") or self.default_lookback
        prev_close = closes[-2]
        current_close = closes[-1]

        # Recent range from previous bars (excluding the current bar)
        recent_highs = highs[-(lookback + 1):-1]
        recent_high = max(recent_highs)

        support, support_mode = self._resolve_support(entry, closes, lows, lookback)
        if support <= 0:
            return []

        # Below support — ignore the stock entirely
        if current_close < support:
            return []

        atr_vals = _atr(highs, lows, closes)
        atr = atr_vals[-1] if atr_vals[-1] else current_close * 0.02

        # Fresh breakout above recent high (prev close <= high, current close > high)
        if current_close > recent_high and prev_close <= recent_high:
            sl = support
            target = _short_target(current_close, current_close + max(current_close - support, atr * 2),
                                   is_buy=True, pct=self.target_pct)
            confidence = min(1.0, 0.65 + (current_close > recent_high * 1.02) * 0.15)
            return [Signal(
                symbol=symbol,
                strategy=StrategyName.WATCHLIST_BREAKOUT,
                signal_type=SignalType.BUY,
                price=current_close,
                stop_loss=round(sl, 2),
                target=round(target, 2),
                confidence=round(confidence, 2),
                reason=f"Watchlist: fresh {lookback}-day range breakout above {recent_high:.2f} (prev {prev_close:.2f}) | Support {support:.2f} ({support_mode}) | SL below support",
                timeframe=timeframe,
                details={
                    "lookback": lookback,
                    "recent_high": recent_high,
                    "support": support,
                    "support_mode": support_mode,
                    "state": "BREAKOUT",
                    "atr": round(atr, 2),
                },
            )]

        return []


# ---------------------------------------------------------------------------
# Strategy: Buy on Retracement (buy the dip in an uptrend)
# ---------------------------------------------------------------------------
class BuyOnRetracement:
    """
    Buy-on-retracement for short-term options traders:
    1. UPTREND: price above EMA(fast) and EMA(slow), slow EMA rising.
    2. PULLBACK: price retraced >= min_pullback% from the recent swing high.
    3. SUPPORT: last 3 days' lows tapped the fast EMA zone.
    4. REVERSAL: bullish candle, close back above fast EMA, RSI recovering.
    5. VOLUME: healthy pullback (volume below cap) — not a breakdown.
    SL = pullback swing low (with ATR buffer). Target = swing high, capped 3.5%.
    """

    def __init__(
        self,
        ema_fast: int = 20,
        ema_slow: int = 50,
        pullback_tolerance_pct: float = 1.5,
        min_pullback_pct: float = 2.0,
        rsi_min: float = 45.0,
        vol_max_mult: float = 1.5,
        lookback_high: int = 20,
        target_pct: float | None = None,
        min_rr: float = 0.0,
        max_sl_pct: float = 100.0,
        min_target_pct: float = 0.0,
        extension_pct: float = 4.8,
        trail_breakeven: bool = True,
        trail_activate_pct: float = 1.0,
        trail_buffer_pct: float = 1.0,
    ):
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.pullback_tolerance_pct = pullback_tolerance_pct
        self.min_pullback_pct = min_pullback_pct
        self.rsi_min = rsi_min
        self.vol_max_mult = vol_max_mult
        self.lookback_high = lookback_high
        self.target_pct = target_pct
        # Quality gates (fixes the inverted R:R problem)
        self.min_rr = min_rr          # reject if target/SL < 1.5:1
        self.max_sl_pct = max_sl_pct  # reject if SL wider than 3% of entry
        self.min_target_pct = min_target_pct  # reject if target < 2% away
        # Cash-style target: prior swing high extended by this % (default 4.8%)
        self.extension_pct = extension_pct
        # Breakeven trail: once price moves +activate% above entry, raise SL to
        # cost - buffer% (protects the trade while letting winners run).
        self.trail_breakeven = trail_breakeven
        self.trail_activate_pct = trail_activate_pct
        self.trail_buffer_pct = trail_buffer_pct

    def scan(
        self,
        symbol: str,
        opens: list[float],
        highs: list[float],
        lows: list[float],
        closes: list[float],
        volumes: list[float],
        timeframe: str = "daily",
    ) -> list[Signal]:
        signals = []
        n = len(closes)
        if n < 60:
            return signals

        current_close = closes[-1]
        current_open = opens[-1]
        current_volume = volumes[-1]
        avg_volume = sum(volumes[-20:]) / max(min(20, n), 1)

        ema_f = _ema(closes, self.ema_fast)
        ema_s = _ema(closes, self.ema_slow)
        ema_f_val = ema_f[-1] if ema_f[-1] else 0
        ema_s_val = ema_s[-1] if ema_s[-1] else 0
        ema_s_prev5 = ema_s[-5] if ema_s[-5] else ema_s_val

        # 1. Uptrend: price above both EMAs, slow EMA rising
        if ema_s_val <= 0 or ema_f_val <= 0:
            return signals
        if current_close <= ema_f_val or current_close <= ema_s_val:
            return signals
        if ema_s_val <= ema_s_prev5:
            return signals

        # 2. Recent swing high (excluding current bar)
        swing_high = max(highs[-self.lookback_high:-1]) if n > self.lookback_high else max(highs[:-1])
        if swing_high <= 0:
            return signals

        # 3. A real pullback happened from the swing high
        pullback_pct = (swing_high - current_close) / swing_high * 100
        if pullback_pct < self.min_pullback_pct:
            return signals

        # 4. Price tapped the fast-EMA support zone during the pullback
        tol = self.pullback_tolerance_pct / 100.0
        touched_support = any(abs(low - ema_f_val) / ema_f_val < tol for low in lows[-3:])

        # 5. Reversal confirmation on the current bar
        bullish_candle = current_close > current_open
        closed_above = current_close > ema_f_val
        rsi_vals = _rsi(closes, 14)
        rsi = rsi_vals[-1] if rsi_vals[-1] is not None else 0
        rsi_ok = rsi >= self.rsi_min

        # 6. Healthy volume (quiet pullback, not a breakdown)
        vol_ratio = current_volume / max(avg_volume, 1)
        vol_ok = vol_ratio < self.vol_max_mult

        if touched_support and bullish_candle and closed_above and rsi_ok and vol_ok:
            atr_vals = _atr(highs, lows, closes)
            atr = atr_vals[-1] if atr_vals[-1] else current_close * 0.02
            swing_low = min(lows[-5:])
            # SL = pullback swing low with ATR buffer, not below trend EMA
            sl = min(swing_low - atr * 0.25, ema_s_val * 0.99)
            # Cash-style target: prior swing high extended by `extension_pct`
            # (e.g. 4.8% past the old high). target_pct param overrides to a
            # short-term % cap if set.
            if self.target_pct is not None:
                target = _short_target(current_close, swing_high, is_buy=True, pct=self.target_pct)
            else:
                target = swing_high * (1 + self.extension_pct / 100.0)

            # ── Quality gates: reject bad risk:reward setups ──
            sl_dist = current_close - sl
            if sl_dist <= 0:
                return signals
            target_dist = target - current_close
            rr = target_dist / sl_dist
            sl_pct = sl_dist / current_close * 100
            target_pct = target_dist / current_close * 100
            if rr < self.min_rr or sl_pct > self.max_sl_pct or target_pct < self.min_target_pct:
                return signals

            confidence = min(1.0, 0.55 + (pullback_pct > 3) * 0.1 + (rsi > 55) * 0.1 + (vol_ratio < 1.0) * 0.1)
            trail_note = ""
            if self.trail_breakeven:
                trail_note = f" | Trail SL to cost-{self.trail_buffer_pct:.0f}% at +{self.trail_activate_pct:.0f}%"
            signals.append(Signal(
                symbol=symbol,
                strategy=StrategyName.BUY_ON_RETRACEMENT,
                signal_type=SignalType.BUY,
                price=current_close,
                stop_loss=round(sl, 2),
                target=round(target, 2),
                confidence=round(confidence, 2),
                reason=f"Uptrend retracement {pullback_pct:.1f}% from swing high {swing_high:.2f} | EMA{self.ema_fast} support | RSI {rsi:.1f} | {vol_ratio:.1f}x vol | R:R 1:{rr:.1f}{trail_note}",
                timeframe=timeframe,
                details={
                    "swing_high": round(swing_high, 2),
                    "pullback_pct": round(pullback_pct, 2),
                    "ema_fast": round(ema_f_val, 2),
                    "ema_slow": round(ema_s_val, 2),
                    "rsi": round(rsi, 2),
                    "volume_ratio": round(vol_ratio, 2),
                    "rr": round(rr, 2),
                    "sl_pct": round(sl_pct, 2),
                    "trail": {
                        "type": "breakeven",
                        "activate_pct": self.trail_activate_pct,
                        "buffer_pct": self.trail_buffer_pct,
                    },
                },
            ))
        return signals


# ---------------------------------------------------------------------------
# Option Strike Suggestion Engine
# ---------------------------------------------------------------------------
# Indices: NIFTY (50-pt strikes), BANKNIFTY (100-pt strikes), FINNIFTY (50-pt strikes)
INDICES_INFO = {
    "NSE:NIFTY50-INDEX": {"name": "NIFTY50", "strike_step": 50, "lot_size": 50},
    "NSE:BANKNIFTY-INDEX": {"name": "BANKNIFTY", "strike_step": 100, "lot_size": 15},
    "NSE:FINNIFTY-INDEX": {"name": "FINNIFTY", "strike_step": 50, "lot_size": 40},
}


def suggest_option_strike(
    symbol: str,
    current_price: float,
    signal_type: SignalType,
    confidence: float,
    sl: float,
    target: float,
) -> dict:
    """
    Suggest the optimal option strike price for index trades.

    Logic:
    - BUY signal → suggest ATM or 1-strike OTM CE (Call)
    - SELL signal → suggest ATM or 1-strike OTM PE (Put)
    - Strong confidence (>=0.7) → ATM (faster move, higher delta)
    - Medium confidence (0.5-0.7) → 1-strike OTM (cheaper, better R:R)
    - Weak confidence (<0.5) → skip (not enough edge)

    Returns dict with:
    - option_type: CE or PE
    - suggested_strike: rounded to strike_step
    - strike_distance: how far from ATM
    - premium_estimate: rough ATM premium (historical)
    - rationale: why this strike
    """
    info = INDICES_INFO.get(symbol)
    if not info:
        return {"option_type": "N/A", "suggested_strike": 0, "rationale": "Not an index"}

    step = info["strike_step"]
    lot_size = info["lot_size"]

    # Round current price to nearest strike
    atm_strike = round(current_price / step) * step

    # Determine option type
    if signal_type == SignalType.BUY:
        option_type = "CE"  # Bullish → buy Call
    else:
        option_type = "PE"  # Bearish → buy Put

    # Strike selection based on confidence
    if confidence >= 0.7:
        # Strong signal → ATM (highest delta, moves fastest)
        strike = atm_strike
        rationale = f"Strong signal ({confidence:.0%}) → ATM {option_type} for maximum delta"
        strike_distance = 0
    elif confidence >= 0.5:
        # Medium signal → 1-strike OTM (cheaper, better R:R)
        if signal_type == SignalType.BUY:
            strike = atm_strike + step  # 1 strike above for CE
        else:
            strike = atm_strike - step  # 1 strike below for PE
        rationale = f"Medium signal ({confidence:.0%}) → 1-strike OTM {option_type} for better R:R"
        strike_distance = 1
    else:
        # Weak signal → 2 strikes OTM (cheapest, highest leverage)
        if signal_type == SignalType.BUY:
            strike = atm_strike + 2 * step
        else:
            strike = atm_strike - 2 * step
        rationale = f"Weak signal ({confidence:.0%}) → 2-strike OTM {option_type} for maximum leverage"
        strike_distance = 2

    # R:R check for option trade
    sl_distance_pct = abs(current_price - sl) / current_price * 100
    target_distance_pct = abs(target - current_price) / current_price * 100
    option_rr = target_distance_pct / max(sl_distance_pct, 0.1)

    return {
        "option_type": option_type,
        "suggested_strike": int(strike),
        "atm_strike": int(atm_strike),
        "strike_distance": strike_distance,
        "lot_size": lot_size,
        "strike_step": step,
        "rationale": rationale,
        "underlying": current_price,
        "sl_distance_pct": round(sl_distance_pct, 2),
        "target_distance_pct": round(target_distance_pct, 2),
        "option_rr": round(option_rr, 2),
        "expiry_type": "weekly",
    }


# ---------------------------------------------------------------------------
# Strategy: Index Range Breakout (15-min, options-optimized)
# ---------------------------------------------------------------------------
class IndexRangeBreakout:
    """
    Index Range Breakout — designed for index options trading.

    Logic (15-minute timeframe):
    1. Detect consolidation: price oscillates in a tight range for 10+ bars (2.5+ hours)
    2. Measure range: high/low boundaries, width %, duration
    3. Breakout: close above/below range with volume confirmation
    4. Candle confirmation: strong bullish/bearish candle at breakout
    5. Generate signal with option strike suggestion

    Key features:
    - Uses 15-min candles (balances speed vs noise)
    - Range must be tight (0.3%-1.5% of index) to be actionable
    - Requires volume expansion (1.5x avg) for conviction
    - Targets measured move (range width projected)
    - Stop loss at opposite range boundary
    - R:R minimum 1:1.5
    """

    # Only for indices
    INDEX_SYMBOLS = {"NSE:NIFTY50-INDEX", "NSE:BANKNIFTY-INDEX", "NSE:FINNIFTY-INDEX"}

    def __init__(
        self,
        min_bars: int = 10,
        max_range_pct: float = 1.5,
        min_range_pct: float = 0.2,
        volume_mult: float = 1.5,
        atr_period: int = 14,
        target_pct: float | None = None,
        min_rr: float = 1.5,
    ):
        self.min_bars = min_bars  # Minimum consolidation bars
        self.max_range_pct = max_range_pct  # Max range width % (too wide = not consolidation)
        self.min_range_pct = min_range_pct  # Min range width % (too tight = no opportunity)
        self.volume_mult = volume_mult  # Volume confirmation multiplier
        self.atr_period = atr_period
        self.target_pct = target_pct
        self.min_rr = min_rr  # Minimum risk:reward ratio

    def scan(
        self,
        symbol: str,
        opens: list[float],
        highs: list[float],
        lows: list[float],
        closes: list[float],
        volumes: list[float],
        timeframe: str = "15min",
    ) -> list[Signal]:
        signals: list[Signal] = []

        # Only run on indices
        if symbol not in self.INDEX_SYMBOLS:
            return signals

        n = len(closes)
        if n < self.min_bars + 5:
            return signals

        current_close = closes[-1]
        current_volume = volumes[-1]

        # --- Step 1: Detect consolidation range ---
        # Look at the last N bars to find the range
        lookback_bars = min(30, n - 5)  # Use up to 30 bars (7.5 hours)
        range_highs = highs[-lookback_bars:-1]  # Exclude current bar
        range_lows = lows[-lookback_bars:-1]

        range_high = max(range_highs)
        range_low = min(range_lows)
        range_width = range_high - range_low

        if range_high == 0 or range_width == 0:
            return signals

        range_width_pct = (range_width / range_high) * 100

        # Range must be within acceptable bounds
        if range_width_pct < self.min_range_pct or range_width_pct > self.max_range_pct:
            return signals

        # --- Step 2: Check consolidation quality ---
        # Price should oscillate within range (touch both sides)
        touched_upper = any(h >= range_high * 0.99 for h in highs[-lookback_bars:])
        touched_lower = any(l <= range_low * 1.01 for l in lows[-lookback_bars:])

        if not (touched_upper and touched_lower):
            return signals

        # Count how many bars stayed within range (consolidation quality)
        bars_in_range = sum(
            1 for i in range(-lookback_bars, 0)
            if range_low * 0.99 <= lows[i] and highs[i] <= range_high * 1.01
        )
        consolidation_quality = bars_in_range / lookback_bars

        if consolidation_quality < 0.7:  # At least 70% of bars in range
            return signals

        # --- Step 3: Breakout detection ---
        atr_vals = _atr(highs, lows, closes)
        atr = atr_vals[-1] if atr_vals[-1] else range_width * 0.5

        avg_volume = sum(volumes[-lookback_bars:]) / lookback_bars
        vol_ratio = current_volume / max(avg_volume, 1)

        # Bullish breakout: close above range high with volume
        if current_close > range_high and vol_ratio >= self.volume_mult:
            # Candle confirmation: bullish candle (close > open, close near high)
            body = current_close - opens[-1]
            candle_range = highs[-1] - lows[-1]
            is_bullish = body > 0 and candle_range > 0
            close_near_high = (highs[-1] - current_close) / candle_range < 0.3 if candle_range > 0 else False

            if not (is_bullish and close_near_high):
                return signals

            # Calculate R:R
            sl = range_high - atr * 0.5  # Stop at range boundary - ATR buffer
            measured_move = range_width  # Target = range width projected
            target = current_close + measured_move

            # Apply short-term cap if set
            if self.target_pct:
                target = _short_target(current_close, target, is_buy=True, pct=self.target_pct)

            sl_distance = current_close - sl
            target_distance = target - current_close
            rr = target_distance / max(sl_distance, 0.01)

            if rr < self.min_rr:
                return signals

            confidence = min(1.0, 0.55 + (consolidation_quality - 0.7) * 0.5 + (vol_ratio - self.volume_mult) * 0.1)

            # Suggest option strike
            strike_info = suggest_option_strike(
                symbol, current_close, SignalType.BUY, confidence, sl, target
            )

            signals.append(Signal(
                symbol=symbol,
                strategy=StrategyName.INDEX_RANGE_BREAKOUT,
                signal_type=SignalType.BUY,
                price=current_close,
                stop_loss=round(sl, 2),
                target=round(target, 2),
                confidence=round(confidence, 2),
                reason=f"15min range breakout above {range_high:.2f} | Range: {range_low:.2f}-{range_high:.2f} ({range_width_pct:.1f}%) | {vol_ratio:.1f}x volume | R:R 1:{rr:.1f} | {strike_info['option_type']} {strike_info['suggested_strike']}",
                timeframe=timeframe,
                details={
                    "range_high": round(range_high, 2),
                    "range_low": round(range_low, 2),
                    "range_width_pct": round(range_width_pct, 2),
                    "consolidation_bars": lookback_bars,
                    "consolidation_quality": round(consolidation_quality, 2),
                    "volume_ratio": round(vol_ratio, 2),
                    "rr": round(rr, 2),
                    "option_strike": strike_info,
                },
            ))

        # Bearish breakdown: close below range low with volume
        if current_close < range_low and vol_ratio >= self.volume_mult:
            body = opens[-1] - current_close
            candle_range = highs[-1] - lows[-1]
            is_bearish = body > 0 and candle_range > 0
            close_near_low = (current_close - lows[-1]) / candle_range < 0.3 if candle_range > 0 else False

            if not (is_bearish and close_near_low):
                return signals

            sl = range_low + atr * 0.5
            measured_move = range_width
            target = current_close - measured_move

            if self.target_pct:
                target = _short_target(current_close, target, is_buy=False, pct=self.target_pct)

            sl_distance = sl - current_close
            target_distance = current_close - target
            rr = target_distance / max(sl_distance, 0.01)

            if rr < self.min_rr:
                return signals

            confidence = min(1.0, 0.55 + (consolidation_quality - 0.7) * 0.5 + (vol_ratio - self.volume_mult) * 0.1)

            strike_info = suggest_option_strike(
                symbol, current_close, SignalType.SELL, confidence, sl, target
            )

            signals.append(Signal(
                symbol=symbol,
                strategy=StrategyName.INDEX_RANGE_BREAKOUT,
                signal_type=SignalType.SELL,
                price=current_close,
                stop_loss=round(sl, 2),
                target=round(target, 2),
                confidence=round(confidence, 2),
                reason=f"15min range breakdown below {range_low:.2f} | Range: {range_low:.2f}-{range_high:.2f} ({range_width_pct:.1f}%) | {vol_ratio:.1f}x volume | R:R 1:{rr:.1f} | {strike_info['option_type']} {strike_info['suggested_strike']}",
                timeframe=timeframe,
                details={
                    "range_high": round(range_high, 2),
                    "range_low": round(range_low, 2),
                    "range_width_pct": round(range_width_pct, 2),
                    "consolidation_bars": lookback_bars,
                    "consolidation_quality": round(consolidation_quality, 2),
                    "volume_ratio": round(vol_ratio, 2),
                    "rr": round(rr, 2),
                    "option_strike": strike_info,
                },
            ))

        return signals


# ---------------------------------------------------------------------------
# Strategy: Index Support/Resistance (Choppy Market)
# ---------------------------------------------------------------------------
class IndexSupportResistance:
    """
    Index Support/Resistance — buy on support, sell on resistance in choppy markets.

    Logic (15-minute timeframe):
    1. Detect choppy market: ADX < 25, price oscillating in range
    2. Identify support: price bounced from same level 2+ times
    3. Identify resistance: price rejected from same level 2+ times
    4. Buy at support with bullish confirmation candle
    5. Sell at resistance with bearish confirmation candle
    6. Targets: opposite boundary of range

    Key features:
    - Only triggers in choppy/range-bound markets (ADX < 25)
    - Requires multiple touches at S/R level (2+ bounces)
    - Candle confirmation at entry (hammer at support, shooting star at resistance)
    - Tighter stops (just beyond S/R level)
    - Targets opposite boundary of range
    - Ideal for weekly options (range trades are common in indices)
    """

    INDEX_SYMBOLS = {"NSE:NIFTY50-INDEX", "NSE:BANKNIFTY-INDEX", "NSE:FINNIFTY-INDEX"}

    def __init__(
        self,
        min_bounces: int = 2,
        adx_period: int = 14,
        adx_max: float = 25.0,
        touch_tolerance_pct: float = 0.15,
        min_range_pct: float = 0.3,
        volume_mult: float = 1.2,
        target_pct: float | None = None,
        min_rr: float = 1.5,
    ):
        self.min_bounces = min_bounces  # Minimum bounces at S/R level
        self.adx_period = adx_period
        self.adx_max = adx_max  # ADX below this = choppy market
        self.touch_tolerance_pct = touch_tolerance_pct  # How close price must be to S/R
        self.min_range_pct = min_range_pct  # Minimum range width for trade
        self.volume_mult = volume_mult
        self.target_pct = target_pct
        self.min_rr = min_rr

    def _calc_adx(self, highs, lows, closes) -> float:
        """Calculate ADX (Average Directional Index) for trend strength."""
        n = len(closes)
        if n < self.adx_period + 2:
            return 50.0  # Default high ADX (assume trending if can't calculate)

        # True Range
        trs = []
        for i in range(1, n):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            trs.append(tr)

        # +DM and -DM
        plus_dm = []
        minus_dm = []
        for i in range(1, n):
            up_move = highs[i] - highs[i - 1]
            down_move = lows[i - 1] - lows[i]
            plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0)
            minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0)

        # Smoothed TR, +DM, -DM (Wilder's smoothing)
        period = self.adx_period
        if len(trs) < period:
            return 50.0

        atr_smooth = sum(trs[:period]) / period
        plus_dm_smooth = sum(plus_dm[:period]) / period
        minus_dm_smooth = sum(minus_dm[:period]) / period

        dx_vals = []
        for i in range(period, len(trs)):
            atr_smooth = (atr_smooth * (period - 1) + trs[i]) / period
            plus_dm_smooth = (plus_dm_smooth * (period - 1) + plus_dm[i]) / period
            minus_dm_smooth = (minus_dm_smooth * (period - 1) + minus_dm[i]) / period

            if atr_smooth == 0:
                dx_vals.append(0)
                continue

            plus_di = (plus_dm_smooth / atr_smooth) * 100
            minus_di = (minus_dm_smooth / atr_smooth) * 100
            di_sum = plus_di + minus_di

            if di_sum == 0:
                dx_vals.append(0)
            else:
                dx = abs(plus_di - minus_di) / di_sum * 100
                dx_vals.append(dx)

        if not dx_vals:
            return 50.0

        # ADX = smoothed DX
        adx = sum(dx_vals[:period]) / period
        for dx in dx_vals[period:]:
            adx = (adx * (period - 1) + dx) / period

        return adx

    def _find_support_resistance(self, highs, lows, closes) -> dict:
        """
        Find support and resistance levels using multiple touches.
        Returns dict with 'support' and 'resistance' lists.
        """
        n = len(closes)
        if n < 10:
            return {"support": [], "resistance": []}

        # Use recent 50 bars (or all if less)
        lookback = min(50, n)
        recent_highs = highs[-lookback:]
        recent_lows = lows[-lookback:]
        recent_closes = closes[-lookback:]

        # Find swing highs and lows
        swing_highs = []
        swing_lows = []

        for i in range(2, lookback - 2):
            # Swing high: high is higher than 2 bars before and after
            if (recent_highs[i] >= recent_highs[i-1] and
                recent_highs[i] >= recent_highs[i-2] and
                recent_highs[i] >= recent_highs[i+1] and
                recent_highs[i] >= recent_highs[i+2]):
                swing_highs.append(recent_highs[i])

            # Swing low: low is lower than 2 bars before and after
            if (recent_lows[i] <= recent_lows[i-1] and
                recent_lows[i] <= recent_lows[i-2] and
                recent_lows[i] <= recent_lows[i+1] and
                recent_lows[i] <= recent_lows[i+2]):
                swing_lows.append(recent_lows[i])

        # Cluster nearby levels
        tolerance = self.touch_tolerance_pct / 100.0

        def cluster_levels(levels: list[float], tol: float) -> list[dict]:
            if not levels:
                return []
            levels = sorted(levels)
            clusters = []
            current_cluster = [levels[0]]

            for i in range(1, len(levels)):
                if abs(levels[i] - levels[i-1]) / levels[i-1] < tol:
                    current_cluster.append(levels[i])
                else:
                    if len(current_cluster) >= self.min_bounces:
                        clusters.append({
                            "level": sum(current_cluster) / len(current_cluster),
                            "touches": len(current_cluster),
                        })
                    current_cluster = [levels[i]]

            if len(current_cluster) >= self.min_bounces:
                clusters.append({
                    "level": sum(current_cluster) / len(current_cluster),
                    "touches": len(current_cluster),
                })

            return clusters

        support_levels = cluster_levels(swing_lows, tolerance)
        resistance_levels = cluster_levels(swing_highs, tolerance)

        return {"support": support_levels, "resistance": resistance_levels}

    def _detect_candle_at_level(
        self, opens, highs, lows, closes, level: float, is_support: bool
    ) -> tuple[bool, str]:
        """
        Detect bullish/bearish candle at support/resistance level.
        Returns (confirmed, pattern_name).
        """
        n = len(closes)
        if n < 3:
            return False, ""

        o, h, l, c = opens[-1], highs[-1], lows[-1], closes[-1]
        body = abs(c - o)
        candle_range = h - l
        if candle_range == 0:
            return False, ""

        lower_wick = min(o, c) - l
        upper_wick = h - max(o, c)

        if is_support:
            # Bullish confirmation at support
            # Hammer: long lower wick, small body, near support
            if lower_wick > body * 2 and upper_wick < body:
                return True, "Hammer"
            # Bullish Engulfing
            if n >= 2:
                prev_o, prev_c = opens[-2], closes[-2]
                if c > o and prev_c < prev_o and c > prev_o and o < prev_c:
                    return True, "Bullish Engulfing"
            # Green candle near support
            if c > o and (c - o) / candle_range > 0.5:
                return True, "Green Candle"
        else:
            # Bearish confirmation at resistance
            # Shooting Star: long upper wick, small body, near resistance
            if upper_wick > body * 2 and lower_wick < body:
                return True, "Shooting Star"
            # Bearish Engulfing
            if n >= 2:
                prev_o, prev_c = opens[-2], closes[-2]
                if c < o and prev_c > prev_o and c < prev_o and o > prev_c:
                    return True, "Bearish Engulfing"
            # Red candle near resistance
            if c < o and (o - c) / candle_range > 0.5:
                return True, "Red Candle"

        return False, ""

    def scan(
        self,
        symbol: str,
        opens: list[float],
        highs: list[float],
        lows: list[float],
        closes: list[float],
        volumes: list[float],
        timeframe: str = "15min",
    ) -> list[Signal]:
        signals: list[Signal] = []

        if symbol not in self.INDEX_SYMBOLS:
            return signals

        n = len(closes)
        if n < 50:
            return signals

        current_close = closes[-1]
        current_volume = volumes[-1]

        # --- Step 1: Check if market is choppy ---
        adx = self._calc_adx(highs, lows, closes)
        if adx >= self.adx_max:
            return signals  # Market is trending, not choppy

        # --- Step 2: Find support and resistance levels ---
        sr_levels = self._find_support_resistance(highs, lows, closes)

        if not sr_levels["support"] or not sr_levels["resistance"]:
            return signals

        # --- Step 3: Check proximity to S/R levels ---
        atr_vals = _atr(highs, lows, closes)
        atr = atr_vals[-1] if atr_vals[-1] else current_close * 0.01

        avg_volume = sum(volumes[-20:]) / min(20, n)
        vol_ratio = current_volume / max(avg_volume, 1)

        # Find nearest support below current price
        nearest_support = None
        for s_entry in sr_levels["support"]:
            s = s_entry["level"]
            if s < current_close:
                dist_pct = (current_close - s) / current_close * 100
                if dist_pct < self.touch_tolerance_pct:  # Near support
                    nearest_support = s
                    break

        # Find nearest resistance above current price
        nearest_resistance = None
        for r_entry in sr_levels["resistance"]:
            r = r_entry["level"]
            if r > current_close:
                dist_pct = (r - current_close) / current_close * 100
                if dist_pct < self.touch_tolerance_pct:  # Near resistance
                    nearest_resistance = r
                    break

        # --- Step 4: Generate signals ---

        # BUY at support with bullish confirmation
        if nearest_support is not None:
            confirmed, pattern = self._detect_candle_at_level(
                opens, highs, lows, closes, nearest_support, is_support=True
            )

            if confirmed and vol_ratio >= self.volume_mult:
                # Target: nearest resistance
                target = nearest_resistance if nearest_resistance else current_close + atr * 3
                sl = nearest_support - atr * 0.5

                # Apply cap if set
                if self.target_pct:
                    target = _short_target(current_close, target, is_buy=True, pct=self.target_pct)

                sl_distance = current_close - sl
                target_distance = target - current_close
                rr = target_distance / max(sl_distance, 0.01)

                if rr < self.min_rr:
                    return signals

                confidence = min(1.0, 0.55 + (25 - adx) / 50 * 0.2 + (vol_ratio - 1.0) * 0.1)

                # Find resistance for target
                resistance_level = nearest_resistance if nearest_resistance else current_close + atr * 3

                strike_info = suggest_option_strike(
                    symbol, current_close, SignalType.BUY, confidence, sl, target
                )

                signals.append(Signal(
                    symbol=symbol,
                    strategy=StrategyName.INDEX_SUPPORT_RESISTANCE,
                    signal_type=SignalType.BUY,
                    price=current_close,
                    stop_loss=round(sl, 2),
                    target=round(target, 2),
                    confidence=round(confidence, 2),
                    reason=f"Choppy market (ADX {adx:.0f}) | Support {nearest_support:.2f} | Pattern: {pattern} | Target: {resistance_level:.2f} | R:R 1:{rr:.1f} | {strike_info['option_type']} {strike_info['suggested_strike']}",
                    timeframe=timeframe,
                    details={
                        "adx": round(adx, 1),
                        "support_level": round(nearest_support, 2),
                        "resistance_level": round(resistance_level, 2),
                        "candle_pattern": pattern,
                        "volume_ratio": round(vol_ratio, 2),
                        "rr": round(rr, 2),
                        "option_strike": strike_info,
                    },
                ))

        # SELL at resistance with bearish confirmation
        if nearest_resistance is not None:
            confirmed, pattern = self._detect_candle_at_level(
                opens, highs, lows, closes, nearest_resistance, is_support=False
            )

            if confirmed and vol_ratio >= self.volume_mult:
                target = nearest_support if nearest_support else current_close - atr * 3
                sl = nearest_resistance + atr * 0.5

                if self.target_pct:
                    target = _short_target(current_close, target, is_buy=False, pct=self.target_pct)

                sl_distance = sl - current_close
                target_distance = current_close - target
                rr = target_distance / max(sl_distance, 0.01)

                if rr < self.min_rr:
                    return signals

                confidence = min(1.0, 0.55 + (25 - adx) / 50 * 0.2 + (vol_ratio - 1.0) * 0.1)

                support_level = nearest_support if nearest_support else current_close - atr * 3

                strike_info = suggest_option_strike(
                    symbol, current_close, SignalType.SELL, confidence, sl, target
                )

                signals.append(Signal(
                    symbol=symbol,
                    strategy=StrategyName.INDEX_SUPPORT_RESISTANCE,
                    signal_type=SignalType.SELL,
                    price=current_close,
                    stop_loss=round(sl, 2),
                    target=round(target, 2),
                    confidence=round(confidence, 2),
                    reason=f"Choppy market (ADX {adx:.0f}) | Resistance {nearest_resistance:.2f} | Pattern: {pattern} | Target: {support_level:.2f} | R:R 1:{rr:.1f} | {strike_info['option_type']} {strike_info['suggested_strike']}",
                    timeframe=timeframe,
                    details={
                        "adx": round(adx, 1),
                        "support_level": round(support_level, 2),
                        "resistance_level": round(nearest_resistance, 2),
                        "candle_pattern": pattern,
                        "volume_ratio": round(vol_ratio, 2),
                        "rr": round(rr, 2),
                        "option_strike": strike_info,
                    },
                ))

        return signals


# ---------------------------------------------------------------------------
# Strategy: Momentum Breakout with Confirmation (options momentum rider)
# ---------------------------------------------------------------------------
class MomentumBreakout:
    """
    Momentum Breakout with Confirmation — momentum-rider option setup.

    Logic (designed for 5-min / 15-min intraday; also runs on daily):
    1. PRIMARY TREND  : 20 EMA > 50 EMA = uptrend, 20 EMA < 50 EMA = downtrend
    2. STRONG MOVE    : price moved strongly in the trend direction BEFORE the
       consolidation (>= `strong_atr_mult` ATRs) — this is the momentum fuel
    3. CONSOLIDATION  : the last `consol_bars` (3-5) bars trade sideways in a
       tight range (<= `consol_atr_pct` * ATR) — energy builds for the breakout
    4. BREAKOUT+VOL   : current bar closes above the consolidation high (BUY)
       or below the consolidation low (SELL) with volume >= `vol_mult` x average
    5. VWAP CONFIRM   : BUY only if price is ABOVE VWAP, SELL only if BELOW
    6. OPTION STRIKE  : suggests the ATM option strike via suggest_option_strike

    SL = below the breakout candle low (longs) / above the high (shorts).
    Targets at 1:2 (TP1) and 1:3 (TP2) risk-reward.
    """

    def __init__(
        self,
        ema_fast: int = 20,
        ema_slow: int = 50,
        consol_bars: int = 4,
        consol_atr_pct: float = 1.5,
        consol_vs_move: float = 0.6,
        strong_atr_mult: float = 1.5,
        vol_mult: float = 1.5,
        rr_target1: float = 2.0,
        rr_target2: float = 3.0,
        min_rr: float = 1.5,
        atr_period: int = 14,
    ):
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.consol_bars = consol_bars
        self.consol_atr_pct = consol_atr_pct
        self.consol_vs_move = consol_vs_move
        self.strong_atr_mult = strong_atr_mult
        self.vol_mult = vol_mult
        self.rr_target1 = rr_target1
        self.rr_target2 = rr_target2
        self.min_rr = min_rr
        self.atr_period = atr_period

    def scan(
        self,
        symbol: str,
        opens: list[float],
        highs: list[float],
        lows: list[float],
        closes: list[float],
        volumes: list[float],
        timeframe: str = "5min",
    ) -> list[Signal]:
        signals: list[Signal] = []
        n = len(closes)
        need = self.ema_slow + self.consol_bars + 8
        if n < need:
            return signals

        ema_fast_vals = _ema(closes, self.ema_fast)
        ema_slow_vals = _ema(closes, self.ema_slow)
        vwap_vals = _vwap(highs, lows, closes, volumes)
        atr_vals = _atr(highs, lows, closes, self.atr_period)

        ema_f = ema_fast_vals[-1] if ema_fast_vals[-1] else 0
        ema_s = ema_slow_vals[-1] if ema_slow_vals[-1] else 0
        vwap = vwap_vals[-1] if vwap_vals[-1] else 0
        atr = atr_vals[-1] if atr_vals[-1] else closes[-1] * 0.01

        if atr <= 0 or vwap <= 0:
            return signals

        uptrend = ema_f > ema_s
        downtrend = ema_f < ema_s

        # --- Consolidation window (bars BEFORE the breakout bar) ---
        cb = self.consol_bars
        start = -(cb + 1)
        end = -1  # exclude current bar
        consol_high = max(highs[start:end])
        consol_low = min(lows[start:end])
        consol_range = consol_high - consol_low

        # --- Strong prior move in the trend direction (momentum fuel) ---
        # Measured over the 8 bars strictly BEFORE the consolidation window
        pre_start = -(cb + 1 + 8)
        pre_end = -(cb + 2)
        prior_move = closes[pre_end] - closes[pre_start]
        if uptrend and prior_move < atr * self.strong_atr_mult:
            return signals
        if downtrend and -prior_move < atr * self.strong_atr_mult:
            return signals

        # Consolidation must be a pause: range small relative to ATR AND to the
        # prior move that built the momentum
        if consol_range > atr * self.consol_atr_pct:
            return signals
        if consol_range > abs(prior_move) * self.consol_vs_move:
            return signals

        current_close = closes[-1]
        current_volume = volumes[-1]
        avg_volume = sum(volumes[-20:]) / 20
        vol_ratio = current_volume / max(avg_volume, 1)

        volume_ok = vol_ratio >= self.vol_mult

        # --- Breakout + VWAP confirmation ---
        bull_breakout = uptrend and current_close > consol_high and current_close > vwap and volume_ok
        bear_breakout = downtrend and current_close < consol_low and current_close < vwap and volume_ok

        if not (bull_breakout or bear_breakout):
            return signals

        is_buy = bull_breakout

        if is_buy:
            sl = min(lows[-1], consol_low) - atr * 0.15
            risk = current_close - sl
            if risk <= 0:
                return signals
            tp1 = current_close + risk * self.rr_target1
            tp2 = current_close + risk * self.rr_target2
            rr1 = (tp1 - current_close) / risk
            if rr1 < self.min_rr:
                return signals
            tp1 = _short_target(current_close, tp1, is_buy=True)
            tp2 = _short_target(current_close, tp2, is_buy=True)
            sl = round(sl, 2)
            tp1 = round(tp1, 2)
            tp2 = round(tp2, 2)
        else:
            sl = max(highs[-1], consol_high) + atr * 0.15
            risk = sl - current_close
            if risk <= 0:
                return signals
            tp1 = current_close - risk * self.rr_target1
            tp2 = current_close - risk * self.rr_target2
            rr1 = (current_close - tp1) / risk
            if rr1 < self.min_rr:
                return signals
            tp1 = _short_target(current_close, tp1, is_buy=False)
            tp2 = _short_target(current_close, tp2, is_buy=False)
            sl = round(sl, 2)
            tp1 = round(tp1, 2)
            tp2 = round(tp2, 2)

        confidence = min(1.0, 0.5
                         + (vol_ratio >= 2.0) * 0.1
                         + (consol_range <= atr * 0.8) * 0.1
                         + (uptrend and current_close > ema_s * 1.005 or
                            downtrend and current_close < ema_s * 0.995) * 0.1)
        confidence = round(confidence, 2)

        strike_info = suggest_option_strike(
            symbol, current_close, SignalType.BUY if is_buy else SignalType.SELL,
            confidence, sl, tp2,
        )
        is_index = strike_info.get("option_type", "N/A") != "N/A"

        if is_buy:
            reason = (f"Momentum breakout above consolidation {consol_high:.2f} | "
                      f"Trend: EMA{self.ema_fast}>{self.ema_slow} (bull) | Price above VWAP ({vwap:.2f}) | "
                      f"Consolidated {cb} bars ({consol_low:.2f}-{consol_high:.2f}) after a {prior_move:.2f} move | "
                      f"{vol_ratio:.1f}x volume | TP1 1:{self.rr_target1:.0f} @ {tp1}, TP2 1:{self.rr_target2:.0f} @ {tp2}")
        else:
            reason = (f"Momentum breakdown below consolidation {consol_low:.2f} | "
                      f"Trend: EMA{self.ema_fast}<{self.ema_slow} (bear) | Price below VWAP ({vwap:.2f}) | "
                      f"Consolidated {cb} bars ({consol_low:.2f}-{consol_high:.2f}) after a {-prior_move:.2f} move | "
                      f"{vol_ratio:.1f}x volume | TP1 1:{self.rr_target1:.0f} @ {tp1}, TP2 1:{self.rr_target2:.0f} @ {tp2}")
        if is_index:
            reason += f" | {strike_info['option_type']} {strike_info['suggested_strike']}"

        signals.append(Signal(
            symbol=symbol,
            strategy=StrategyName.MOMENTUM_BREAKOUT,
            signal_type=SignalType.BUY if is_buy else SignalType.SELL,
            price=round(current_close, 2),
            stop_loss=sl,
            target=tp1,
            confidence=confidence,
            reason=reason,
            timeframe=timeframe,
            details={
                "trend": "UPTREND" if is_buy else "DOWNTREND",
                "ema_fast": round(ema_f, 2),
                "ema_slow": round(ema_s, 2),
                "vwap": round(vwap, 2),
                "vwap_ok": True,
                "consol_high": round(consol_high, 2),
                "consol_low": round(consol_low, 2),
                "consol_bars": cb,
                "consol_range": round(consol_range, 2),
                "prior_move": round(prior_move, 2),
                "volume_ratio": round(vol_ratio, 2),
                "atr": round(atr, 2),
                "rr": round(rr1, 2),
                "tp1": tp1,
                "tp2": tp2,
                "option_strike": strike_info,
            },
        ))

        return signals
