"""Type of place around each watched property (settings: places), for filters such as "mountain spa" or "old town":

    mountain     elevation >= mountain_min_elevation_m, or a ski area within ski_km
    ski          ski area (landuse=winter_sports) within ski_km
    sea / beach  coastline within sea_km / a beach within beach_km
    lake         shore of a named lake within lake_km
    city         a city (place=city) within city_km
    major_city   a city of at least major_city_population within major_city_km
    old_town     at least old_town_min historic sites with a Wikidata entry within old_town_radius_m, or the centre of
                 a UNESCO World Heritage site (heritage=1) within unesco_km
    vineyards    vineyards (landuse=vineyard) within vineyard_km
    thermal      a hot spring or a thermal bath within thermal_km
    countryside  none of the above

OpenStreetMap through Overpass: one query per batch of nearby watched properties, each with a small radius around it
(`around`), so only what is near the properties comes back. Elevation from the landscape file, or Open Topo Data for
properties it doesn't have yet. Rolling: never processed first, then older than refresh_days.

    python -m hotel_scraper.places [--max-minutes 10]
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

from . import config, discover_osm, geo
from .landscape import elevations
from .scan import read_csv, write_csv

FIELDS = ["pid", "lat", "lon", "elevation_m", "sea_km", "beach_km", "lake_km", "ski_km", "city_km", "major_city_km",
          "vineyard_km", "historic_n", "unesco_km", "thermal_km", "tags", "updated"]
THERMAL_NAME = re.compile(r"therm|terme|toplice|fürd|furdo|lázn|lazne|bagni|kúpe|kupe", re.I)
TAG_ORDER = ["mountain", "ski", "sea", "beach", "lake", "old_town", "major_city", "city", "vineyards", "thermal"]


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%H:%M:%S} {msg}", flush=True)


def _m(km: float) -> int:
    return int(round(km * 1000))


def query(points: list[tuple[float, float]], pc: dict, timeout: int) -> str:
    """One Overpass query for a batch: outlines (coast, lakes) with geometry, everything else as centre points."""
    geom, centre = [], []
    city_m = _m(max(pc["city_km"], pc["major_city_km"]))
    for la, lo in points:
        a = f"{la:.5f},{lo:.5f}"
        geom += [f'way["natural"="coastline"](around:{_m(pc["sea_km"])},{a});',
                 f'way["natural"="water"]["water"="lake"]["name"](around:{_m(pc["lake_km"])},{a});',
                 f'relation["natural"="water"]["water"="lake"]["name"](around:{_m(pc["lake_km"])},{a});']
        centre += [f'nwr["natural"="beach"](around:{_m(pc["beach_km"])},{a});',
                   f'nwr["landuse"="winter_sports"](around:{_m(pc["ski_km"])},{a});',
                   f'node["place"="city"](around:{city_m},{a});',
                   f'nwr["landuse"="vineyard"](around:{_m(pc["vineyard_km"])},{a});',
                   f'nwr["historic"]["wikidata"](around:{pc["old_town_radius_m"]},{a});',
                   f'nwr["heritage"="1"](around:{_m(pc["unesco_km"])},{a});',
                   f'nwr["natural"="hot_spring"](around:{_m(pc["thermal_km"])},{a});',
                   f'nwr["amenity"="public_bath"](around:{_m(pc["thermal_km"])},{a});']
    return (f"[out:json][timeout:{timeout}];\n(\n" + "\n".join(geom) + "\n);\nout geom;\n(\n" + "\n".join(centre)
            + "\n);\nout center;")


def features(elements: list[dict], step: int = 3) -> dict[str, list]:
    """Overpass elements -> points per kind (cities as (lat, lon, population))."""
    out = {k: [] for k in ("sea", "lake", "beach", "ski", "city", "vineyard", "historic", "unesco", "thermal")}
    seen = set()

    def pts(geom):
        return [(g["lat"], g["lon"]) for g in (geom or [])[::step]]

    def centre(el):
        if "lat" in el:
            return el["lat"], el["lon"]
        c = el.get("center") or {}
        return (c.get("lat"), c.get("lon")) if c else (None, None)
    for el in elements:
        k = (el.get("type"), el.get("id"))
        if k in seen:      # the same object comes back once per property it is near
            continue
        seen.add(k)
        t = el.get("tags") or {}
        if t.get("natural") == "coastline":
            out["sea"] += pts(el.get("geometry"))
            continue
        if t.get("water") == "lake":
            for m in el.get("members") or [el]:    # a lake relation's outline is in its members
                out["lake"] += pts(m.get("geometry"))
            continue
        la, lo = centre(el)
        if la is None:
            continue
        if t.get("natural") == "beach":
            out["beach"].append((la, lo))
        elif t.get("landuse") == "winter_sports":
            out["ski"].append((la, lo))
        elif t.get("place") == "city":
            try:
                pop = int(re.sub(r"[^\d]", "", t.get("population", "")) or 0)
            except ValueError:
                pop = 0
            out["city"].append((la, lo, pop))
        elif t.get("landuse") == "vineyard":
            out["vineyard"].append((la, lo))
        elif t.get("natural") == "hot_spring" or (t.get("amenity") == "public_bath" and (
                t.get("bath:type") == "thermal" or THERMAL_NAME.search(t.get("name", "")))):
            out["thermal"].append((la, lo))
        if t.get("heritage") == "1":
            out["unesco"].append((la, lo))
        if t.get("historic") and t.get("wikidata"):
            out["historic"].append((la, lo))
    return out


def _nearest(la, lo, pts, max_km):
    best = None
    for p in pts:
        d = geo.haversine_km(la, lo, p[0], p[1])
        if d <= max_km and (best is None or d < best):
            best = d
    return "" if best is None else round(best, 2)


def measure(la: float, lo: float, f: dict, pc: dict) -> dict:
    big = [c for c in f["city"] if c[2] >= pc["major_city_population"]]
    return {"sea_km": _nearest(la, lo, f["sea"], pc["sea_km"]), "beach_km": _nearest(la, lo, f["beach"], pc["beach_km"]),
            "lake_km": _nearest(la, lo, f["lake"], pc["lake_km"]), "ski_km": _nearest(la, lo, f["ski"], pc["ski_km"]),
            "city_km": _nearest(la, lo, f["city"], pc["city_km"]),
            "major_city_km": _nearest(la, lo, big, pc["major_city_km"]),
            "vineyard_km": _nearest(la, lo, f["vineyard"], pc["vineyard_km"]),
            "historic_n": sum(1 for p in f["historic"] if geo.haversine_km(la, lo, p[0], p[1]) * 1000 <= pc["old_town_radius_m"]),
            "unesco_km": _nearest(la, lo, f["unesco"], pc["unesco_km"]),
            "thermal_km": _nearest(la, lo, f["thermal"], pc["thermal_km"])}


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


def batches(rows: list[dict], size: int) -> list[list[dict]]:
    """Nearby properties together (0.5-degree cells, west to east), so each query covers one small area."""
    rows = sorted(rows, key=lambda r: (int(float(r["lat"]) * 2), float(r["lon"])))
    return [rows[i:i + size] for i in range(0, len(rows), size)]


def due(watch: list[dict], known: dict, today: date, refresh_days: int) -> list[dict]:
    out = []
    for w in watch:
        k = known.get(w["pid"], {})
        if not k.get("updated"):
            out.append(w)
        elif (today - date.fromisoformat(k["updated"][:10])).days >= refresh_days:
            out.append(w)
    return out


def run(cfg: dict, max_minutes: float, fetch=None, elev=None, today: date | None = None, log=log,
        clock=time.monotonic, sample: Path | None = None, sleep=time.sleep) -> dict:
    pc, p, op, lc = cfg["places"], cfg["paths"], cfg["overpass"], cfg["landscape"]
    today = today or date.today()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    watch = [w for w in read_csv(p["watchlist"]) if not w.get("gone_since") and w.get("lat")]
    known = {r["pid"]: r for r in read_csv(p["places"])}
    land = {r["pid"]: r for r in read_csv(p["landscape"])}
    todo = due(watch, known, today, pc["refresh_days"])
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
    fetch = fetch or (lambda q: discover_osm.overpass_fetch(q, op["endpoints"], op["timeout_seconds"], op["retries"], log=log))
    started, done, failed = clock(), 0, 0
    for b in batches(todo, pc["batch"]):
        if clock() - started > max_minutes * 60:
            break
        pts = [(float(w["lat"]), float(w["lon"])) for w in b]
        try:
            data = fetch(query(pts, pc, op["timeout_seconds"]))
        except Exception as e:  # noqa: BLE001
            failed += 1
            log(f"batch of {len(b)} near {pts[0]}: {str(e)[:120]}")
            continue
        if sample and not sample.exists():   # one real response per run, for the parser's regression test
            sample.parent.mkdir(parents=True, exist_ok=True)
            sample.write_text(json.dumps({"points": pts[:3], "elements": [e for e in data.get("elements", [])
                                          if not e.get("geometry")][:400]}), encoding="utf-8")
        f = features(data.get("elements", []), lc["line_step"])
        for w, (la, lo) in zip(b, pts):
            r = {"pid": w["pid"], "lat": w["lat"], "lon": w["lon"],
                 "elevation_m": land.get(w["pid"], {}).get("elevation_m") or elev_of.get(w["pid"])
                 or known.get(w["pid"], {}).get("elevation_m", ""),
                 **measure(la, lo, f, pc), "updated": now}
            r["tags"] = tags_for(r, pc)
            known[w["pid"]] = r
        done += len(b)
        log(f"batch {len(b)} near {pts[0][0]:.2f},{pts[0][1]:.2f}: {len(data.get('elements', []))} elements")
        sleep(random.uniform(*op["pause_seconds"]))
    write_csv(sorted(known.values(), key=lambda r: r["pid"]), p["places"], FIELDS)
    from collections import Counter
    counts = Counter(t for w in watch for t in (known.get(w["pid"], {}).get("tags") or "").split(",") if t)
    return {"due": len(todo), "done": done, "failed_batches": failed,
            "coverage": f"{sum(1 for w in watch if known.get(w['pid'], {}).get('updated'))}/{len(watch)}",
            "tags": dict(counts.most_common())}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(config.DEFAULT_PATH))
    ap.add_argument("--max-minutes", type=float)
    args = ap.parse_args(argv)
    cfg = config.load(args.config)
    out = Path(cfg["paths"]["out_dir"])
    res = run(cfg, args.max_minutes or cfg["places"]["max_minutes"], sample=out / "places_sample.json")
    out.mkdir(parents=True, exist_ok=True)
    md = (f"## Type of place\n\nProcessed {res['done']} of {res['due']} due ({res['failed_batches']} batches failed); "
          f"coverage {res['coverage']}.\n\nTags: " + ", ".join(f"{k} {v}" for k, v in res["tags"].items()) + "\n")
    (out / "places_summary.md").write_text(md, encoding="utf-8")
    print(md, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
