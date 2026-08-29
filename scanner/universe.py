"""
Stock Universe — All 211 F&O stocks and indices for scanning.
Source: NSE Official F&O List (matched against Dhan NSE FNO Lot Size CSV)
"""

from scanner.fno_universe import FNO_STOCKS_COMPLETE, ALL_SYMBOLS, TOTAL_STOCKS

# Deduped F&O stock list (the raw list has a duplicate IDFCFIRSTB)
_seen = set()
FNO_STOCKS = []
for sym in FNO_STOCKS_COMPLETE:
    if sym not in _seen:
        _seen.add(sym)
        FNO_STOCKS.append(sym)

INDICES = [
    "NSE:NIFTY50-INDEX",
    "NSE:BANKNIFTY-INDEX",
    "NSE:FINNIFTY-INDEX",
    "NSE:NIFTYIT-INDEX",
    "NSE:NIFTYMIDCAP100-INDEX",
    "NSE:NIFTYNEXT50-INDEX",
    "NSE:NIFTYBANK-INDEX",
]

ALL_SYMBOLS = INDICES + FNO_STOCKS

# Remove duplicates
seen = set()
ALL_UNIQUE = []
for sym in ALL_SYMBOLS:
    if sym not in seen:
        seen.add(sym)
        ALL_UNIQUE.append(sym)

ALL_SYMBOLS = ALL_UNIQUE


def get_symbol_name(symbol: str) -> str:
    """Short display name: NSE:SBIN-EQ -> SBIN, NIFTY50-INDEX -> NIFTY50."""
    name = symbol.split(":")[-1]
    name = name.replace("-EQ", "").replace("-BE", "")
    name = name.replace("-INDEX", "")
    return name
