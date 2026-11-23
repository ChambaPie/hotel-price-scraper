"""Type of place around each watched property (settings: places), for filters such as "mountain spa" or "old town":

    mountain     elevation >= mountain_min_elevation_m, or a ski area within ski_km
    ski          ski area (landuse=winter_sports) within ski_km
    sea / beach  coastline within sea_km / a beach within beach_km
    lake         shore of a named lake within lake_km
    city         a city (place=city) within city_km
    major_city   a city of at least major_city_population within major_city_km
    old_town     at least old_town_min historic sites with a Wikidata entry within old_town_radius_m, or a UNESCO
                 World Heritage site (heritage=1) within unesco_km
    vineyards    vineyards (landuse=vineyard) within vineyard_km
    thermal      a hot spring or a thermal bath within thermal_km
    countryside  none of the above

Source: OpenStreetMap extracts from Geofabrik (download.geofabrik.de), read locally with pyosmium: for each watched
property the smallest extract that contains it (Italy north-east, Austria, ...), one download per extract, deleted
after use. The public Overpass servers were too overloaded for this (2026-11-23: every batch timed out).
Elevation from the landscape file, or Open Topo Data for properties it doesn't have. Rolling: never processed first,
then older than refresh_days.

    python -m hotel_scraper.places [--max-minutes 45]
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from . import config, geo
from .landscape import elevations
from .scan import read_csv, write_csv

FIELDS = ["pid", "lat", "lon", "elevation_m", "sea_km", "beach_km", "lake_km", "ski_km", "city_km", "major_city_km",
          "vineyard_km", "historic_n", "unesco_km", "thermal_km", "coast_m", "coast_side", "extract", "tags", "updated"]
THERMAL_NAME = re.compile(r"therm|terme|toplice|fürd|furdo|lázn|lazne|bagni|kúpe|kupe", re.I)
TAG_ORDER = ["mountain", "ski", "sea", "beach", "lake", "old_town", "major_city", "city", "vineyards", "thermal"]
KINDS = ("sea", "lake", "beach", "ski", "city", "vineyard", "historic", "unesco", "thermal")
UA = "hotel-deal-radar/0.8 (personal project)"


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%H:%M:%S} {msg}", flush=True)


# ---------- which extract holds each property (pure) ----------

def _in_ring(lat: float, lon: float, ring: list) -> bool:
    inside, j = False, len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def _contains(geom: dict, lat: float, lon: float) -> bool:
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    for poly in polys:
        if poly and _in_ring(lat, lon, poly[0]) and not any(_in_ring(lat, lon, h) for h in poly[1:]):
            return True
    return False


def extracts_from_index(index: dict) -> list[dict]:
    """Geofabrik index-v1.json -> [{id, url, bbox (w, s, e, n), area, geometry}], extracts with a .pbf only."""
    out = []
    for f in index.get("features", []):
        pr, g = f.get("properties") or {}, f.get("geometry")
        url = (pr.get("urls") or {}).get("pbf")
        if not url or not g or not pr.get("parent"):   # continents (no parent) are far too big to download
            continue
        pts = [c for poly in (g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]) for c in poly[0]]
        w, e = min(p[0] for p in pts), max(p[0] for p in pts)
        s, n = min(p[1] for p in pts), max(p[1] for p in pts)
        out.append({"id": pr["id"], "url": url, "bbox": (w, s, e, n), "area": (e - w) * (n - s), "geometry": g})
    return out


def choose_extracts(rows: list[dict], extracts: list[dict], skip: set[str] = frozenset()) -> dict[str, list[dict]]:
    """{extract id: properties}: for each property the smallest extract (by bounding box) that contains it."""
    by_size = sorted((x for x in extracts if x["id"] not in skip), key=lambda x: x["area"])
    groups: dict[str, list[dict]] = {}
    for r in rows:
        la, lo = float(r["lat"]), float(r["lon"])
        for x in by_size:
            w, s, e, n = x["bbox"]
            if w <= lo <= e and s <= la <= n and _contains(x["geometry"], la, lo):
                groups.setdefault(x["id"], []).append(r)
                break
    return groups


# ---------- what is around (pure) ----------

def classify(tags: dict) -> list[str]:
    """The feature kinds an OpenStreetMap object counts as (an object can be several, e.g. a UNESCO castle)."""
    t = tags
    kinds = []
    if t.get("natural") == "coastline":
        kinds.append("sea")
    if t.get("water") == "lake" and t.get("name"):
        kinds.append("lake")
    if t.get("natural") == "beach":
        kinds.append("beach")
    if t.get("landuse") == "winter_sports":
        kinds.append("ski")
    if t.get("place") == "city":
        kinds.append("city")
    if t.get("landuse") == "vineyard":
        kinds.append("vineyard")
    if t.get("natural") == "hot_spring" or (t.get("amenity") == "public_bath" and (
            t.get("bath:type") == "thermal" or THERMAL_NAME.search(t.get("name", "")))):
        kinds.append("thermal")
    if t.get("heritage") == "1":
        kinds.append("unesco")
    if t.get("historic") and (t.get("wikidata") or t.get("wikipedia")):
        kinds.append("historic")
    return kinds


def population(tags: dict) -> int:
    try:
        return int(re.sub(r"[^\d]", "", tags.get("population", "")) or 0)
    except ValueError:
        return 0


class Grid:
    """Points on a 0.05-degree grid (4-5 km cells): nearest distance and count within a radius."""
    def __init__(self, points, cell: float = 0.05):
        self.cell, self.cells = cell, {}
        for p in points:
            self.cells.setdefault((int(p[0] // cell), int(p[1] // cell)), []).append(p)

    def _near(self, lat, lon, km):
        r = int(km / (111 * self.cell)) + 1
        ci, cj = int(lat // self.cell), int(lon // self.cell)
        for i in range(ci - r, ci + r + 1):
            for j in range(cj - r, cj + r + 1):
                yield from self.cells.get((i, j), [])

    def nearest_km(self, lat, lon, max_km):
        best = None
        for p in self._near(lat, lon, max_km):
            d = geo.haversine_km(lat, lon, p[0], p[1])
            if d <= max_km and (best is None or d < best):
                best = d
        return "" if best is None else round(best, 2)

    def count_within(self, lat, lon, km):
        return sum(1 for p in self._near(lat, lon, km) if geo.haversine_km(lat, lon, p[0], p[1]) <= km)


class Coast:
    """Coastline segments in OpenStreetMap's direction (land on the left, water on the right): which side of the
    nearest coastline a position is on. Owner, 2026-11-23: hotels must not sit in the sea."""
    def __init__(self, lines, cell: float = 0.02):
        self.cell, self.cells = cell, {}
        for line in lines:
            for a, b in zip(line, line[1:]):
                for c in {self._key(*a), self._key(*b), self._key((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)}:
                    self.cells.setdefault(c, []).append((a, b))

    def _key(self, lat, lon):
        return int(lat // self.cell), int(lon // self.cell)

    def side(self, lat: float, lon: float, max_km: float) -> dict:
        """{"coast_m": metres to the nearest coastline, "coast_side": land | water | ""} ("" = none within max_km,
        or a corner where the two nearest segments disagree)."""
        import math
        r = int(max_km / (111 * self.cell)) + 1
        ci, cj = self._key(lat, lon)
        kx, ky = 111.32 * math.cos(math.radians(lat)), 110.57
        best, sides = None, set()
        for i in range(ci - r, ci + r + 1):
            for j in range(cj - r, cj + r + 1):
                for a, b in self.cells.get((i, j), []):
                    ax, ay, bx, by = (a[1] - lon) * kx, (a[0] - lat) * ky, (b[1] - lon) * kx, (b[0] - lat) * ky
                    dx, dy = bx - ax, by - ay
                    ll = dx * dx + dy * dy
                    t = 0.0 if ll == 0 else max(0.0, min(1.0, (-ax * dx - ay * dy) / ll))
                    d = math.hypot(ax + t * dx, ay + t * dy)
                    cross = dx * (0 - ay) - dy * (0 - ax)       # > 0: the position is left of a->b, on land
                    if best is None or d < best - 1e-6:
                        best, sides = d, {cross > 0}
                    elif abs(d - best) <= 1e-6:
                        sides.add(cross > 0)
        if best is None or best > max_km:
            return {"coast_m": "", "coast_side": ""}
        side = "" if len(sides) != 1 else ("land" if sides.pop() else "water")
        return {"coast_m": round(best * 1000), "coast_side": side}


def measure(la: float, lo: float, grids: dict, cities: list[tuple], pc: dict) -> dict:
    def near(points, km):
        best = None
        for p in points:
            d = geo.haversine_km(la, lo, p[0], p[1])
            if d <= km and (best is None or d < best):
                best = d
        return "" if best is None else round(best, 2)
    return {"sea_km": grids["sea"].nearest_km(la, lo, pc["sea_km"]),
            "beach_km": grids["beach"].nearest_km(la, lo, pc["beach_km"]),
            "lake_km": grids["lake"].nearest_km(la, lo, pc["lake_km"]),
            "ski_km": grids["ski"].nearest_km(la, lo, pc["ski_km"]),
            "city_km": near(cities, pc["city_km"]),
            "major_city_km": near([c for c in cities if c[2] >= pc["major_city_population"]], pc["major_city_km"]),
            "vineyard_km": grids["vineyard"].nearest_km(la, lo, pc["vineyard_km"]),
            "historic_n": grids["historic"].count_within(la, lo, pc["old_town_radius_m"] / 1000),
            "unesco_km": grids["unesco"].nearest_km(la, lo, pc["unesco_km"]),
            "thermal_km": grids["thermal"].nearest_km(la, lo, pc["thermal_km"])}


def tags_for(r: dict, pc: dict) -> str:
    def has(k):
        return r.get(k) not in ("", None)
    t = set()
    try:
        elev = float(r["elevation_m"]) if has("elevation_m") else None
    except ValueError:
        elev = None
    if (elev is not None and elev >= pc["mountain_min_elevation_m"]) or has("ski_km"):
        t.add("mountain")
    for tag, k in (("ski", "ski_km"), ("sea", "sea_km"), ("beach", "beach_km"), ("lake", "lake_km"),
                   ("city", "city_km"), ("major_city", "major_city_km"), ("vineyards", "vineyard_km"),
                   ("thermal", "thermal_km")):
        if has(k):
            t.add(tag)
    if int(r.get("historic_n") or 0) >= pc["old_town_min"] or has("unesco_km"):
        t.add("old_town")
    if "major_city" in t:
        t.add("city")
    if not t & {"mountain", "ski", "sea", "beach", "lake", "city", "old_town"}:
        t.add("countryside")
    return ",".join(x for x in TAG_ORDER + ["countryside"] if x in t)


# ---------- reading an extract (pyosmium) ----------

def read_features(path: str, bbox: tuple, step: int = 3) -> tuple[dict, list]:
    """Points per kind near the properties (bbox = s, w, n, e, already padded) and cities as (lat, lon, population).
    Coastlines and lake shores keep every `step`-th vertex; other areas and lines their first vertex."""
    import osmium
    from osmium.filter import KeyFilter, TagFilter
    s, w, n, e = bbox
    pts = {k: [] for k in KINDS}
    cities, coast = [], []

    def keep(lat, lon):
        return s <= lat <= n and w <= lon <= e

    def add(kinds, coords, tags):
        coords = [c for c in coords if keep(*c)]
        if not coords:
            return
        for k in kinds:
            if k == "city":
                cities.append((coords[0][0], coords[0][1], population(tags)))
            elif k in ("sea", "lake"):
                pts[k] += coords
            else:
                pts[k].append(coords[0])

    def loc(nd):
        return (nd.location.lat, nd.location.lon) if nd.location.valid() else None

    def handle(o, areas: bool):
        tags = dict(o.tags)
        kinds = classify(tags)
        if not kinds:
            return
        if o.is_node():
            if o.location.valid():
                add(kinds, [(o.location.lat, o.location.lon)], tags)
        elif o.is_way():
            if areas and o.is_closed() and "sea" not in kinds:
                return                     # its area comes separately
            add(kinds, [c for c in (loc(nd) for nd in list(o.nodes)[::step]) if c], tags)
            if tags.get("natural") == "coastline":   # in order, for the land/water side; split where it leaves the box
                nodes = list(o.nodes)
                part = []
                for nd in nodes[::step] + ([nodes[-1]] if (len(nodes) - 1) % step else []):
                    c = loc(nd)
                    if c and keep(*c):
                        part.append(c)
                    else:
                        if len(part) > 1:
                            coast.append(part)
                        part = []
                if len(part) > 1:
                    coast.append(part)
        elif o.is_area():
            ring = next(iter(o.outer_rings()), None)
            if ring is None:
                return
            nodes = [c for c in (loc(nd) for nd in list(ring)[:: step if "lake" in kinds else 10 ** 9]) if c]
            add(kinds, nodes, tags)

    main = TagFilter(("natural", "coastline"), ("natural", "water"), ("natural", "beach"), ("natural", "hot_spring"),
                     ("landuse", "winter_sports"), ("landuse", "vineyard"), ("place", "city"), ("heritage", "1"),
                     ("amenity", "public_bath"))
    for o in osmium.FileProcessor(path).with_locations().with_areas().with_filter(main):
        handle(o, True)
    for o in osmium.FileProcessor(path).with_locations().with_filter(KeyFilter("historic")):
        if not o.is_relation():
            handle(o, False)
    pts["_coast"] = coast                  # polylines, not points: run() builds a Coast from them
    return pts, cities


class _ipv4_only:
    """GitHub's runners sometimes have no IPv6 route ("Network is unreachable" on 2026-11-23 for Geofabrik and the
    main Overpass server): resolve to IPv4 addresses only while this is active."""
    def __enter__(self):
        import socket
        self._orig = socket.getaddrinfo
        socket.getaddrinfo = lambda *a, **k: [r for r in self._orig(*a, **k) if r[0] == socket.AF_INET] or self._orig(*a, **k)
        return self

    def __exit__(self, *exc):
        import socket
        socket.getaddrinfo = self._orig


def fetch_url(url: str, timeout: int = 120, tries: int = 3, log=log, sleep=time.sleep):
    """urlopen with retries; the second and later tries over IPv4 only."""
    last = None
    for i in range(tries):
        try:
            if i == 0:
                return urlopen(Request(url, headers={"User-Agent": UA}), timeout=timeout)
            with _ipv4_only():
                return urlopen(Request(url, headers={"User-Agent": UA}), timeout=timeout)
        except OSError as e:   # URLError, timeouts, unreachable networks
            last = e
            log(f"{url.split('/')[2]}: try {i + 1} failed: {e}")
            sleep(5 * (i + 1))
    raise RuntimeError(f"{url}: {last}")


def download(url: str, dest: Path, log=log) -> int:
    with fetch_url(url, log=log) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f, length=1 << 20)
    return dest.stat().st_size


# ---------- run ----------

def due(watch: list[dict], known: dict, today: date, refresh_days: int) -> list[dict]:
    """Never measured, measured at another position (the hotel's position was corrected), or older than refresh_days."""
    out = []
    for w in watch:
        k = known.get(w["pid"], {})
        if not k.get("updated") or (today - date.fromisoformat(k["updated"][:10])).days >= refresh_days \
                or (k.get("lat"), k.get("lon")) != (w["lat"], w["lon"]) or k.get("coast_m") is None:   # before 2026-11-23
            out.append(w)
    return out


def positions(cfg: dict) -> dict:
    from .locations import positions as _positions
    return _positions(cfg) if cfg["paths"].get("locations") else {}


def run(cfg: dict, max_minutes: float, index=None, read=read_features, fetch=download, elev=None,
        today: date | None = None, log=log, clock=time.monotonic, workdir: Path | None = None) -> dict:
    pc, p, lc = cfg["places"], cfg["paths"], cfg["landscape"]
    today = today or date.today()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    pos = positions(cfg)   # the checked position of each hotel (locations.py), else the listed one
    watch = [{**w, **dict(zip(("lat", "lon"), pos.get(w["pid"], (w["lat"], w["lon"]))))}
             for w in read_csv(p["watchlist"]) if not w.get("gone_since") and w.get("lat")]
    known = {r["pid"]: r for r in read_csv(p["places"])}
    land = {r["pid"]: r for r in read_csv(p["landscape"])}
    todo = due(watch, known, today, pc["refresh_days"])
    started = clock()
    stats = {"due": len(todo), "done": 0, "extracts": 0, "failed_extracts": 0, "downloaded_mb": 0}
    if not todo:
        return {**stats, "coverage": f"{len(watch)}/{len(watch)}", "tags": {}}
    # elevation: from the landscape file, else Open Topo Data (within its daily budget)
    need = [w for w in todo if land.get(w["pid"], {}).get("elevation_m") in (None, "")
            and known.get(w["pid"], {}).get("elevation_m") in (None, "")][: pc["elevation_points_per_run"]]
    elev_of = {}
    if need:
        elev = elev or (lambda pts: elevations(pts, lc["elevation_dataset"], lc["elevation_api"]))
        try:
            for w, v in zip(need, elev([(float(w["lat"]), float(w["lon"])) for w in need])):
                elev_of[w["pid"]] = "" if v is None else round(v)
        except Exception as e:  # noqa: BLE001
            log(f"elevation lookup stopped: {e}")
    if index is None:
        try:
            with fetch_url(pc["extracts_index"], log=log) as r:
                index = json.load(r)
        except RuntimeError as e:
            log(f"extract index not reachable: {e}")
            return {**stats, "failed_extracts": -1, "coverage": f"{len(watch) - len(todo)}/{len(watch)}", "tags": {}}
    extracts = extracts_from_index(index)
    groups = choose_extracts(todo, extracts, set(pc.get("skip_extracts") or []))
    urls = {x["id"]: x["url"] for x in extracts}
    workdir = Path(workdir or tempfile.mkdtemp(prefix="osm-"))
    pad = pc["pad_km"] / 111
    for xid, rows in sorted(groups.items(), key=lambda kv: -len(kv[1])):   # most properties first
        if clock() - started > max_minutes * 60:
            break
        lats, lons = [float(r["lat"]) for r in rows], [float(r["lon"]) for r in rows]
        bbox = (min(lats) - pad, min(lons) - pad * 1.5, max(lats) + pad, max(lons) + pad * 1.5)
        dest = workdir / f"{xid.replace('/', '_')}.osm.pbf"
        t0 = clock()
        try:
            size = fetch(urls[xid], dest, log=log)
            stats["downloaded_mb"] += round(size / 1e6)
            pts, cities = read(str(dest), bbox, lc["line_step"])
        except Exception as e:  # noqa: BLE001  (one extract failing must not stop the others)
            stats["failed_extracts"] += 1
            log(f"{xid}: {str(e)[:150]}")
            continue
        finally:
            dest.unlink(missing_ok=True)
        coast = Coast(pts.pop("_coast", []))
        grids = {k: Grid(v) for k, v in pts.items()}
        for w in rows:
            la, lo = float(w["lat"]), float(w["lon"])
            r = {"pid": w["pid"], "lat": w["lat"], "lon": w["lon"],
                 "elevation_m": land.get(w["pid"], {}).get("elevation_m") or elev_of.get(w["pid"])
                 or known.get(w["pid"], {}).get("elevation_m", ""),
                 **measure(la, lo, grids, cities, pc), **coast.side(la, lo, pc["coast_km"]), "extract": xid,
                 "updated": now}
            r["tags"] = tags_for(r, pc)
            known[w["pid"]] = r
        stats["done"] += len(rows)
        stats["extracts"] += 1
        log(f"{xid}: {len(rows)} properties, {sum(len(v) for v in pts.values())} feature points, "
            f"{len(cities)} cities, {round(clock() - t0)} s")
    write_csv(sorted(known.values(), key=lambda r: r["pid"]), p["places"], FIELDS)
    from collections import Counter
    counts = Counter(t for w in watch for t in (known.get(w["pid"], {}).get("tags") or "").split(",") if t)
    left = sum(1 for w in watch if not known.get(w["pid"], {}).get("updated"))
    stats.update(coverage=f"{len(watch) - left}/{len(watch)}", tags=dict(counts.most_common()),
                 not_in_any_extract=len(todo) - sum(len(v) for v in groups.values()))
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(config.DEFAULT_PATH))
    ap.add_argument("--max-minutes", type=float)
    args = ap.parse_args(argv)
    cfg = config.load(args.config)
    out = Path(cfg["paths"]["out_dir"])
    res = run(cfg, args.max_minutes or cfg["places"]["max_minutes"])
    out.mkdir(parents=True, exist_ok=True)
    md = (f"## Type of place\n\nProcessed {res['done']} of {res['due']} due from {res['extracts']} OpenStreetMap "
          f"extracts ({res['downloaded_mb']} MB, {res['failed_extracts']} failed); coverage {res['coverage']}.\n\nTags: "
          + ", ".join(f"{k} {v}" for k, v in res["tags"].items()) + "\n")
    (out / "places_summary.md").write_text(md, encoding="utf-8")
    print(md, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
