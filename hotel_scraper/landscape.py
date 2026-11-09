"""Landscape tags for every catalog property, for filtering (settings: landscape).

    mountain     elevation >= mountain_min_elevation_m, or a ski area within ski_km
    ski          ski area (OSM landuse=winter_sports) within ski_km
    sea          coastline within sea_km
    beach        beach (OSM natural=beach) within beach_km
    lake         shore of a named lake within lake_km
    city         a city (OSM place=city) within city_km
    countryside  none of the above

Elevation: Open Topo Data public API (EU-DEM 25 m; max 100 points per call, 1 call/s, 1,000 calls/day).
Map features: Overpass, one map tile at a time (rotation like the catalog refresh), features never stored, only the
distances per property in paths.landscape.

    python -m hotel_scraper.landscape [--max-minutes 15]
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import catalog as cat
from . import config, discover_osm, geo, tiles
from .scan import read_csv, write_csv

FIELDS = ["pid", "lat", "lon", "elevation_m", "sea_km", "beach_km", "lake_km", "ski_km", "city_km", "tags", "updated"]
FEATURE_QUERY = """[out:json][timeout:{timeout}];
(
  way["natural"="coastline"]({bbox});
  way["natural"="water"]["water"="lake"]["name"]({bbox});
  relation["natural"="water"]["water"="lake"]["name"]({bbox});
);
out geom;
(
  nwr["natural"="beach"]({bbox});
  nwr["landuse"="winter_sports"]({bbox});
  node["place"="city"]({bbox});
);
out center;"""


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%H:%M:%S} {msg}", flush=True)


# ---------- pure functions (unit-tested) ----------

def features_from_overpass(elements: list[dict], step: int = 3) -> dict[str, list[tuple[float, float]]]:
    """Overpass elements -> points per feature type. Lines and outlines are sampled every `step` nodes."""
    out = {"sea": [], "lake": [], "beach": [], "ski": [], "city": []}

    def pts(geom):
        return [(g["lat"], g["lon"]) for g in (geom or [])[::step]]
    for el in elements:
        t = el.get("tags") or {}
        if t.get("natural") == "coastline":
            out["sea"] += pts(el.get("geometry"))
        elif t.get("water") == "lake":
            if el["type"] == "relation":
                for m in el.get("members") or []:
                    out["lake"] += pts(m.get("geometry"))
            else:
                out["lake"] += pts(el.get("geometry"))
        else:
            c = (el.get("lat"), el.get("lon")) if "lat" in el else ((el.get("center") or {}).get("lat"), (el.get("center") or {}).get("lon"))
            if c[0] is None:
                continue
            if t.get("natural") == "beach":
                out["beach"].append(c)
            elif t.get("landuse") == "winter_sports":
                out["ski"].append(c)
            elif t.get("place") == "city":
                out["city"].append(c)
    return out


class Grid:
    """Nearest-point lookup on a 0.05-degree grid (about 4-5 km cells)."""
    def __init__(self, points: list[tuple[float, float]], cell: float = 0.05):
        self.cell, self.cells = cell, {}
        for la, lo in points:
            self.cells.setdefault((int(la / cell), int(lo / cell)), []).append((la, lo))

    def nearest_km(self, lat: float, lon: float, max_km: float) -> float | None:
        r = int(max_km / (111 * self.cell)) + 1
        ci, cj = int(lat / self.cell), int(lon / self.cell)
        best = None
        for i in range(ci - r, ci + r + 1):
            for j in range(cj - r, cj + r + 1):
                for la, lo in self.cells.get((i, j), []):
                    d = geo.haversine_km(lat, lon, la, lo)
                    if d <= max_km and (best is None or d < best):
                        best = d
        return round(best, 2) if best is not None else None


def tags_for(row: dict, lc: dict) -> str:
    def num(k):
        try:
            return float(row[k]) if row.get(k) not in ("", None) else None
        except ValueError:
            return None
    t = []
    elev, ski = num("elevation_m"), num("ski_km")
    if (elev is not None and elev >= lc["mountain_min_elevation_m"]) or (ski is not None and ski <= lc["ski_km"]):
        t.append("mountain")
    for tag, key in (("ski", "ski_km"), ("sea", "sea_km"), ("beach", "beach_km"), ("lake", "lake_km"), ("city", "city_km")):
        v = num(key)
        if v is not None and v <= lc[key]:
            t.append(tag)
    return ",".join(t) or "countryside"


# ---------- network ----------

def elevations(points: list[tuple[float, float]], dataset: str, base: str, sleep=time.sleep) -> list[float | None]:
    out = []
    for i in range(0, len(points), 100):
        chunk = points[i:i + 100]
        url = f"{base.rstrip('/')}/{dataset}?" + urlencode({"locations": "|".join(f"{a:.5f},{b:.5f}" for a, b in chunk)})
        with urlopen(Request(url, headers={"User-Agent": "hotel-deal-radar/0.6 (personal project)"}), timeout=60) as r:
            res = json.load(r).get("results") or []
        out += [x.get("elevation") for x in res] + [None] * (len(chunk) - len(res))
        sleep(1.2)
    return out


def run(cfg: dict, max_minutes: float, fetch=None, elev=None, log=log, clock=time.monotonic) -> dict:
    lc, p, o, op = cfg["landscape"], cfg["paths"], cfg["origin"], cfg["overpass"]
    started = clock()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    props = [r for r in cat.read(p["catalog"]) if not r.get("gone_since") and r.get("lat")]
    land = {r["pid"]: r for r in read_csv(p["landscape"])}
    for r in props:
        land.setdefault(r["pid"], {"pid": r["pid"]}).update(lat=r["lat"], lon=r["lon"])

    # 1. elevation for properties that don't have one (within the daily call budget)
    need = [pid for pid, r in land.items() if r.get("elevation_m") in (None, "")][: lc["elevation_calls_per_run"] * 100]
    if need:
        elev = elev or (lambda pts: elevations(pts, lc["elevation_dataset"], lc["elevation_api"]))
        try:
            vals = elev([(float(land[pid]["lat"]), float(land[pid]["lon"])) for pid in need])
            for pid, v in zip(need, vals):
                land[pid]["elevation_m"] = "" if v is None else round(v)
        except Exception as e:  # noqa: BLE001
            log(f"elevation lookup stopped: {e}")
    # 2. map features, tile by tile (oldest first), distances for the properties inside each tile
    live = fetch is None
    fetch = fetch or (lambda q: discover_osm.overpass_fetch(q, op["endpoints"], op["timeout_seconds"], op["retries"], log=log))
    all_tiles = sorted(geo.tiles(o["lat"], o["lon"], cfg["universe"]["radius_km"], op["tile_deg"]),
                       key=lambda t: geo.haversine_km(o["lat"], o["lon"], (t[0] + t[2]) / 2, (t[1] + t[3]) / 2))
    state = tiles.load(p["landscape_tiles"])
    done = 0
    pad = lc["tile_padding_deg"]
    for t in tiles.choose(all_tiles, state, lc["tiles_per_run"]):
        if clock() - started > max_minutes * 60:
            break
        s, w, n, e = t
        inside = [r for r in land.values() if r.get("lat") and s <= float(r["lat"]) <= n and w <= float(r["lon"]) <= e]
        if not inside:
            state[tiles.key(t)] = {"done": now, "elements": 0, "kept": 0, "seconds": 0}
            continue
        t0 = clock()
        try:
            data = fetch(FEATURE_QUERY.format(timeout=op["timeout_seconds"], bbox=f"{s - pad},{w - pad},{n + pad},{e + pad}"))
        except Exception as ex:  # noqa: BLE001
            log(f"tile {t}: features failed: {str(ex)[:120]}")
            continue
        feats = features_from_overpass(data.get("elements", []), lc["line_step"])
        grids = {k: Grid(v) for k, v in feats.items()}
        for r in inside:
            la, lo = float(r["lat"]), float(r["lon"])
            for key, ft in (("sea_km", "sea"), ("beach_km", "beach"), ("lake_km", "lake"), ("ski_km", "ski"), ("city_km", "city")):
                d = grids[ft].nearest_km(la, lo, lc["search_km"])
                r[key] = "" if d is None else d
            r["updated"] = now
        state[tiles.key(t)] = {"done": now, "elements": len(data.get("elements", [])), "kept": len(inside),
                               "seconds": round(clock() - t0, 1)}
        done += 1
        log(f"tile {t}: {len(inside)} properties, {sum(len(v) for v in feats.values())} feature points")
        if live:
            time.sleep(random.uniform(*op["pause_seconds"]))
    for r in land.values():
        r["tags"] = tags_for(r, lc)
    write_csv(sorted(land.values(), key=lambda r: r["pid"]), p["landscape"], FIELDS)
    tiles.save(state, p["landscape_tiles"])
    left = sum(1 for t in all_tiles if tiles.key(t) not in state)
    from collections import Counter
    counts = Counter(tag for r in land.values() for tag in r["tags"].split(","))
    return {"tiles_done": done, "tiles_never": left, "elevations": sum(1 for r in land.values() if r.get("elevation_m") not in ("", None)),
            "properties": len(land), "tags": dict(counts.most_common())}


def apply_tags(rows: list[dict], landscape_path) -> None:
    """Copy landscape tags onto catalog rows (column 'landscape')."""
    tags = {r["pid"]: r.get("tags", "") for r in read_csv(landscape_path)}
    for r in rows:
        if r["pid"] in tags:
            r["landscape"] = tags[r["pid"]]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(config.DEFAULT_PATH))
    ap.add_argument("--max-minutes", type=float, default=15)
    args = ap.parse_args(argv)
    cfg = config.load(args.config)
    res = run(cfg, args.max_minutes)
    out = Path(cfg["paths"]["out_dir"])
    out.mkdir(parents=True, exist_ok=True)
    md = (f"## Landscape tags\n\nTiles this run: {res['tiles_done']}; never processed: {res['tiles_never']}. "
          f"Elevations: {res['elevations']} of {res['properties']}.\n\nTags: " +
          ", ".join(f"{k} {v}" for k, v in res["tags"].items()) + "\n")
    (out / "landscape_summary.md").write_text(md, encoding="utf-8")
    print(md, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
