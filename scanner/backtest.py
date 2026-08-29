"""
Backtester v2 — full strategy evaluation with walk-forward testing.

Usage:
    from scanner.backtest import Backtester, run_full_backtest
    bt = Backtester()
    results = bt.run("NSE:SBIN-EQ", days=252)

    # Or run full backtest across universe
    results = run_full_backtest(symbols=["NSE:TCS-EQ", "NSE:RELIANCE-EQ"], days=252)
"""

import json
import os
import sys
from scanner.rate_limiter import get_limiter
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from auth.login import get_fyers_client, load_env
from scanner.strategies import (
    RangeBreakout, EarlyBreakout, HighSupportBuy,
    ChannelConsolidationBreakout, VolumeShocker, MedChannelBreakout,
    WatchlistBreakout, BuyOnRetracement, ChannelBreakout,
    MomentumBreakout, Signal, SignalType, StrategyName,
    RANGE_BREAKOUT_PERIODS,
)
from scanner.universe import get_symbol_name, ALL_SYMBOLS


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    symbol: str
    strategy: str
    signal_type: str
    entry_price: float
    entry_date: str
    exit_price: float
    exit_date: str
    pnl_pct: float
    hold_days: int
    hit_target: bool
    hit_sl: bool


@dataclass
class BacktestResult:
    strategy: str
    total_trades: int
    winning_trades: int
    losing_trades: int
    breakeven_trades: int
    win_rate: float
    avg_pnl_pct: float
    total_pnl_pct: float
    max_win_pct: float
    max_loss_pct: float
    avg_win_pct: float
    avg_loss_pct: float
    profit_factor: float
    expectancy: float
    max_drawdown_pct: float
    sharpe_ratio: float
    avg_hold_days: float
    grade: str
    trades: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Backtester
# ---------------------------------------------------------------------------

