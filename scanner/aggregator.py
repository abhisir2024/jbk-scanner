"""
Signal Aggregator — groups signals by stock and assigns strength levels.

Signal Strength Logic:
- 1 strategy agrees → BUY / SELL (normal)
- 2 strategies agree → STRONG BUY / STRONG SELL
- 3+ strategies agree → VERY STRONG BUY / VERY STRONG SELL

Confluence is determined by:
- Same symbol + same signal direction (BUY or SELL)
- From different strategies (not same strategy on different timeframes)
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from scanner.strategies import Signal, SignalType


class SignalStrength(Enum):
    BUY = "BUY"
    STRONG_BUY = "STRONG BUY"
    VERY_STRONG_BUY = "VERY STRONG BUY"
    SELL = "SELL"
    STRONG_SELL = "STRONG SELL"
    VERY_STRONG_SELL = "VERY STRONG SELL"


STRENGTH_MAP = {
    SignalType.BUY: {
        1: SignalStrength.BUY,
        2: SignalStrength.STRONG_BUY,
        3: SignalStrength.VERY_STRONG_BUY,
    },
    SignalType.SELL: {
        1: SignalStrength.SELL,
        2: SignalStrength.STRONG_SELL,
        3: SignalStrength.VERY_STRONG_SELL,
    },
}

STRENGTH_EMOJI = {
    SignalStrength.BUY: "🟢",
    SignalStrength.STRONG_BUY: "🟢🟢",
    SignalStrength.VERY_STRONG_BUY: "🟢🟢🟢",
    SignalStrength.SELL: "🔴",
    SignalStrength.STRONG_SELL: "🔴🔴",
    SignalStrength.VERY_STRONG_SELL: "🔴🔴🔴",
}

STRENGTH_COLOR = {
    SignalStrength.BUY: "#22c55e",
    SignalStrength.STRONG_BUY: "#16a34a",
    SignalStrength.VERY_STRONG_BUY: "#15803d",
    SignalStrength.SELL: "#ef4444",
    SignalStrength.STRONG_SELL: "#dc2626",
    SignalStrength.VERY_STRONG_SELL: "#b91c1c",
}


@dataclass
class AggregatedSignal:
    """A signal with confluence from multiple strategies."""
    symbol: str
    symbol_name: str
    strength: SignalStrength
    signal_type: SignalType
    strategies: list[str]  # list of strategy names that agreed
    strategy_count: int
    price: float
    stop_loss: float
    target: float
    confidence: float  # averaged across strategies
    reasons: list[str]  # one reason per strategy
    timeframe: str
    details: dict = field(default_factory=dict)


def aggregate_signals(signals: list[Signal]) -> list[AggregatedSignal]:
    """
    Group signals by symbol+direction, then rank by confluence count.
    Returns sorted list: strongest signals first.
    """
    from scanner.universe import get_symbol_name

    # Group by (symbol, direction)
    groups: dict[tuple, list[Signal]] = {}
    for s in signals:
        key = (s.symbol, s.signal_type)
        groups.setdefault(key, []).append(s)

    aggregated: list[AggregatedSignal] = []

    for (symbol, direction), group_signals in groups.items():
        # Deduplicate strategies — keep the best signal per strategy name
        seen_strategies: dict[str, Signal] = {}
        for s in group_signals:
            strategy_key = s.strategy.value
            if strategy_key not in seen_strategies or s.confidence > seen_strategies[strategy_key].confidence:
                seen_strategies[strategy_key] = s

        unique_signals = list(seen_strategies.values())
        count = len(unique_signals)

        # Determine strength
        strength_options = STRENGTH_MAP[direction]
        if count >= 3:
            strength = strength_options[3]
        elif count >= 2:
            strength = strength_options[2]
        else:
            strength = strength_options[1]

        # Aggregate metrics
        avg_confidence = sum(s.confidence for s in unique_signals) / count
        # Best stop loss: most protective (highest for BUY, lowest for SELL)
        if direction == SignalType.BUY:
            best_sl = max(s.stop_loss for s in unique_signals)
            best_target = max(s.target for s in unique_signals)
        else:
            best_sl = min(s.stop_loss for s in unique_signals)
            best_target = min(s.target for s in unique_signals)

        # Use the most recent timeframe
        timeframe_priority = {"daily": 0, "15min": 1, "5min": 2}
        best_tf = min(unique_signals, key=lambda s: timeframe_priority.get(s.timeframe, 3)).timeframe

        # Confirmation status: a group is confirmed if ANY constituent signal
        # passed all confirmation rules (volume gate + hold + pullback).
        confirmed = any(s.details.get("confirmed") for s in unique_signals)
        conf_rules = []
        for s in unique_signals:
            conf_rules += [r for r in s.details.get("conf_rules", []) if r not in conf_rules]

        aggregated.append(AggregatedSignal(
            symbol=symbol,
            symbol_name=get_symbol_name(symbol),
            strength=strength,
            signal_type=direction,
            strategies=[s.strategy.value for s in unique_signals],
            strategy_count=count,
            price=unique_signals[-1].price,  # latest price
            stop_loss=round(best_sl, 2),
            target=round(best_target, 2),
            confidence=round(avg_confidence, 2),
            reasons=[s.reason for s in unique_signals],
            timeframe=best_tf,
            details={
                "strategies": [s.strategy.value for s in unique_signals],
                "individual_confidence": {s.strategy.value: s.confidence for s in unique_signals},
                "trend": unique_signals[0].details.get("trend", "UNKNOWN"),
                "ema50_slope": unique_signals[0].details.get("ema50_slope", 0),
                "ema50": unique_signals[0].details.get("ema50", 0),
                "confirmed": confirmed,
                "conf_rules": conf_rules,
            },
        ))

    # Sort: VERY STRONG first, then STRONG, then normal; within same strength by confidence
    strength_order = {
        SignalStrength.VERY_STRONG_BUY: 0,
        SignalStrength.VERY_STRONG_SELL: 0,
        SignalStrength.STRONG_BUY: 1,
        SignalStrength.STRONG_SELL: 1,
        SignalStrength.BUY: 2,
        SignalStrength.SELL: 2,
    }
    aggregated.sort(key=lambda a: (strength_order.get(a.strength, 3), -a.confidence))

    return aggregated


def format_aggregated_table(signals: list[AggregatedSignal]) -> str:
    """Format aggregated signals as a readable table."""
    if not signals:
        return "No signals found."

    lines = []
    lines.append(f"\n{'='*110}")
    lines.append(f"  SCANNER RESULTS — {len(signals)} stock(s) with signals")
    lines.append(f"{'='*110}")
    lines.append(
        f"  {'Symbol':<10} {'Strength':<18} {'Strategies':<6} {'Price':>10} {'SL':>10} {'Target':>10} {'Conf':>6} {'TF':<8} Strategy Names"
    )
    lines.append(f"  {'-'*106}")

    for s in signals:
        emoji = STRENGTH_EMOJI.get(s.strength, "")
        strat_list = ", ".join(s.strategies)
        lines.append(
            f"  {s.symbol_name:<10} {s.strength.value:<18} {s.strategy_count:<6} "
            f"{s.price:>10.2f} {s.stop_loss:>10.2f} {s.target:>10.2f} "
            f"{s.confidence:>5.0%} {s.timeframe:<8} {strat_list}"
        )

    lines.append(f"{'='*110}\n")
    return "\n".join(lines)


def aggregated_to_dict(signal: AggregatedSignal) -> dict:
    """Convert AggregatedSignal to JSON-serializable dict with quality score."""
    from scanner.quality import score_signal
    d = {
        "symbol": signal.symbol,
        "symbol_name": signal.symbol_name,
        "strength": signal.strength.value,
        "signal_type": signal.signal_type.value,
        "strategies": signal.strategies,
        "strategy_count": signal.strategy_count,
        "price": signal.price,
        "stop_loss": signal.stop_loss,
        "target": signal.target,
        "confidence": signal.confidence,
        "reasons": signal.reasons,
        "timeframe": signal.timeframe,
        "emoji": STRENGTH_EMOJI.get(signal.strength, ""),
        "color": STRENGTH_COLOR.get(signal.strength, "#94a3b8"),
        "confirmed": bool(signal.details.get("confirmed")),
        "conf_rules": signal.details.get("conf_rules", []),
        "details": signal.details,
    }
    try:
        qs = score_signal(d)
        d["quality_score"] = qs.total
        d["quality_tier"] = qs.tier
        d["quality_breakdown"] = qs.breakdown
    except Exception:
        d["quality_score"] = 50
        d["quality_tier"] = "MODERATE"
        d["quality_breakdown"] = {}
    return d
