"""
Candlestick Pattern Scanner
============================
Detects all major candlestick patterns for buy/sell signals.

Single Candle: Doji, Hammer, Inverted Hammer, Marubozu
Double Candle: Engulfing, Piercing, Dark Cloud, Tweezer
Triple Candle: Morning Star, Evening Star, Three Soldiers, Three Crows
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional
import math


class PatternType(Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"


class PatternName(Enum):
    # Single Candle
    DOJI = "Doji"
    HAMMER = "Hammer"
    INVERTED_HAMMER = "Inverted Hammer"
    MARUBOZU = "Marubozu"
    SPINNING_TOP = "Spinning Top"
    
    # Double Candle
    BULLISH_ENGULFING = "Bullish Engulfing"
    BEARISH_ENGULFING = "Bearish Engulfing"
    PIERCING_LINE = "Piercing Line"
    DARK_CLOUD = "Dark Cloud Cover"
    TWEEZER_BOTTOM = "Tweezer Bottom"
    TWEEZER_TOP = "Tweezer Top"
    
    # Triple Candle
    MORNING_STAR = "Morning Star"
    EVENING_STAR = "Evening Star"
    THREE_WHITE_SOLDIERS = "Three White Soldiers"
    THREE_BLACK_CROWS = "Three Black Crows"
    RISING_THREE = "Rising Three Methods"
    FALLING_THREE = "Falling Three Methods"


@dataclass
class CandlestickSignal:
    """A detected candlestick pattern signal."""
    pattern: PatternName
    pattern_type: PatternType
    price: float
    confidence: float  # 0.0 - 1.0
    reason: str
    details: dict


class CandlestickScanner:
    """Scans for candlestick patterns in OHLCV data.

    Filters (per user theory — candles spot EARLY range breakouts):
    - breakout_context : only keep patterns when price is near the recent range
      high (BUY) or range low (SELL) — i.e., an early-breakout setup.
    - min_confidence   : drop weak/indecision patterns (Doji, Spinning Top...).
    - buy_biased       : drop SELL patterns unless they are STRONG reversals
      (confidence >= strong_sell_min) — a weak candle must not be a sell idea.
    """

    def __init__(
        self,
        min_confidence: float = 0.6,
        buy_biased: bool = True,
        strong_sell_min: float = 0.7,
        breakout_context: bool = True,
        context_lookback: int = 15,
        context_proximity_pct: float = 2.5,
    ):
        self.min_confidence = min_confidence
        self.buy_biased = buy_biased
        self.strong_sell_min = strong_sell_min
        self.breakout_context = breakout_context
        self.context_lookback = context_lookback
        self.context_proximity_pct = context_proximity_pct

    def scan(
        self,
        opens: list[float],
        highs: list[float],
        lows: list[float],
        closes: list[float],
        volumes: list[float],
    ) -> list[CandlestickSignal]:
        """Scan the last few candles for patterns, then apply quality filters."""
        signals = []
        n = len(closes)
        if n < 5:
            return signals
        
        # Get last 3 candles for analysis
        i = n - 1  # Current candle
        prev = n - 2  # Previous candle
        prev2 = n - 3  # 2 candles ago
        
        # Candle properties
        body = closes[i] - opens[i]
        body_abs = abs(body)
        upper_wick = highs[i] - max(opens[i], closes[i])
        lower_wick = min(opens[i], closes[i]) - lows[i]
        candle_range = highs[i] - lows[i]
        
        prev_body = closes[prev] - opens[prev]
        prev_body_abs = abs(prev_body)
        prev_range = highs[prev] - lows[prev]
        
        prev2_body = closes[prev2] - opens[prev2]
        
        if candle_range == 0 or prev_range == 0:
            return signals
        
        # ============================================
        # SINGLE CANDLE PATTERNS
        # ============================================
        
        # --- Doji ---
        if body_abs <= candle_range * 0.1:
            # Dragonfly Doji (bullish at support)
            if lower_wick > body_abs * 3 and upper_wick < candle_range * 0.1:
                signals.append(CandlestickSignal(
                    pattern=PatternName.DOJI,
                    pattern_type=PatternType.BULLISH,
                    price=closes[i],
                    confidence=0.6,
                    reason=f"Dragonfly Doji - potential bullish reversal | Lower wick: {lower_wick:.2f}",
                    details={"type": "dragonfly", "body_ratio": round(body_abs/candle_range, 3)},
                ))
            # Gravestone Doji (bearish at resistance)
            elif upper_wick > body_abs * 3 and lower_wick < candle_range * 0.1:
                signals.append(CandlestickSignal(
                    pattern=PatternName.DOJI,
                    pattern_type=PatternType.BEARISH,
                    price=closes[i],
                    confidence=0.6,
                    reason=f"Gravestone Doji - potential bearish reversal | Upper wick: {upper_wick:.2f}",
                    details={"type": "gravestone", "body_ratio": round(body_abs/candle_range, 3)},
                ))
            # Regular Doji
            elif upper_wick > body_abs * 2 and lower_wick > body_abs * 2:
                signals.append(CandlestickSignal(
                    pattern=PatternName.DOJI,
                    pattern_type=PatternType.BULLISH if prev_body < 0 else PatternType.BEARISH,
                    price=closes[i],
                    confidence=0.4,
                    reason=f"Standard Doji - indecision after {'downtrend' if prev_body < 0 else 'uptrend'}",
                    details={"type": "standard"},
                ))
        
        # --- Hammer (bullish) ---
        if (lower_wick > body_abs * 2 and 
            upper_wick < candle_range * 0.1 and
            prev_body < 0 and  # Previous candle was bearish
            body_abs > 0):
            confidence = min(0.8, 0.5 + (lower_wick / candle_range) * 0.3)
            signals.append(CandlestickSignal(
                pattern=PatternName.HAMMER,
                pattern_type=PatternType.BULLISH,
                price=closes[i],
                confidence=round(confidence, 2),
                reason=f"Hammer - bullish reversal at support | Lower wick {lower_wick:.2f}x body",
                details={"wick_ratio": round(lower_wick / body_abs, 2)},
            ))
        
        # --- Inverted Hammer (bullish) ---
        if (upper_wick > body_abs * 2 and
            lower_wick < candle_range * 0.1 and
            prev_body < 0 and
            body_abs > 0):
            signals.append(CandlestickSignal(
                pattern=PatternName.INVERTED_HAMMER,
                pattern_type=PatternType.BULLISH,
                price=closes[i],
                confidence=0.55,
                reason=f"Inverted Hammer - potential reversal | Upper wick: {upper_wick:.2f}",
                details={"wick_ratio": round(upper_wick / body_abs, 2)},
            ))
        
        # --- Marubozu (strong candle) ---
        if (body_abs > candle_range * 0.9 and
            candle_range > 0):
            direction = "bullish" if body > 0 else "bearish"
            signals.append(CandlestickSignal(
                pattern=PatternName.MARUBOZU,
                pattern_type=PatternType.BULLISH if body > 0 else PatternType.BEARISH,
                price=closes[i],
                confidence=0.65,
                reason=f"{direction.title()} Marubozu - strong {direction} momentum | Body: {body_abs:.2f} ({body_abs/candle_range*100:.0f}% of range)",
                details={"body_pct": round(body_abs/candle_range*100, 1)},
            ))
        
        # --- Spinning Top ---
        if (body_abs <= candle_range * 0.3 and
            body_abs > candle_range * 0.05 and
            upper_wick > body_abs and
            lower_wick > body_abs):
            signals.append(CandlestickSignal(
                pattern=PatternName.SPINNING_TOP,
                pattern_type=PatternType.BULLISH if prev_body < 0 else PatternType.BEARISH,
                price=closes[i],
                confidence=0.4,
                reason=f"Spinning Top - indecision | Body: {body_abs:.2f}, Range: {candle_range:.2f}",
                details={"body_range_ratio": round(body_abs/candle_range, 2)},
            ))
        
        # ============================================
        # DOUBLE CANDLE PATTERNS
        # ============================================
        
        # --- Bullish Engulfing ---
        if (prev_body < 0 and  # Previous was bearish
            body > 0 and  # Current is bullish
            opens[i] <= closes[prev] and  # Opens below prev close
            closes[i] >= opens[prev]):  # Closes above prev open
            confidence = min(0.85, 0.6 + (body_abs / prev_body_abs) * 0.1)
            signals.append(CandlestickSignal(
                pattern=PatternName.BULLISH_ENGULFING,
                pattern_type=PatternType.BULLISH,
                price=closes[i],
                confidence=round(confidence, 2),
                reason=f"Bullish Engulfing - strong reversal | Current body {body_abs:.2f} engulfs prev {prev_body_abs:.2f}",
                details={"body_ratio": round(body_abs / prev_body_abs, 2)},
            ))
        
        # --- Bearish Engulfing ---
        if (prev_body > 0 and
            body < 0 and
            opens[i] >= closes[prev] and
            closes[i] <= opens[prev]):
            confidence = min(0.85, 0.6 + (body_abs / prev_body_abs) * 0.1)
            signals.append(CandlestickSignal(
                pattern=PatternName.BEARISH_ENGULFING,
                pattern_type=PatternType.BEARISH,
                price=closes[i],
                confidence=round(confidence, 2),
                reason=f"Bearish Engulfing - strong reversal | Current body {body_abs:.2f} engulfs prev {prev_body_abs:.2f}",
                details={"body_ratio": round(body_abs / prev_body_abs, 2)},
            ))
        
        # --- Piercing Line ---
        mid_prev = (opens[prev] + closes[prev]) / 2
        if (prev_body < 0 and
            body > 0 and
            opens[i] < lows[prev] and
            closes[i] > mid_prev and
            closes[i] < closes[prev]):
            signals.append(CandlestickSignal(
                pattern=PatternName.PIERCING_LINE,
                pattern_type=PatternType.BULLISH,
                price=closes[i],
                confidence=0.7,
                reason=f"Piercing Line - bullish reversal | Close {closes[i]:.2f} above 50% of prev candle ({mid_prev:.2f})",
                details={"penetration": round((closes[i] - opens[i]) / prev_body_abs * 100, 1)},
            ))
        
        # --- Dark Cloud Cover ---
        mid_prev = (opens[prev] + closes[prev]) / 2
        if (prev_body > 0 and
            body < 0 and
            opens[i] > highs[prev] and
            closes[i] < mid_prev and
            closes[i] > opens[prev]):
            signals.append(CandlestickSignal(
                pattern=PatternName.DARK_CLOUD,
                pattern_type=PatternType.BEARISH,
                price=closes[i],
                confidence=0.7,
                reason=f"Dark Cloud Cover - bearish reversal | Close {closes[i]:.2f} below 50% of prev candle ({mid_prev:.2f})",
                details={"penetration": round((opens[i] - closes[i]) / prev_body_abs * 100, 1)},
            ))
        
        # --- Tweezer Bottom ---
        if (prev_body < 0 and
            body > 0 and
            abs(lows[i] - lows[prev]) < candle_range * 0.02):
            signals.append(CandlestickSignal(
                pattern=PatternName.TWEEZER_BOTTOM,
                pattern_type=PatternType.BULLISH,
                price=closes[i],
                confidence=0.65,
                reason=f"Tweezer Bottom - support tested twice | Lows: {lows[i]:.2f} ≈ {lows[prev]:.2f}",
                details={"low_diff": round(abs(lows[i] - lows[prev]), 2)},
            ))
        
        # --- Tweezer Top ---
        if (prev_body > 0 and
            body < 0 and
            abs(highs[i] - highs[prev]) < candle_range * 0.02):
            signals.append(CandlestickSignal(
                pattern=PatternName.TWEEZER_TOP,
                pattern_type=PatternType.BEARISH,
                price=closes[i],
                confidence=0.65,
                reason=f"Tweezer Top - resistance tested twice | Highs: {highs[i]:.2f} ≈ {highs[prev]:.2f}",
                details={"high_diff": round(abs(highs[i] - highs[prev]), 2)},
            ))
        
        # ============================================
        # TRIPLE CANDLE PATTERNS
        # ============================================
        
        # --- Morning Star ---
        if (prev_body < 0 and
            prev2_body < 0 and  # Two bearish candles
            abs(closes[prev] - opens[prev]) < prev_range * 0.3 and  # Middle is small
            body > 0 and
            closes[i] > (opens[prev2] + closes[prev2]) / 2):
            signals.append(CandlestickSignal(
                pattern=PatternName.MORNING_STAR,
                pattern_type=PatternType.BULLISH,
                price=closes[i],
                confidence=0.8,
                reason=f"Morning Star - strong bullish reversal | 3-candle pattern",
                details={},
            ))
        
        # --- Evening Star ---
        if (prev_body > 0 and
            prev2_body > 0 and
            abs(closes[prev] - opens[prev]) < prev_range * 0.3 and
            body < 0 and
            closes[i] < (opens[prev2] + closes[prev2]) / 2):
            signals.append(CandlestickSignal(
                pattern=PatternName.EVENING_STAR,
                pattern_type=PatternType.BEARISH,
                price=closes[i],
                confidence=0.8,
                reason=f"Evening Star - strong bearish reversal | 3-candle pattern",
                details={},
            ))
        
        # --- Three White Soldiers ---
        if (prev2_body > 0 and prev_body > 0 and body > 0 and
            closes[prev2] > opens[prev2] and
            closes[prev] > opens[prev] and
            closes[i] > opens[i] and
            opens[prev] > opens[prev2] and
            opens[i] > opens[prev]):
            signals.append(CandlestickSignal(
                pattern=PatternName.THREE_WHITE_SOLDIERS,
                pattern_type=PatternType.BULLISH,
                price=closes[i],
                confidence=0.75,
                reason=f"Three White Soldiers - strong bullish momentum | 3 consecutive green candles",
                details={},
            ))
        
        # --- Three Black Crows ---
        if (prev2_body < 0 and prev_body < 0 and body < 0 and
            closes[prev2] < opens[prev2] and
            closes[prev] < opens[prev] and
            closes[i] < opens[i] and
            opens[prev] < opens[prev2] and
            opens[i] < opens[prev]):
            signals.append(CandlestickSignal(
                pattern=PatternName.THREE_BLACK_CROWS,
                pattern_type=PatternType.BEARISH,
                price=closes[i],
                confidence=0.75,
                reason=f"Three Black Crows - strong bearish momentum | 3 consecutive red candles",
                details={},
            ))

        # ============================================
        # QUALITY FILTERS
        # ============================================
        # 1. Suppress low-confidence / indecision patterns
        signals = [s for s in signals if s.confidence >= self.min_confidence]

        # 2. Buy-bias: weak candles must not be a sell idea.
        #    SELL only kept when it is a STRONG bearish reversal.
        if self.buy_biased:
            signals = [
                s for s in signals
                if s.pattern_type != PatternType.BEARISH or s.confidence >= self.strong_sell_min
            ]

        # 3. Early-breakout context: pattern must occur near the recent range
        #    high (BUY) or range low (SELL), where a breakout is forming.
        if self.breakout_context and len(closes) >= self.context_lookback + 1:
            recent_high = max(highs[-(self.context_lookback + 1):-1])
            recent_low = min(lows[-(self.context_lookback + 1):-1])
            prox = self.context_proximity_pct / 100.0
            kept = []
            for s in signals:
                px = s.price
                if s.pattern_type == PatternType.BULLISH:
                    if px >= recent_high * (1 - prox):
                        kept.append(s)
                else:  # BEARISH
                    if px <= recent_low * (1 + prox):
                        kept.append(s)
            signals = kept

        return signals


# Convenience function for scanner engine
def scan_candlesticks(
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    volumes: list[float],
    **kwargs,
) -> list[CandlestickSignal]:
    """Quick function to scan for candlestick patterns."""
    scanner = CandlestickScanner(**kwargs)
    return scanner.scan(opens, highs, lows, closes, volumes)
