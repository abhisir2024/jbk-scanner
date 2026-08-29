"""
Verify the scanner universe against the official NSE F&O master list.

Downloads the latest NSE F&O master from Fyers and reports:
  - stocks in the scanner universe that have NO futures contracts
  - stocks with futures contracts that are MISSING from the universe

Usage:
    python scripts/verify_fo_universe.py
    python scripts/verify_fo_universe.py --keep     # keep downloaded master
"""

import argparse
import csv
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scanner.fno_universe import FNO_STOCKS_COMPLETE

FO_MASTER_URL = "https://public.fyers.in/sym_details/NSE_FO.csv"
DEFAULT_MASTER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "nse_fo_master.csv")


def download_master() -> list:
    print(f"Downloading NSE F&O master from {FO_MASTER_URL} ...")
    req = urllib.request.Request(FO_MASTER_URL, headers={"User-Agent": "Mozilla/5.0"})
    data = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", errors="replace")
    return list(csv.reader(data.splitlines()))


def load_master(path: str) -> list:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return list(csv.reader(f))
    return download_master()


def name(symbol: str) -> str:
    return symbol.split(":")[-1].replace("-EQ", "")


def main():
    parser = argparse.ArgumentParser(description="Verify scanner universe vs NSE F&O master")
    parser.add_argument("--master", default=None, help="Path to downloaded NSE_FO.csv (skips download)")
    parser.add_argument("--keep", action="store_true", help="Save downloaded master to data/nse_fo_master.csv")
    args = parser.parse_args()

    if args.master:
        rows = load_master(args.master)
    else:
        rows = download_master()
        if args.keep:
            with open(DEFAULT_MASTER, "w", encoding="utf-8") as f:
                f.writelines(",".join(r) + "\n" for r in rows)
            print(f"Saved master to {DEFAULT_MASTER}")

    has_fut = set()
    for r in rows:
        if len(r) < 14:
            continue
        if r[9].upper().endswith("FUT"):
            has_fut.add(r[13])

    universe = [name(s) for s in FNO_STOCKS_COMPLETE]

    missing_from_fo = sorted(set(universe) - has_fut)
    missing_from_universe = sorted(has_fut - set(universe))

    print("\n=== In universe but NO futures ===")
    if missing_from_fo:
        for s in missing_from_fo:
            print(" ", s)
    else:
        print("  (none)")

    print("\n=== Have futures but NOT in universe ===")
    if missing_from_universe:
        for s in missing_from_universe:
            print(" ", s)
    else:
        print("  (none)")

    print(f"\nUniverse: {len(universe)} stocks | F&O master underlyings: {len(has_fut)}")
    if missing_from_fo:
        print("ACTION NEEDED: remove/add the stocks above in scanner/fno_universe.py")
        sys.exit(1)
    print("Universe matches the official F&O list.")
    sys.exit(0)


if __name__ == "__main__":
    main()
