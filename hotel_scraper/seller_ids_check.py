"""Re-check every stored seller match with today's same-hotel rules (agoda.judge, settings name_match and each reader's
own distances). A match that no longer passes is set to "not found" (looked up again after the reader's retry_days)
and its prices are dropped, so a price read for another hotel never reaches the deal check.

    python -m hotel_scraper.seller_ids_check --ids data/seller_ids.csv --prices data/seller_prices.csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config
from .agoda import ID_FIELDS, PRICE_FIELDS, judge
from .scan import read_csv, write_csv

SECTIONS = {"agoda": "agoda", "trip_com": "tripcom", "hrs": "hrs", "vio": "vio", "super_com": "super_com",
            "edreams": "edreams"}


def recheck(ids: list[dict], watch: dict[str, dict], cfg: dict) -> tuple[list[dict], set]:
    """(rows, {(seller, pid) no longer matched}). Rows of sellers matched another way (official sites) are kept."""
    dropped = set()
    out = []
    for r in ids:
        sec = SECTIONS.get(r.get("seller", ""))
        w = watch.get(r["pid"])
        if r.get("status") == "ok" and sec and w and r.get("seller_name") and r.get("lat") and r.get("lon"):
            m = judge(w, {"name": r["seller_name"], "lat": r["lat"], "lon": r["lon"]}, cfg[sec])
            if m and not m["ok"]:
                r = {**r, "status": "not found"}
                dropped.add((r["seller"], r["pid"]))
        out.append(r)
    return out, dropped


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(config.DEFAULT_PATH))
    ap.add_argument("--ids", required=True)
    ap.add_argument("--prices")
    args = ap.parse_args(argv)
    cfg = config.load(args.config)
    pos = {}
    if cfg["paths"].get("locations") and Path(cfg["paths"]["locations"]).exists():
        from .locations import positions
        pos = positions(cfg)
    watch = {}
    for w in read_csv(cfg["paths"]["watchlist"]):
        lat, lon = pos.get(w["pid"], (w.get("lat"), w.get("lon")))
        if lat and lon:
            watch[w["pid"]] = {**w, "lat": lat, "lon": lon}
    rows, dropped = recheck(read_csv(args.ids), watch, cfg)
    write_csv(rows, args.ids, ID_FIELDS)
    gone = 0
    if args.prices and Path(args.prices).exists():
        prices = read_csv(args.prices)
        keep = [x for x in prices if (x.get("seller"), x.get("pid")) not in dropped]
        gone = len(prices) - len(keep)
        write_csv(keep, args.prices, PRICE_FIELDS)
    by = {}
    for s, _ in dropped:
        by[s] = by.get(s, 0) + 1
    print(f"matches re-checked: {len(dropped)} no longer the same hotel {by or ''}; {gone} prices dropped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
