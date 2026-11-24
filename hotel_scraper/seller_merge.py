"""Combine what parallel seller readers wrote (settings sellers): each reader's job uploads its own seller's rows;
this replaces that seller's rows in the shared files and keeps every other seller's.

    python -m hotel_scraper.seller_merge --kind prices --base data/seller_prices.csv --parts parts/*/seller_prices.csv
    python -m hotel_scraper.seller_merge --kind ids --base data/seller_ids.csv --parts parts/*/seller_ids.csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .agoda import ID_FIELDS, PRICE_FIELDS
from .scan import read_csv, write_csv


def priced(r: dict) -> bool:
    """False for a row that says 'available' with no price above zero (Trip.com, run 45397529258: 137 such rows at
    €0). Such a row is no price and is never published."""
    if r.get("available") != "yes" or "price" not in r:
        return True
    try:
        return float(r.get("price") or 0) > 0
    except ValueError:
        return False


def merge(base: list[dict], parts: list[list[dict]], key) -> list[dict]:
    """Rows of `base`, with every seller that appears in a part replaced by that part's rows; one row per key (the
    latest seen), so a part that came unfiltered can't repeat rows (run 45374215059: cancelled jobs uploaded the whole
    file four times over)."""
    sellers = {r["seller"] for part in parts for r in part}
    out = [r for r in base if r.get("seller") not in sellers]
    for part in parts:
        out += part
    stamp = (lambda r: r.get("seen_at") or r.get("checked_at") or "")
    best: dict = {}
    for r in out:
        if not priced(r):
            continue
        k = key(r)
        if k not in best or stamp(r) >= stamp(best[k]):
            best[k] = r
    return sorted(best.values(), key=key)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kind", choices=["prices", "ids"], required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--parts", nargs="*", default=[])
    ap.add_argument("--only", help="keep only this seller's rows of --base (a reader's own part) and stop")
    args = ap.parse_args(argv)
    fields = PRICE_FIELDS if args.kind == "prices" else ID_FIELDS
    key = (lambda r: (r["seller"], r["pid"], r["night"])) if args.kind == "prices" else (lambda r: (r["seller"], r["pid"]))
    base = read_csv(args.base) if Path(args.base).exists() else []
    if args.only:
        write_csv(sorted((r for r in base if r.get("seller") == args.only), key=key), args.base, fields)
        return 0
    parts = [read_csv(p) for p in args.parts if Path(p).exists()]
    rows = merge(base, parts, key)
    write_csv(rows, args.base, fields)
    print(f"{args.kind}: {len(rows)} rows ({', '.join(sorted({r['seller'] for p in parts for r in p}))} from this run)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
