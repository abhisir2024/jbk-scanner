"""
Signal Tracker -- logs signals and tracks their outcomes for analysis.

Features:
- Log every signal with entry price, SL, target, timestamp
- Track actual outcome after N days (win/loss/active)
- Calculate win rate, avg P&L, profit factor per strategy
- Generate analysis reports
"""

import json
import os
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict
from typing import Optional

# Signal log file
SIGNAL_LOG_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "signal_history.json")


@dataclass
class TrackedSignal:
    """A signal that was generated and is being tracked."""
    id: str  # unique id: symbol_strategy_timestamp
    symbol: str
    symbol_name: str
    strategy: str
    signal_type: str  # BUY or SELL
    strength: str  # BUY, STRONG BUY, etc.
    entry_price: float
    stop_loss: float
    target: float
    confidence: float
    timeframe: str
    reasons: list[str]
    
    # Timestamps
    signal_time: str  # when signal was generated
    
    # Outcome tracking
    status: str = "active"  # active, hit_target, hit_sl, expired, manual_exit
    exit_price: Optional[float] = None
    exit_time: Optional[str] = None
    pnl_pct: Optional[float] = None
    actual_outcome: Optional[str] = None  # win, loss, breakeven
    
    # Current tracking
    current_price: Optional[float] = None
    current_pnl_pct: Optional[float] = None
    days_held: int = 0
    max_favorable: float = 0.0  # max favorable excursion
    max_adverse: float = 0.0  # max adverse excursion


