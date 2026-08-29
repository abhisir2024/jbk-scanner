"""
Signal Groups — clusters related signals for easy scanning.
============================================================

Groups signals by:
  1. SECTOR — "3 Bank stocks showing breakout"
  2. STRATEGY — "8 Volume Shocker signals today"
  3. PATTERN — "5 stocks at 52W high support"
  4. STRENGTH — "4 VERY STRONG signals"
"""

from dataclasses import dataclass, field


@dataclass
class SignalGroup:
    """A cluster of related signals."""
    group_type: str      # "sector", "strategy", "pattern", "strength"
    group_name: str      # e.g. "Banking", "Volume Shocker", "52W High"
    signals: list        # list of signal dicts
    count: int = 0
    emoji: str = ""
    highlight: str = ""  # short description

    def __post_init__(self):
        self.count = len(self.signals)


def group_signals(signals: list[dict]) -> dict:
    """
    Group signals into sectors only.
    Returns dict with keys: sectors, summary.
    """
    if not signals:
        return {"sectors": [], "summary": []}

    sectors = _group_by_sector(signals)
    summary = _build_sector_summary(signals, sectors)

    return {
        "sectors": sectors,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Sector grouping
# ---------------------------------------------------------------------------

SECTOR_MAP = {
    "Banking": ["SBIN", "HDFCBANK", "ICICIBANK", "KOTAKBANK", "AXISBANK",
                "FEDERALBNK", "PNB", "BANKBARODA", "IDFCFIRSTB", "INDUSINDBK",
                "BANKINDIA", "RBLBANK", "BANDHANBNK", "CANBK", "UNIONBANK",
                "YESBANK", "INDIANB", "AUBANK"],
    "IT": ["TCS", "INFY", "WIPRO", "TECHM", "HCLTECH", "MPHASIS",
           "COFORGE", "PERSISTENT", "KPITTECH", "OFSS"],
    "Pharma": ["SUNPHARMA", "DRREDDY", "CIPLA", "DIVISLAB", "LUPIN", "BIOCON",
               "TORNTPHARM", "ALKEM", "AUROPHARMA", "LAURUSLABS", "ZYDUSLIFE"],
    "Auto": ["TATAMOTORS", "BAJAJ-AUTO", "M&M", "HEROMOTOCO", "MARUTI", "EICHERMOT",
             "ASHOKLEY", "TVSMOTOR", "FORCEMOT", "TMPV", "HYUNDAI"],
    "Metal": ["TATASTEEL", "HINDALCO", "JSWSTEEL", "VEDL", "SAIL", "NMDC",
              "NATIONALUM", "HINDZINC", "JINDALSTEL", "COALINDIA", "SOLARINDS"],
    "FMCG": ["HINDUNILVR", "ITC", "NESTLEIND", "TATACONSUM", "BRITANNIA", "MARICO",
             "GODREJCP", "DABUR", "COLPAL", "UNITDSPR", "VBL"],
    "Real Estate": ["DLF", "GODREJPROP", "OBEROIRLTY", "PRESTIGE", "LODHA",
                    "BRIGADE", "PHOENIXLTD"],
    "Financial": ["BAJFINANCE", "BAJAJFINSV", "SBICARD", "SBILIFE", "HDFCLIFE",
                  "ICICIPRULI", "PNBHOUSING", "CHOLAFIN", "MUTHOOTFIN", "MANAPPURAM",
                  "LICHSGFIN", "SHRIRAMFIN", "BAJAJHLDNG", "LTF", "MFSL", "PFC",
                  "RECLTD", "IRFC", "IREDA"],
    "Power": ["NTPC", "POWERGRID", "TATAPOWER", "NHPC", "GAIL", "IOC", "BPCL",
              "HINDPETRO", "ONGC", "OIL", "ADANIPOWER", "ADANIGREEN"],
    "Infra": ["LT", "BHARATFORG", "BEL", "HAL", "COCHINSHIP", "MAZDOCK", "RVNL",
              "NBCC", "IRCON"],
    "Consumer": ["TITAN", "TRENT", "PAGEIND", "VOLTAS", "HAVELLS", "POLYCAB", "KEI",
                 "DIXON", "SONACOMS"],
    "Cement": ["AMBUJACEM", "ULTRACEMCO", "SHREECEM", "ACC"],
    "Telecom": ["BHARTIARTL", "IDEA"],
    "Media": ["ZEE", "NAUKRI"],
}

SECTOR_EMOJI = {
    "Banking": "🏦", "IT": "💻", "Pharma": "💊", "Auto": "🚗",
    "Metal": "⛏️", "FMCG": "🛒", "Real Estate": "🏠", "Financial": "💰",
    "Power": "⚡", "Infra": "🏗️", "Consumer": "🛍️", "Cement": "🧱",
    "Telecom": "📡", "Media": "📺", "Other": "📦",
}


def _get_sector(symbol: str) -> str:
    name = symbol.split(":")[-1].replace("-EQ", "").replace("-BE", "")
    for sector, stocks in SECTOR_MAP.items():
        if name in stocks:
            return sector
    return "Other"


def _group_by_sector(signals: list[dict]) -> list[SignalGroup]:
    sector_signals = {}
    for s in signals:
        sector = _get_sector(s.get("symbol", ""))
        if sector not in sector_signals:
            sector_signals[sector] = []
        sector_signals[sector].append(s)

    groups = []
    for sector, sigs in sorted(sector_signals.items(), key=lambda x: -len(x[1])):
        if len(sigs) < 2:
            continue  # Skip single-signal sectors
        buys = sum(1 for s in sigs if s.get("signal_type") == "BUY")
        sells = len(sigs) - buys
        highlight = f"{len(sigs)} stocks"
        if buys and sells:
            highlight += f" ({buys}B/{sells}S)"
        elif buys:
            highlight += f" BUY"
        else:
            highlight += f" SELL"
        groups.append(SignalGroup(
            group_type="sector",
            group_name=sector,
            signals=sigs,
            emoji=SECTOR_EMOJI.get(sector, "📦"),
            highlight=highlight,
        ))
    return groups


# ---------------------------------------------------------------------------
# Strategy grouping
# ---------------------------------------------------------------------------

STRATEGY_EMOJI = {
    "Range Breakout 9D": "📊", "Range Breakout 15D": "📊",
    "Range Breakout 21D": "📊", "Range Breakout 60D": "📊",
    "Channel Consolidation Breakout": "🔲",
    "Early Breakout": "⚡",
    "52W High Support Buy": "🏔️",
    "Volume Shocker Buy": "🔊",
    "Candlestick Pattern": "🕯️",
    "Med Channel Breakout": "📐",
    "Watchlist Range Breakout": "⭐",
    "Buy on Retracement": "🔁",
    "Trendline Channel Breakout": "📈",
    "Momentum Breakout": "🚀",
}


def _group_by_strategy(signals: list[dict]) -> list[SignalGroup]:
    strat_signals = {}
    for s in signals:
        # Handle both single strategy and strategies list
        strats = s.get("strategies", [])
        if not strats and s.get("strategy"):
            strats = [s["strategy"]]
        for strat in strats:
            if strat not in strat_signals:
                strat_signals[strat] = []
            strat_signals[strat].append(s)

    groups = []
    for strat, sigs in sorted(strat_signals.items(), key=lambda x: -len(x[1])):
        if len(sigs) < 2:
            continue
        buys = sum(1 for s in sigs if s.get("signal_type") == "BUY")
        sells = len(sigs) - buys
        highlight = f"{len(sigs)} signals"
        if buys and sells:
            highlight += f" ({buys}B/{sells}S)"
        groups.append(SignalGroup(
            group_type="strategy",
            group_name=strat,
            signals=sigs,
            emoji=STRATEGY_EMOJI.get(strat, "📊"),
            highlight=highlight,
        ))
    return groups


# ---------------------------------------------------------------------------
# Pattern grouping
# ---------------------------------------------------------------------------

def _get_pattern(signal: dict) -> str:
    """Determine the pattern type of a signal."""
    strats = signal.get("strategies", [])
    if not strats and signal.get("strategy"):
        strats = [signal["strategy"]]
    details = signal.get("details", {})

    # Priority order
    if any("52W" in s or "High Support" in s for s in strats):
        return "52W High Support"
    if any("Range Breakout" in s for s in strats):
        return "Range Breakout"
    if any("Channel" in s and "Consolidation" in s for s in strats):
        return "Channel Squeeze"
    if any("Early Breakout" in s for s in strats):
        return "Pre-Breakout"
    if any("Volume Shocker" in s for s in strats):
        return "Volume Spike"
    if any("Retracement" in s for s in strats):
        return "Buy the Dip"
    if any("Candle" in s for s in strats):
        return "Candlestick"
    if any("Momentum" in s for s in strats):
        return "Momentum"

    return "Other"


PATTERN_EMOJI = {
    "52W High Support": "🏔️", "Range Breakout": "📊", "Channel Squeeze": "🔲",
    "Pre-Breakout": "⚡", "Volume Spike": "🔊", "Buy the Dip": "🔁",
    "Candlestick": "🕯️", "Momentum": "🚀", "Other": "📦",
}


def _group_by_pattern(signals: list[dict]) -> list[SignalGroup]:
    pattern_signals = {}
    for s in signals:
        pattern = _get_pattern(s)
        if pattern not in pattern_signals:
            pattern_signals[pattern] = []
        pattern_signals[pattern].append(s)

    groups = []
    for pattern, sigs in sorted(pattern_signals.items(), key=lambda x: -len(x[1])):
        if len(sigs) < 2:
            continue
        buys = sum(1 for s in sigs if s.get("signal_type") == "BUY")
        sells = len(sigs) - buys
        names = [s.get("symbol_name", s.get("symbol", "?")) for s in sigs[:5]]
        highlight = f"{len(sigs)} stocks: {', '.join(names)}"
        groups.append(SignalGroup(
            group_type="pattern",
            group_name=pattern,
            signals=sigs,
            emoji=PATTERN_EMOJI.get(pattern, "📦"),
            highlight=highlight,
        ))
    return groups


# ---------------------------------------------------------------------------
# Strength grouping
# ---------------------------------------------------------------------------

STRENGTH_EMOJI = {
    "VERY STRONG BUY": "🟢🟢🟢", "VERY STRONG SELL": "🔴🔴🔴",
    "STRONG BUY": "🟢🟢", "STRONG SELL": "🔴🔴",
    "BUY": "🟢", "SELL": "🔴",
}


def _group_by_strength(signals: list[dict]) -> list[SignalGroup]:
    strength_signals = {}
    for s in signals:
        strength = s.get("strength", s.get("signal_type", "BUY"))
        if strength not in strength_signals:
            strength_signals[strength] = []
        strength_signals[strength].append(s)

    order = ["VERY STRONG BUY", "STRONG BUY", "BUY", "SELL", "STRONG SELL", "VERY STRONG SELL"]
    groups = []
    for strength in order:
        if strength in strength_signals:
            sigs = strength_signals[strength]
            names = [s.get("symbol_name", s.get("symbol", "?")) for s in sigs[:5]]
            highlight = f"{len(sigs)} stocks: {', '.join(names)}"
            groups.append(SignalGroup(
                group_type="strength",
                group_name=strength,
                signals=sigs,
                emoji=STRENGTH_EMOJI.get(strength, "📊"),
                highlight=highlight,
            ))
    return groups


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def _build_sector_summary(
    signals: list[dict],
    sectors: list[SignalGroup],
) -> list[dict]:
    """Build human-readable summary lines."""
    summary = []
    total = len(signals)
    buys = sum(1 for s in signals if s.get("signal_type") == "BUY")
    sells = total - buys

    summary.append({
        "text": f"{total} signals ({buys} BUY, {sells} SELL) across {len(sectors)} sectors",
        "emoji": "📊",
        "type": "overview",
    })

    for s in sectors[:3]:
        summary.append({
            "text": f"{s.count} {s.emoji} {s.group_name} stocks",
            "emoji": s.emoji,
            "type": "sector",
        })

    return summary
