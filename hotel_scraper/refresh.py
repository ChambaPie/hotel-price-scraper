"""OpenStreetMap refresh (rolling set of map tiles) -> data/osm_catalog.csv, the source of official star ratings.
The catalog you select from is built by ta_refresh (TripAdvisor listings), which reads stars from this file.

    python -m hotel_scraper.refresh                       # live
    python -m hotel_scraper.refresh --max-tiles 3         # quick live test
    python -m hotel_scraper.refresh --fixture tests/fixtures/overpass_sample.json   # offline (no drive times)

Writes <out_dir>/catalog_summary.md every run, and <out_dir>/catalog_changes.md only when an existing
catalog changed (the workflow turns that file into a GitHub issue).
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

from . import catalog as cat
from . import config, discover_osm, drive_time, geo, selection, tiles


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%H:%M:%S} {msg}", flush=True)


def summary_md(rows: list[dict], watched: list[dict], dts: drive_time.Stats | None = None,
               tile_line: str = "") -> str:
    active = [r for r in rows if not r.get("gone_since")]
    lines = [f"## Catalog: {len(active)} active properties, {len(watched)} watched", ""]
    if tile_line:
        lines += [tile_line, ""]
    if dts is not None:
        lines += [f"Drive times: {dts.routed} routed now, {dts.cached} from cache, {dts.pending} pending, "
                  f"{dts.unroutable} unroutable, {dts.dropped} left out (over the limit)."
                  + (f" Router error: {dts.error}" if dts.error else ""), ""]
    lines += [
             "| Country | " + " | ".join(sorted({r['kind'] for r in active})) + " | Watched |",
             "|---|" + "---|" * (len({r['kind'] for r in active}) + 1)]
    kinds = sorted({r["kind"] for r in active})
    by = Counter((r.get("country") or "??", r["kind"]) for r in active)
    w_by = Counter(r.get("country") or "??" for r in watched)
    for cc in sorted({c for c, _ in by}, key=lambda c: -sum(by[(c, k)] for k in kinds)):
        lines.append(f"| {cc} | " + " | ".join(str(by[(cc, k)]) for k in kinds) + f" | {w_by[cc]} |")
    return "\n".join(lines) + "\n"


def reportable(ch: cat.Changes, seen_before) -> cat.Changes:
    """Properties found in a tile's first-ever refresh are the map being filled in, not news: leave them out."""
    return cat.Changes(new=[r for r in ch.new if seen_before(r)], gone=ch.gone, returned=ch.returned)


def changes_md(ch: cat.Changes, watched_ids: set[str], limit: int, today: str) -> str:
    def block(title: str, items: list[dict]) -> list[str]:
        if not items:
            return []
        out = [f"### {title} ({len(items)})", "", "| Id | Name | Type | Stars | Town | Country | km | Watched |",
               "|---|---|---|---|---|---|---|---|"]
        for r in items[:limit]:
            out.append(f"| `{r['pid']}` | {r['name']} | {r['kind']} | {r.get('stars', '')} | {r.get('city', '')} "
                       f"| {r.get('country', '')} | {r.get('distance_km', '')} | {'yes' if r['pid'] in watched_ids else 'no'} |")
        if len(items) > limit:
            out.append(f"\n…and {len(items) - limit} more (see data/catalog.csv, first_seen/gone_since = {today}).")
        return out + [""]
    body = [f"Catalog changes on {today}.", "",
            "To change what is watched, run the **Edit selection** workflow with the ids, "
            "or edit `config/include.txt` / `config/exclude.txt`.", ""]
    body += block("New", ch.new) + block("Back again", ch.returned) + block("Gone", ch.gone)
    return "\n".join(body)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(config.DEFAULT_PATH))
    ap.add_argument("--max-tiles", type=int)
    ap.add_argument("--fixture", help="Offline: use this Overpass JSON for every tile")
    ap.add_argument("--today", help="Override today's date (YYYY-MM-DD), for tests")
    ap.add_argument("--max-minutes", type=float, help="Override overpass.max_minutes for this run (e.g. a longer first fill)")
    args = ap.parse_args(argv)

    cfg = config.load(args.config)
    p, op, o, u = cfg["paths"], cfg["overpass"], cfg["origin"], cfg["universe"]
    if args.max_minutes:
        op["max_minutes"] = args.max_minutes
    today = args.today or date.today().isoformat()
    started = time.monotonic()
    if args.fixture:
        payload = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
        fetch, sleep = (lambda _q: payload), (lambda _s: None)
    else:
        turn = itertools.count()

        def fetch(q):  # rotate the starting endpoint per tile to spread load across instances
            k = next(turn) % len(op["endpoints"])
            eps = op["endpoints"][k:] + op["endpoints"][:k]
            return discover_osm.overpass_fetch(q, eps, op["timeout_seconds"], op["retries"], log=log)
        sleep = time.sleep

    all_tiles = sorted(geo.tiles(o["lat"], o["lon"], u["radius_km"], op["tile_deg"]),  # nearest first
                       key=lambda t: geo.haversine_km(o["lat"], o["lon"], (t[0] + t[2]) / 2, (t[1] + t[3]) / 2))
    state = tiles.load(p["tile_state"])
    seen_before = tiles.covered_fn([discover_osm.TileResult(t, True) for t in all_tiles if tiles.key(t) in state])
    todo = tiles.choose(all_tiles, state, op["tiles_per_run"])[: args.max_tiles or None]
    log(f"{len(all_tiles)} tiles in the area, {sum(tiles.key(t) not in state for t in all_tiles)} never refreshed; "
        f"refreshing {len(todo)} this run (budget {op['max_minutes']} min)")
    props, results = discover_osm.discover(cfg, fetch, tile_list=todo, sleep=sleep, log=log,
                                           deadline=started + op["max_minutes"] * 60)
    ok = [t for t in results if t.ok]
    if not ok:
        log("No tile could be refreshed; keeping the existing catalog untouched.")
        return 1
    dts = None
    if args.fixture:
        log("Offline fixture run: drive times skipped.")
    elif props:
        log(f"routing drive times for {len(props)} properties")
        props, dts = drive_time.update(props, cfg, today, log=log)
    found = [x.row() for x in props]
    previous = cat.read(p["osm_catalog"])
    rows, ch = cat.merge(previous, found, today, covered=tiles.covered_fn(results))
    cat.write(rows, p["osm_catalog"])
    tiles.record(state, results, datetime.now(timezone.utc).isoformat(timespec="seconds"))
    tiles.save(state, p["tile_state"])
    failed = [t for t in results if not t.ok]
    never_left = sum(tiles.key(t) not in state for t in all_tiles)
    tile_line = (f"Map tiles this run: {len(ok)} refreshed, {len(failed)} failed, {never_left} never refreshed yet "
                 f"(of {len(all_tiles)}); {sum(t.seconds for t in results):.0f}s querying.")
    watched = [r for r in rows if not r.get("gone_since") and r.get("kind") == "hotel"]

    out = Path(p["out_dir"])
    out.mkdir(parents=True, exist_ok=True)
    (out / "osm_summary.md").write_text(summary_md(rows, watched, dts, tile_line).replace(
        "## Catalog:", "## OpenStreetMap catalog:").replace(" watched", " starred hotels"), encoding="utf-8")
    print((out / "osm_summary.md").read_text(encoding="utf-8"), flush=True)
    log(f"new {len(ch.new)}, back {len(ch.returned)}, gone {len(ch.gone)}; total {time.monotonic() - started:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
