"""Live property source: OpenStreetMap via Overpass (free, ODbL).

Property types, OSM tags, star rules, radius, origin and endpoints all come from config/settings.yaml.
Each tile is one Overpass query covering every configured type.
"""
from __future__ import annotations

import json
import random
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import geo

# "4", "4S", "4 S", "4 sup", "4 superior", "4+", "4.5", "4,5". Rejects "45", "4-5", "40".
STARS_RE = re.compile(r"^\s*([1-5])(?:\s*(?:s|sup|superior|\+)|[.,]5)?\s*$", re.IGNORECASE)

FIELDS = ["pid", "kind", "name", "stars", "stars_raw", "lat", "lon", "distance_km", "drive_hours", "road_km",
          "country", "city", "website", "wikidata"]


@dataclass
class Property:
    pid: str          # stable id, e.g. "osm:way/123"
    kind: str         # key of universe.property_types
    name: str
    stars: int | None
    stars_raw: str
    lat: float
    lon: float
    distance_km: float          # straight line, used only as a pre-filter
    drive_hours: float | str = ""   # filled by drive_time.update ("" = not routed yet / unroutable)
    road_km: float | str = ""
    country: str = ""
    city: str = ""
    website: str = ""
    wikidata: str = ""

    def row(self) -> dict:
        return {f: ("" if getattr(self, f) is None else getattr(self, f)) for f in FIELDS}


def parse_stars(raw: str | None) -> int | None:
    m = STARS_RE.match(raw or "")
    return int(m.group(1)) if m else None


def _filter(tags: dict, extra: str = "") -> str:
    parts = []
    for k, v in tags.items():
        k = str(k).replace('"', '\\"')
        parts.append(f'["{k}"]' if v == "*" else f'["{k}"="{str(v).replace(chr(34), chr(92) + chr(34))}"]')
    return "".join(parts) + extra


def build_query(types: dict, bbox: tuple, timeout: int) -> str:
    s, w, n, e = bbox
    lines = []
    for spec in types.values():
        stars_needed = '["stars"]' if spec.get("stars") else ""  # skip unrated objects server-side when stars are required
        for tags in spec["osm_tags"]:
            lines.append(f"  nwr{_filter(tags, stars_needed)}({s},{w},{n},{e});")
    return f"[out:json][timeout:{timeout}];\n(\n" + "\n".join(lines) + "\n);\nout center tags;"


def classify(tags: dict, types: dict) -> str | None:
    for kind, spec in types.items():
        for cond in spec["osm_tags"]:
            if all((k in tags) if v == "*" else tags.get(k) == str(v) for k, v in cond.items()):
                return kind
    return None


def parse_elements(elements: Iterable[dict], origin_lat: float, origin_lon: float, radius_km: float,
                   types: dict) -> list[Property]:
    out = []
    for el in elements:
        tags = el.get("tags") or {}
        name = (tags.get("name") or "").strip()
        kind = classify(tags, types)
        if not name or not kind:
            continue
        stars = parse_stars(tags.get("stars"))
        allowed = types[kind].get("stars") or []
        if allowed and stars not in allowed:
            continue
        if "lat" in el:
            lat, lon = el["lat"], el["lon"]
        elif "center" in el:
            lat, lon = el["center"]["lat"], el["center"]["lon"]
        else:
            continue
        dist = geo.haversine_km(origin_lat, origin_lon, lat, lon)
        if dist > radius_km:
            continue
        out.append(Property(
            pid=f"osm:{el['type']}/{el['id']}", kind=kind, name=name, stars=stars, stars_raw=tags.get("stars", ""),
            lat=round(lat, 6), lon=round(lon, 6), distance_km=round(dist, 1),
            country=(tags.get("addr:country") or "").upper(), city=tags.get("addr:city") or "",
            website=tags.get("website") or tags.get("contact:website") or "", wikidata=tags.get("wikidata") or "",
        ))
    return out


