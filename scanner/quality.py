"""
Signal Quality Scoring — evaluates each signal on a 0-100 scale.
================================================================

Factors (max 100 points):
  1. STRATEGY CONFLUENCE   (0-20 pts) — How many strategies agree
  2. TREND ALIGNMENT       (0-15 pts) — EMA50 slope, price vs EMAs
  3. VOLUME CONFIRMATION   (0-15 pts) — Volume ratio, trend, accumulation
  4. MOMENTUM              (0-10 pts) — RSI, MACD histogram
  5. RISK:REWARD           (0-15 pts) — Target vs stop loss ratio
  6. SUPPORT QUALITY       (0-15 pts) — How strong the support level is
  7. PATTERN CONFIRMATION  (0-10 pts) — Candlestick, higher lows, etc.

Quality Tiers:
  80-100  VERY HIGH  — Strong confluence, all confirmations align
  60-79   HIGH       — Most confirmations, good setup
  40-59   MODERATE   — Some confirmations, needs monitoring
  20-39   LOW        — Few confirmations, high risk
  0-19    VERY LOW   — No confirmations, avoid
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class QualityScore:
    total: int              # 0-100
    tier: str               # VERY HIGH, HIGH, MODERATE, LOW, VERY LOW
    confluence: int         # 0-20
    trend: int              # 0-15
    volume: int             # 0-15
    momentum: int           # 0-10
    risk_reward: int        # 0-15
    support: int            # 0-15
    pattern: int            # 0-10
    breakdown: dict         # Detailed breakdown for UI


def score_signal(
    signal_dict: dict,
    all_signals_for_symbol: list[dict] | None = None,
    candles: list | None = None,
) -> QualityScore:
    """
    Score a signal from 0-100 based on multiple confirmation factors.

    signal_dict should have: symbol, strategy, signal_type, price, stop_loss,
    target, confidence, reason, timeframe, details, strategies (list).
    """
    confluence = _score_confluence(signal_dict, all_signals_for_symbol)
    trend = _score_trend(signal_dict, candles)
    volume = _score_volume(signal_dict, candles)
    momentum = _score_momentum(signal_dict, candles)
    risk_reward = _score_risk_reward(signal_dict)
    support = _score_support(signal_dict)
    pattern = _score_pattern(signal_dict)

    total = confluence + trend + volume + momentum + risk_reward + support + pattern
    total = min(100, max(0, total))

    tier = _tier(total)

    breakdown = {
        "confluence": {"score": confluence, "max": 20, "label": "Strategy Confluence"},
        "trend": {"score": trend, "max": 15, "label": "Trend Alignment"},
        "volume": {"score": volume, "max": 15, "label": "Volume"},
        "momentum": {"score": momentum, "max": 10, "label": "Momentum"},
        "risk_reward": {"score": risk_reward, "max": 15, "label": "Risk:Reward"},
        "support": {"score": support, "max": 15, "label": "Support Quality"},
        "pattern": {"score": pattern, "max": 10, "label": "Pattern"},
    }

    return QualityScore(
        total=total, tier=tier,
        confluence=confluence, trend=trend, volume=volume,
        momentum=momentum, risk_reward=risk_reward,
        support=support, pattern=pattern,
        breakdown=breakdown,
    )


# ---------------------------------------------------------------------------
# Individual scoring functions
# ---------------------------------------------------------------------------

def _score_confluence(signal: dict, all_signals: list[dict] | None) -> int:
    """How many strategies agree on this symbol?"""
    strategies = signal.get("strategies", [])
    if not strategies and signal.get("strategy"):
        strategies = [signal["strategy"]]
    n = len(strategies)

    if n >= 4:
        return 20  # Very strong confluence
    elif n == 3:
        return 16
    elif n == 2:
        return 12
    elif n == 1:
        return 6
    return 0


def _score_trend(signal: dict, candles: list | None) -> int:
    """Trend alignment: EMA50 slope, price vs EMAs, market regime."""
    details = signal.get("details", {})
    score = 0

    # EMA50 slope
    ema50_slope = details.get("ema50_slope")
    if ema50_slope is not None:
        if signal.get("signal_type") == "BUY":
            if ema50_slope > 0.5:
                score += 8  # Strong uptrend
            elif ema50_slope > 0.1:
                score += 5  # Gentle uptrend
            elif ema50_slope > 0:
                score += 3  # Flat to slight up
        else:  # SELL
            if ema50_slope < -0.5:
                score += 8
            elif ema50_slope < -0.1:
                score += 5
            elif ema50_slope < 0:
                score += 3

    # Price vs EMA50
    ema50 = details.get("ema50")
    price = signal.get("price", 0)
    if ema50 and price:
        if signal.get("signal_type") == "BUY" and price > ema50:
            score += 4
        elif signal.get("signal_type") == "SELL" and price < ema50:
            score += 4

    # Trend from details
    trend = details.get("trend", "")
    if signal.get("signal_type") == "BUY" and "UPTREND" in trend.upper():
        score += 3
    elif signal.get("signal_type") == "SELL" and "DOWNTREND" in trend.upper():
        score += 3

    return min(15, score)


def _score_volume(signal: dict, candles: list | None) -> int:
    """Volume confirmation: ratio, trend, accumulation."""
    details = signal.get("details", {})
    score = 0

    vol_ratio = details.get("volume_ratio", 0)

    # Volume ratio
    if vol_ratio >= 2.0:
        score += 8  # High conviction
    elif vol_ratio >= 1.5:
        score += 6  # Good volume
    elif vol_ratio >= 1.0:
        score += 4  # Average
    elif vol_ratio >= 0.8:
        score += 2  # Slightly low
    # Below 0.8 = 0 points

    # Volume trend rising
    if details.get("vol_trend_rising"):
        score += 4

    # Accumulation (higher lows)
    higher_lows = details.get("higher_lows_count", 0)
    if higher_lows >= 5:
        score += 3
    elif higher_lows >= 3:
        score += 2

    return min(15, score)


def _score_momentum(signal: dict, candles: list | None) -> int:
    """Momentum: RSI, MACD histogram."""
    details = signal.get("details", {})
    score = 0

    # RSI
    rsi = details.get("rsi")
    if rsi is not None:
        if signal.get("signal_type") == "BUY":
            if 55 <= rsi <= 70:
                score += 5  # Sweet spot
            elif 45 <= rsi < 55:
                score += 3  # OK
            elif rsi > 70:
                score += 1  # Overbought risk
        else:  # SELL
            if 30 <= rsi <= 45:
                score += 5
            elif 45 < rsi <= 55:
                score += 3
            elif rsi < 30:
                score += 1

    # MACD histogram
    macd_hist = details.get("macd_histogram")
    if macd_hist is not None:
        if signal.get("signal_type") == "BUY" and macd_hist > 0:
            score += 5
        elif signal.get("signal_type") == "SELL" and macd_hist < 0:
            score += 5
        elif abs(macd_hist) < 0.1:
            score += 2  # Neutral

    return min(10, score)


def _score_risk_reward(signal: dict) -> int:
    """Risk:Reward ratio quality."""
    price = signal.get("price", 0)
    stop_loss = signal.get("stop_loss", 0)
    target = signal.get("target", 0)

    if not price or not stop_loss or not target:
        return 5  # Default if unknown

    risk = abs(price - stop_loss)
    reward = abs(target - price)

    if risk <= 0:
        return 0

    rr = reward / risk

    if rr >= 3.0:
        return 15  # Excellent R:R
    elif rr >= 2.5:
        return 13
    elif rr >= 2.0:
        return 11
    elif rr >= 1.5:
        return 9
    elif rr >= 1.0:
        return 6
    elif rr >= 0.5:
        return 3
    return 0


def _score_support(signal: dict) -> int:
    """Support level quality: breakout retest > EMA confluence > single EMA."""
    details = signal.get("details", {})
    score = 0

    support_type = details.get("support_type", [])
    if isinstance(support_type, str):
        support_type = [support_type]

    # Breakout retest is strongest
    for s in support_type:
        if "breakout retest" in s.lower():
            score += 7
        elif "50 ema" in s.lower() or "ema50" in s.lower():
            score += 4
        elif "20 ema" in s.lower() or "ema20" in s.lower():
            score += 3

    # Multiple support levels
    if len(support_type) >= 2:
        score += 5  # Confluence of support
    elif len(support_type) == 1:
        score += 3

    # Confirmed signal
    if details.get("confirmed"):
        score += 3

    # ADX trend strength
    adx = details.get("adx")
    if adx is not None:
        if adx > 30:
            score += 2  # Strong trend
        elif adx > 20:
            score += 1  # Moderate trend

    return min(15, score)


def _score_pattern(signal: dict) -> int:
    """Pattern confirmation: candlestick, accumulation, proximity."""
    details = signal.get("details", {})
    score = 0

    # 52W high proximity
    dist = details.get("dist_from_high_pct")
    if dist is not None:
        if dist < 2:
            score += 4  # Very close
        elif dist < 5:
            score += 3
        elif dist < 8:
            score += 2

    # Higher lows (accumulation)
    hl = details.get("higher_lows_count", 0)
    if hl >= 5:
        score += 3
    elif hl >= 3:
        score += 2

    # Confirmed signal tag
    conf_rules = details.get("conf_rules", [])
    if len(conf_rules) >= 3:
        score += 3
    elif len(conf_rules) >= 2:
        score += 2
    elif len(conf_rules) >= 1:
        score += 1

    return min(10, score)


def _tier(score: int) -> str:
    if score >= 80:
        return "VERY HIGH"
    elif score >= 60:
        return "HIGH"
    elif score >= 40:
        return "MODERATE"
    elif score >= 20:
        return "LOW"
    return "VERY LOW"


def tier_color(tier: str) -> str:
    return {
        "VERY HIGH": "#22c55e",
        "HIGH": "#4ade80",
        "MODERATE": "#eab308",
        "LOW": "#f97316",
        "VERY LOW": "#ef4444",
    }.get(tier, "#94a3b8")


def tier_emoji(tier: str) -> str:
    return {
        "VERY HIGH": "🔥",
        "HIGH": "✅",
        "MODERATE": "⚡",
        "LOW": "⚠️",
        "VERY LOW": "🚫",
    }.get(tier, "")