class Backtester:
    """Backtest strategies against historical data with walk-forward simulation."""

    def __init__(self):
        load_env()
        self.fyers = get_fyers_client()

    def _fetch_candles(self, symbol: str, days: int = 252) -> list:
        end = datetime.now()
        start = end - timedelta(days=days + 60)
        data = {
            "symbol": symbol,
            "resolution": "D",
            "date_format": 1,
            "range_from": start.strftime("%Y-%m-%d"),
            "range_to": end.strftime("%Y-%m-%d"),
            "cont_flag": 1,
        }
        limiter = get_limiter()
        resp = limiter.retry_call(self.fyers.history, data=data)
        if resp and resp.get("s") == "ok":
            return resp.get("candles", [])
        return []

    def _get_all_strategies(self) -> dict:
        """Return all strategies with their current parameters."""
        watchlist_rules = {}
        try:
            path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "watchlist_rules.json")
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    watchlist_rules = json.load(f)
        except Exception:
            pass

        return {
            "Range Breakout 9D": RangeBreakout(lookback=9, volume_mult=1.5),
            "Range Breakout 15D": RangeBreakout(lookback=15, volume_mult=1.5),
            "Range Breakout 21D": RangeBreakout(lookback=21, volume_mult=1.5),
            "Range Breakout 60D": RangeBreakout(lookback=60, volume_mult=1.5),
            "Channel Consolidation": ChannelConsolidationBreakout(),
            "Early Breakout": EarlyBreakout(lookback=15, proximity_pct=2.0, volume_mult=1.5),
            "52W High Support": HighSupportBuy(proximity_pct=5.0, vol_max_mult=3.0, rsi_min=45.0),
            "Volume Shocker": VolumeShocker(volume_mult=2.0),
            "Med Channel Breakout": MedChannelBreakout(lookback=30, channel_pct=15.0, volume_mult=0.8),
            "Watchlist Breakout": WatchlistBreakout(config=watchlist_rules),
            "Buy on Retracement": BuyOnRetracement(),
            "Channel Breakout": ChannelBreakout(),
            "Momentum Breakout": MomentumBreakout(),
        }

    def _run_walkforward(
        self, strategy, candles: list, symbol: str,
        lookback: int = 60, hold_days: int = 10,
    ) -> list:
        """Walk forward through candles, running strategy at each bar."""
        trades = []
        n = len(candles)
        if n < lookback + hold_days:
            return trades

        last_signal_bar = -999  # prevent duplicate signals within hold period

        for i in range(lookback, n - hold_days):
            if i - last_signal_bar < hold_days:
                continue  # still in hold period

            window = candles[:i + 1]
            opens = [c[1] for c in window]
            highs = [c[2] for c in window]
            lows = [c[3] for c in window]
            closes = [c[4] for c in window]
            volumes = [c[5] for c in window]

            try:
                signals = strategy.scan(symbol, opens, highs, lows, closes, volumes, "daily")
            except Exception:
                continue
            if not signals:
                continue

            signal = signals[0]
            last_signal_bar = i

            # Entry at next bar open
            entry_price = candles[i + 1][1]
            entry_date = datetime.fromtimestamp(candles[i + 1][0]).strftime("%Y-%m-%d")

            exit_price = entry_price
            exit_date = entry_date
            hit_target = False
            hit_sl = False

            for j in range(i + 2, min(i + 2 + hold_days, n)):
                bar_high = candles[j][2]
                bar_low = candles[j][3]

                if signal.signal_type == SignalType.BUY:
                    if bar_high >= signal.target:
                        exit_price = signal.target
                        exit_date = datetime.fromtimestamp(candles[j][0]).strftime("%Y-%m-%d")
                        hit_target = True
                        break
                    if bar_low <= signal.stop_loss:
                        exit_price = signal.stop_loss
                        exit_date = datetime.fromtimestamp(candles[j][0]).strftime("%Y-%m-%d")
                        hit_sl = True
                        break
                else:
                    if bar_low <= signal.target:
                        exit_price = signal.target
                        exit_date = datetime.fromtimestamp(candles[j][0]).strftime("%Y-%m-%d")
                        hit_target = True
                        break
                    if bar_high >= signal.stop_loss:
                        exit_price = signal.stop_loss
                        exit_date = datetime.fromtimestamp(candles[j][0]).strftime("%Y-%m-%d")
                        hit_sl = True
                        break

            if not hit_target and not hit_sl:
                last_idx = min(i + 1 + hold_days, n - 1)
                exit_price = candles[last_idx][4]
                exit_date = datetime.fromtimestamp(candles[last_idx][0]).strftime("%Y-%m-%d")

            if signal.signal_type == SignalType.BUY:
                pnl_pct = ((exit_price - entry_price) / entry_price) * 100
            else:
                pnl_pct = ((entry_price - exit_price) / entry_price) * 100

            hold = (datetime.strptime(exit_date, "%Y-%m-%d") - datetime.strptime(entry_date, "%Y-%m-%d")).days

            trades.append(Trade(
                symbol=symbol,
                strategy=signal.strategy.value,
                signal_type=signal.signal_type.value,
                entry_price=round(entry_price, 2),
                entry_date=entry_date,
                exit_price=round(exit_price, 2),
                exit_date=exit_date,
                pnl_pct=round(pnl_pct, 2),
                hold_days=hold,
                hit_target=hit_target,
                hit_sl=hit_sl,
            ))

        return trades

    def _compute_result(self, strategy_name: str, trades: list) -> BacktestResult:
        """Compute summary stats from a list of trades."""
        if not trades:
            return BacktestResult(
                strategy=strategy_name, total_trades=0, winning_trades=0, losing_trades=0,
                breakeven_trades=0, win_rate=0, avg_pnl_pct=0, total_pnl_pct=0,
                max_win_pct=0, max_loss_pct=0, avg_win_pct=0, avg_loss_pct=0,
                profit_factor=0, expectancy=0, max_drawdown_pct=0, sharpe_ratio=0,
                avg_hold_days=0, grade="N/A",
            )

        winners = [t for t in trades if t.pnl_pct > 0]
        losers = [t for t in trades if t.pnl_pct < 0]
        breakeven = [t for t in trades if t.pnl_pct == 0]

        total_pnl = sum(t.pnl_pct for t in trades)
        avg_pnl = total_pnl / len(trades)

        win_pnls = [t.pnl_pct for t in winners]
        loss_pnls = [t.pnl_pct for t in losers]

        avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0
        avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0
        max_win = max(win_pnls) if win_pnls else 0
        max_loss = min(loss_pnls) if loss_pnls else 0

        gross_profit = sum(win_pnls) if win_pnls else 0
        gross_loss = abs(sum(loss_pnls)) if loss_pnls else 0.01
        profit_factor = gross_profit / gross_loss

        # Expectancy = (win_rate * avg_win) - (loss_rate * avg_loss)
        wr = len(winners) / len(trades)
        lr = len(losers) / len(trades)
        expectancy = (wr * avg_win) + (lr * avg_loss)

        # Max drawdown
        cumulative = 0
        peak = 0
        max_dd = 0
        for t in trades:
            cumulative += t.pnl_pct
            peak = max(peak, cumulative)
            dd = peak - cumulative
            max_dd = max(max_dd, dd)

        # Sharpe ratio (simplified, assuming 0% risk-free rate)
        pnls = [t.pnl_pct for t in trades]
        mean_pnl = sum(pnls) / len(pnls)
        if len(pnls) > 1:
            variance = sum((p - mean_pnl) ** 2 for p in pnls) / (len(pnls) - 1)
            std_dev = variance ** 0.5
            sharpe = (mean_pnl / std_dev) * (252 ** 0.5) if std_dev > 0 else 0
        else:
            sharpe = 0

        avg_hold = sum(t.hold_days for t in trades) / len(trades)

        # Grade
        grade = self._grade(win_rate=wr * 100, pf=profit_factor, expectancy=expectancy)

        return BacktestResult(
            strategy=strategy_name,
            total_trades=len(trades),
            winning_trades=len(winners),
            losing_trades=len(losers),
            breakeven_trades=len(breakeven),
            win_rate=round(wr * 100, 1),
            avg_pnl_pct=round(avg_pnl, 2),
            total_pnl_pct=round(total_pnl, 2),
            max_win_pct=round(max_win, 2),
            max_loss_pct=round(max_loss, 2),
            avg_win_pct=round(avg_win, 2),
            avg_loss_pct=round(avg_loss, 2),
            profit_factor=round(profit_factor, 2),
            expectancy=round(expectancy, 2),
            max_drawdown_pct=round(max_dd, 2),
            sharpe_ratio=round(sharpe, 2),
            avg_hold_days=round(avg_hold, 1),
            grade=grade,
            trades=trades,
        )

    @staticmethod
    def _grade(win_rate: float, pf: float, expectancy: float) -> str:
        score = 0
        if win_rate >= 50: score += 3
        elif win_rate >= 45: score += 2
        elif win_rate >= 40: score += 1
        if pf >= 2.0: score += 3
        elif pf >= 1.5: score += 2
        elif pf >= 1.0: score += 1
        if expectancy > 0.5: score += 3
        elif expectancy > 0.2: score += 2
        elif expectancy > 0: score += 1
        grades = {0: "F", 1: "D", 2: "D+", 3: "C-", 4: "C", 5: "C+",
                  6: "B-", 7: "B", 8: "B+", 9: "A-", 10: "A", 11: "A+", 12: "A++"}
        return grades.get(score, "F")

    def run(self, symbol: str, days: int = 252, hold_days: int = 10) -> dict:
        """Run all strategies on one symbol and return results."""
        candles = self._fetch_candles(symbol, days)
        if not candles or len(candles) < 60:
            return {}

        name = get_symbol_name(symbol)
        strategies = self._get_all_strategies()
        results = {}

        for strat_name, strat in strategies.items():
            trades = self._run_walkforward(strat, candles, symbol, hold_days=hold_days)
            result = self._compute_result(strat_name, trades)
            results[strat_name] = result

        return results

    @staticmethod
    def print_results(results: dict):
        for name, r in results.items():
            if r.total_trades == 0:
                continue
            print(f"\n{'='*60}")
            print(f"  {name} [{r.grade}]")
            print(f"{'='*60}")
            print(f"  Trades: {r.total_trades} (W:{r.winning_trades} L:{r.losing_trades} BE:{r.breakeven_trades})")
            print(f"  Win Rate: {r.win_rate}%  |  Profit Factor: {r.profit_factor}  |  Expectancy: {r.expectancy:+.2f}%")
            print(f"  Avg P&L: {r.avg_pnl_pct:+.2f}%  |  Total P&L: {r.total_pnl_pct:+.2f}%")
            print(f"  Avg Win: {r.avg_win_pct:+.2f}%  |  Avg Loss: {r.avg_loss_pct:+.2f}%")
            print(f"  Max Win: {r.max_win_pct:+.2f}%  |  Max Loss: {r.max_loss_pct:+.2f}%")
            print(f"  Sharpe: {r.sharpe_ratio}  |  Max DD: {r.max_drawdown_pct:.2f}%  |  Avg Hold: {r.avg_hold_days:.0f}d")