def _norm_name(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(hotel|resort|spa|the|b&b|bed and breakfast|&|and)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def dedupe(props: list[Property], max_km: float = 0.2) -> list[Property]:
    """One row per property. The same place is often mapped twice (node + building outline):
    same normalised name within max_km -> keep the outline, borrow missing tags from the twin."""
    order = {"relation": 0, "way": 1, "node": 2}
    props = sorted(props, key=lambda p: (order.get(p.pid.split(":")[1].split("/")[0], 3), p.pid))
    seen, kept, by_name = set(), [], {}
    for p in props:
        if p.pid in seen:
            continue
        seen.add(p.pid)
        key = _norm_name(p.name)
        twin = next((o for o in by_name.get(key, []) if geo.haversine_km(p.lat, p.lon, o.lat, o.lon) <= max_km), None)
        if twin:
            for f in ("website", "wikidata", "city", "country"):
                if not getattr(twin, f) and getattr(p, f):
                    setattr(twin, f, getattr(p, f))
            continue
        by_name.setdefault(key, []).append(p)
        kept.append(p)
    return kept


VATICAN = (41.900, 12.445, 41.908, 12.459)


def fix_country(cc: str, lat: float, lon: float) -> str:
    """The offline lookup picks the nearest town, which labels central Rome as Vatican City."""
    if cc == "VA" and not (VATICAN[0] <= lat <= VATICAN[2] and VATICAN[1] <= lon <= VATICAN[3]):
        return "IT"
    return cc


def fix_town(name: str, cc: str, lat: float, lon: float) -> str:
    """Nearest-town lookups name central Rome "Vatican City"."""
    return "Rome" if cc == "VA" and fix_country(cc, lat, lon) == "IT" else name


def add_places(props: list[Property]) -> None:
    """Fill missing country codes and towns offline (GeoNames via reverse_geocoder); skip if unavailable."""
    missing = [p for p in props if not p.country or not p.city]
    if not missing:
        return
    try:
        import reverse_geocoder as rg
    except ImportError:
        return
    for p, r in zip(missing, rg.search([(p.lat, p.lon) for p in missing], mode=1, verbose=False)):
        p.country = p.country or fix_country(r.get("cc", ""), p.lat, p.lon)
        p.city = p.city or fix_town(r.get("name", ""), r.get("cc", ""), p.lat, p.lon)


def check_overpass(data: dict) -> dict:
    """Overpass answers HTTP 200 with partial data and a 'remark' when a query times out or runs out of memory."""
    remark = data.get("remark") or ""
    if "error" in remark.lower():
        raise RuntimeError(f"Overpass remark: {remark[:200]}")
    return data


def overpass_fetch(query: str, endpoints: list[str], timeout: int, retries: int,
                   sleep: Callable[[float], None] = time.sleep, log=print) -> dict:
    last_err = None
    for attempt in range(retries):
        for url in endpoints:
            t0 = time.monotonic()
            try:
                req = Request(url, data=urlencode({"data": query}).encode(),
                              headers={"User-Agent": "hotel-deal-radar/0.3 (personal project)"})
                with urlopen(req, timeout=timeout + 30) as r:
                    return check_overpass(json.load(r))
            except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError, RuntimeError) as e:
                last_err = e
                log(f"  overpass attempt {attempt + 1} via {url.split('/')[2]} failed after {time.monotonic() - t0:.0f}s: {e}")
                sleep(min(60, 5 * 2 ** attempt) + random.uniform(0, 2))
    raise RuntimeError(f"Overpass failed after {retries} rounds: {last_err}")


@dataclass
class TileResult:
    bbox: tuple
    ok: bool
    elements: int = 0
    kept: int = 0
    seconds: float = 0.0
    error: str = ""


def discover(cfg: dict, fetch: Callable[[str], dict], tile_list: list[tuple] | None = None,
             sleep: Callable[[float], None] = time.sleep, log=print, deadline: float | None = None,
             clock: Callable[[], float] = time.monotonic) -> tuple[list[Property], list[TileResult]]:
    """Query each tile; a failing tile is recorded and skipped, never fatal. Stops starting new tiles after `deadline`."""
    o, u, op = cfg["origin"], cfg["universe"], cfg["overpass"]
    types = u["property_types"]
    if tile_list is None:
        tile_list = geo.tiles(o["lat"], o["lon"], u["radius_km"], op["tile_deg"])
    found: list[Property] = []
    results: list[TileResult] = []
    for i, bbox in enumerate(tile_list, 1):
        if deadline is not None and clock() > deadline:
            log(f"time budget reached after {i - 1} tiles; the rest wait for the next run")
            break
        t0 = clock()
        try:
            data = fetch(build_query(types, bbox, op["timeout_seconds"]))
        except Exception as e:  # noqa: BLE001
            results.append(TileResult(bbox, False, seconds=round(clock() - t0, 1), error=str(e)[:300]))
            log(f"tile {i}/{len(tile_list)} {bbox}: FAILED after {clock() - t0:.0f}s: {str(e)[:200]}")
            continue
        elements = data.get("elements", [])
        batch = parse_elements(elements, o["lat"], o["lon"], u["radius_km"], types)
        found.extend(batch)
        results.append(TileResult(bbox, True, len(elements), len(batch), round(clock() - t0, 1)))
        log(f"tile {i}/{len(tile_list)} {bbox}: {len(elements)} elements, {len(batch)} kept, {clock() - t0:.1f}s")
        if i < len(tile_list):
            sleep(random.uniform(*op["pause_seconds"]))
    props = dedupe(found)
    add_places(props)
    return props, results