class SignalTracker:
    """Tracks signals and their outcomes."""
    
    def __init__(self):
        self.signals = self._load_signals()
    
    def _load_signals(self) -> list[TrackedSignal]:
        """Load tracked signals from file."""
        if not os.path.exists(SIGNAL_LOG_FILE):
            return []
        try:
            with open(SIGNAL_LOG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return [TrackedSignal(**s) for s in data]
        except Exception:
            return []
    
    def _save_signals(self):
        """Save tracked signals to file."""
        with open(SIGNAL_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump([asdict(s) for s in self.signals], f, indent=2, ensure_ascii=False)
    
    def log_signal(self, signal_dict: dict) -> str:
        """
        Log a new signal for tracking.
        Returns the signal ID.
        """
        # Create unique ID
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        strategy_name = signal_dict.get('strategy') or (signal_dict['strategies'][0] if signal_dict.get('strategies') else 'Unknown')
        signal_id = f"{signal_dict['symbol']}_{strategy_name}_{timestamp}".replace(" ", "_")
        
        # Check if already tracked (avoid duplicates)
        existing = [s for s in self.signals if s.symbol == signal_dict['symbol'] 
                   and s.strategy == strategy_name
                   and s.status == "active"]
        if existing:
            return existing[0].id  # already tracking this
        
        tracked = TrackedSignal(
            id=signal_id,
            symbol=signal_dict.get("symbol", ""),
            symbol_name=signal_dict.get("symbol_name", ""),
            strategy=strategy_name,
            signal_type=signal_dict.get("signal_type", ""),
            strength=signal_dict.get("strength", "BUY"),
            entry_price=signal_dict.get("price", 0),
            stop_loss=signal_dict.get("stop_loss", 0),
            target=signal_dict.get("target", 0),
            confidence=signal_dict.get("confidence", 0),
            timeframe=signal_dict.get("timeframe", "daily"),
            reasons=signal_dict.get("reasons", []),
            signal_time=datetime.now().isoformat(),
            current_price=signal_dict.get("price", 0),
        )
        
        self.signals.append(tracked)
        self._save_signals()
        return signal_id
    
    def update_price(self, symbol: str, current_price: float):
        """Update current price for all active signals of a symbol."""
        updated = False
        for signal in self.signals:
            if signal.symbol == symbol and signal.status == "active":
                signal.current_price = current_price
                signal.days_held = (datetime.now() - datetime.fromisoformat(signal.signal_time)).days
                
                # Calculate current P&L
                if signal.signal_type == "BUY":
                    signal.current_pnl_pct = ((current_price - signal.entry_price) / signal.entry_price) * 100
                else:
                    signal.current_pnl_pct = ((signal.entry_price - current_price) / signal.entry_price) * 100
                
                # Track max favorable/adverse excursion
                signal.max_favorable = max(signal.max_favorable, signal.current_pnl_pct)
                signal.max_adverse = min(signal.max_adverse, signal.current_pnl_pct)
                
                # Check if target or SL hit
                if signal.signal_type == "BUY":
                    if current_price >= signal.target:
                        signal.status = "hit_target"
                        signal.exit_price = signal.target
                        signal.exit_time = datetime.now().isoformat()
                        signal.pnl_pct = ((signal.target - signal.entry_price) / signal.entry_price) * 100
                        signal.actual_outcome = "win"
                    elif current_price <= signal.stop_loss:
                        signal.status = "hit_sl"
                        signal.exit_price = signal.stop_loss
                        signal.exit_time = datetime.now().isoformat()
                        signal.pnl_pct = ((signal.stop_loss - signal.entry_price) / signal.entry_price) * 100
                        signal.actual_outcome = "loss"
                else:  # SELL
                    if current_price <= signal.target:
                        signal.status = "hit_target"
                        signal.exit_price = signal.target
                        signal.exit_time = datetime.now().isoformat()
                        signal.pnl_pct = ((signal.entry_price - signal.target) / signal.entry_price) * 100
                        signal.actual_outcome = "win"
                    elif current_price >= signal.stop_loss:
                        signal.status = "hit_sl"
                        signal.exit_price = signal.stop_loss
                        signal.exit_time = datetime.now().isoformat()
                        signal.pnl_pct = ((signal.entry_price - signal.stop_loss) / signal.entry_price) * 100
                        signal.actual_outcome = "loss"
                
                # Expire after 10 days
                if signal.days_held >= 10 and signal.status == "active":
                    signal.status = "expired"
                    signal.exit_price = current_price
                    signal.exit_time = datetime.now().isoformat()
                    signal.pnl_pct = signal.current_pnl_pct
                    signal.actual_outcome = "breakeven" if abs(signal.pnl_pct) < 0.5 else ("win" if signal.pnl_pct > 0 else "loss")
                
                updated = True
        
        if updated:
            self._save_signals()
    
    def manual_exit(self, symbol: str, exit_price: float):
        """Manually close a signal."""
        for signal in self.signals:
            if signal.symbol == symbol and signal.status == "active":
                signal.status = "manual_exit"
                signal.exit_price = exit_price
                signal.exit_time = datetime.now().isoformat()
                if signal.signal_type == "BUY":
                    signal.pnl_pct = ((exit_price - signal.entry_price) / signal.entry_price) * 100
                else:
                    signal.pnl_pct = ((signal.entry_price - exit_price) / signal.entry_price) * 100
                signal.actual_outcome = "win" if signal.pnl_pct > 0 else ("loss" if signal.pnl_pct < 0 else "breakeven")
                self._save_signals()
                return True
        return False
    
    def get_active_signals(self) -> list[TrackedSignal]:
        """Get all active signals."""
        return [s for s in self.signals if s.status == "active"]
    
    def get_closed_signals(self) -> list[TrackedSignal]:
        """Get all closed signals (target hit, SL hit, expired, manual)."""
        return [s for s in self.signals if s.status != "active"]
    
    def get_signals_by_strategy(self, strategy: str) -> list[TrackedSignal]:
        """Get all signals for a specific strategy."""
        return [s for s in self.signals if s.strategy == strategy]
    
    def calculate_stats(self, strategy: str = None) -> dict:
        """Calculate performance statistics."""
        if strategy:
            signals = [s for s in self.signals if s.strategy == strategy]
        else:
            signals = self.signals
        
        closed = [s for s in signals if s.status != "active" and s.pnl_pct is not None]
        if not closed:
            return {
                "total_signals": len(signals),
                "active": len([s for s in signals if s.status == "active"]),
                "closed": 0,
                "win_rate": 0,
                "avg_pnl": 0,
                "total_pnl": 0,
                "profit_factor": 0,
                "avg_win": 0,
                "avg_loss": 0,
                "max_win": 0,
                "max_loss": 0,
            }
        
        winners = [s for s in closed if s.pnl_pct > 0]
        losers = [s for s in closed if s.pnl_pct <= 0]
        
        total_pnl = sum(s.pnl_pct for s in closed)
        avg_pnl = total_pnl / len(closed)
        
        win_pnls = [s.pnl_pct for s in winners]
        loss_pnls = [s.pnl_pct for s in losers]
        
        avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0
        avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0
        max_win = max(win_pnls) if win_pnls else 0
        max_loss = min(loss_pnls) if loss_pnls else 0
        
        gross_profit = sum(win_pnls) if win_pnls else 0
        gross_loss = abs(sum(loss_pnls)) if loss_pnls else 0.01
        profit_factor = gross_profit / gross_loss
        
        return {
            "total_signals": len(signals),
            "active": len([s for s in signals if s.status == "active"]),
            "closed": len(closed),
            "winners": len(winners),
            "losers": len(losers),
            "win_rate": round(len(winners) / len(closed) * 100, 1),
            "avg_pnl": round(avg_pnl, 2),
            "total_pnl": round(total_pnl, 2),
            "profit_factor": round(profit_factor, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "max_win": round(max_win, 2),
            "max_loss": round(max_loss, 2),
        }
    
    def generate_report(self) -> str:
        """Generate a formatted analysis report."""
        lines = []
        lines.append("\n" + "=" * 100)
        lines.append("  SIGNAL PERFORMANCE REPORT")
        lines.append("=" * 100)
        
        # Overall stats
        overall = self.calculate_stats()
        lines.append(f"\n  OVERALL PERFORMANCE:")
        lines.append(f"  {'-' * 40}")
        lines.append(f"  Total Signals:     {overall['total_signals']}")
        lines.append(f"  Active:            {overall['active']}")
        lines.append(f"  Closed:            {overall['closed']}")
        lines.append(f"  Win Rate:          {overall['win_rate']}%")
        lines.append(f"  Avg P&L/Trade:     {overall['avg_pnl']:+.2f}%")
        lines.append(f"  Total P&L:         {overall['total_pnl']:+.2f}%")
        lines.append(f"  Profit Factor:     {overall['profit_factor']:.2f}")
        
        # Per-strategy stats
        strategies = list(set(s.strategy for s in self.signals))
        if strategies:
            lines.append(f"\n  BY STRATEGY:")
            lines.append(f"  {'-' * 40}")
            for strat in sorted(strategies):
                stats = self.calculate_stats(strat)
                if stats['closed'] > 0:
                    lines.append(f"  {strat}:")
                    lines.append(f"    Trades: {stats['closed']} | Win Rate: {stats['win_rate']}% | Avg P&L: {stats['avg_pnl']:+.2f}% | PF: {stats['profit_factor']:.2f}")
        
        # Recent signals
        recent = sorted(self.signals, key=lambda s: s.signal_time, reverse=True)[:10]
        if recent:
            lines.append(f"\n  RECENT SIGNALS (last 10):")
            lines.append(f"  {'-' * 96}")
            lines.append(f"  {'Symbol':<10} {'Type':<6} {'Strategy':<25} {'Entry':>10} {'Current':>10} {'P&L':>8} {'Status':<12} {'Days':>5}")
            lines.append(f"  {'-' * 96}")
            for s in recent:
                pnl_str = f"{s.pnl_pct:+.2f}%" if s.pnl_pct is not None else (f"{s.current_pnl_pct:+.2f}%" if s.current_pnl_pct is not None else "N/A")
                lines.append(
                    f"  {s.symbol_name:<10} {s.signal_type:<6} {s.strategy:<25} "
                    f"{s.entry_price:>10.2f} {s.current_price or 0:>10.2f} "
                    f"{pnl_str:>8} {s.status:<12} {s.days_held:>5}"
                )
        
        # Active signals needing attention
        active = self.get_active_signals()
        losing = [s for s in active if s.current_pnl_pct is not None and s.current_pnl_pct < -2]
        if losing:
            lines.append(f"\n  [WARNING] ACTIVE SIGNALS IN LOSS (>2%):")
            lines.append(f"  {'-' * 40}")
            for s in losing:
                lines.append(f"  {s.symbol_name}: {s.current_pnl_pct:+.2f}% | SL: {s.stop_loss:.2f} | Entry: {s.entry_price:.2f}")
        
        lines.append("\n" + "=" * 100)
        return "\n".join(lines)
    
    def clear_old_signals(self, days: int = 30):
        """Remove signals older than N days."""
        cutoff = datetime.now() - timedelta(days=days)
        self.signals = [
            s for s in self.signals
            if datetime.fromisoformat(s.signal_time) > cutoff
        ]
        self._save_signals()