# ---------------------------------------------------------------------------
# Batch backtest across multiple symbols
# ---------------------------------------------------------------------------

def run_full_backtest(
    symbols: list[str] | None = None,
    days: int = 252,
    hold_days: int = 10,
    max_symbols: int = 50,
) -> dict:
    """Run backtest across multiple symbols and aggregate results per strategy."""
    bt = Backtester()
    if symbols is None:
        symbols = [s for s in ALL_SYMBOLS if "-INDEX" not in s][:max_symbols]

    # Aggregate trades per strategy
    all_trades: dict[str, list] = {}
    for sym in symbols:
        name = get_symbol_name(sym)
        print(f"  Backtesting {name}...", end=" ", flush=True)
        candles = bt._fetch_candles(sym, days)
        if not candles or len(candles) < 60:
            print("skip (no data)")
            continue

        strategies = bt._get_all_strategies()
        for strat_name, strat in strategies.items():
            trades = bt._run_walkforward(strat, candles, sym, hold_days=hold_days)
            if strat_name not in all_trades:
                all_trades[strat_name] = []
            all_trades[strat_name].extend(trades)

        print(f"done ({sum(len(v) for v in all_trades.values())} trades so far)")

    # Compute aggregated results
    aggregated = {}
    for strat_name, trades in all_trades.items():
        aggregated[strat_name] = bt._compute_result(strat_name, trades)

    return aggregated


# ---------------------------------------------------------------------------
# JSON export for dashboard
# ---------------------------------------------------------------------------

def result_to_dict(r: BacktestResult) -> dict:
    """Convert BacktestResult to JSON-safe dict (without trades list)."""
    return {
        "strategy": r.strategy,
        "total_trades": r.total_trades,
        "winning_trades": r.winning_trades,
        "losing_trades": r.losing_trades,
        "breakeven_trades": r.breakeven_trades,
        "win_rate": r.win_rate,
        "avg_pnl_pct": r.avg_pnl_pct,
        "total_pnl_pct": r.total_pnl_pct,
        "max_win_pct": r.max_win_pct,
        "max_loss_pct": r.max_loss_pct,
        "avg_win_pct": r.avg_win_pct,
        "avg_loss_pct": r.avg_loss_pct,
        "profit_factor": r.profit_factor,
        "expectancy": r.expectancy,
        "max_drawdown_pct": r.max_drawdown_pct,
        "sharpe_ratio": r.sharpe_ratio,
        "avg_hold_days": r.avg_hold_days,
        "grade": r.grade,
    }


def save_backtest_results(results: dict, path: str = None):
    """Save backtest results to JSON for dashboard consumption."""
    if path is None:
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "backtest_results.json")

    data = {
        "generated": datetime.now().isoformat(),
        "strategies": {},
    }
    for name, r in results.items():
        data["strategies"][name] = result_to_dict(r)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"\nResults saved to {path}")


def load_backtest_results(path: str = None) -> dict:
    """Load backtest results from JSON."""
    if path is None:
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "backtest_results.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Strategy Backtester")
    parser.add_argument("--symbols", nargs="*", help="Specific symbols to test")
    parser.add_argument("--days", type=int, default=252, help="Days of history")
    parser.add_argument("--hold", type=int, default=10, help="Hold period in days")
    parser.add_argument("--all", action="store_true", help="Test all F&O stocks")
    parser.add_argument("--max", type=int, default=50, help="Max symbols to test")
    parser.add_argument("--save", action="store_true", help="Save results to JSON")
    args = parser.parse_args()

    if args.all or not args.symbols:
        print(f"Running full backtest ({args.max} symbols, {args.days} days)...\n")
        results = run_full_backtest(days=args.days, hold_days=args.hold, max_symbols=args.max)
    else:
        bt = Backtester()
        results = {}
        for sym in args.symbols:
            if not sym.startswith("NSE:"):
                sym = f"NSE:{sym}-EQ"
            r = bt.run(sym, days=args.days, hold_days=args.hold)
            results.update(r)

    Backtester.print_results(results)

    if args.save:
        save_backtest_results(results)
