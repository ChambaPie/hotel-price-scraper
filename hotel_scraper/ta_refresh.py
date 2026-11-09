"""Daily catalog refresh from TripAdvisor listings (via Xotelo): the properties that can actually be priced.

    seed keys (countries, settings) --list--> city keys of properties inside the radius
    city keys --list, paged--> properties --> drive time (OSRM, cached) --> official stars from the OSM catalog
    --> merge into data/catalog.csv --> data/watchlist.csv (+ change report for the daily issue)

Rotation: never-listed city keys first (nearest first), then the oldest; capped per run by
tripadvisor.max_requests_per_run and tripadvisor.max_minutes. A city whose listing fails or is cut short is never
used to mark properties as gone.

    python -m hotel_scraper.ta_refresh
    python -m hotel_scraper.ta_refresh --fixture tests/fixtures/xotelo_list_trieste.json   # offline
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from . import catalog as cat
from . import config, discover_osm, drive_time, geo, selection
from .discover_osm import _norm_name, add_places
from .xotelo import Result, Xotelo

FIELDS = ["pid", "kind", "name", "ta_type", "review_score", "review_count", "stars", "stars_source", "osm_pid",
          "breakfast_label", "lat", "lon", "distance_km", "drive_hours", "road_km", "country", "city",
          "list_key", "url"]


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%H:%M:%S} {msg}", flush=True)


@dataclass
class TAProperty:
    pid: str
    kind: str
    name: str
    ta_type: str
    review_score: float | str
    review_count: int | str
    lat: float
    lon: float
    distance_km: float
    list_key: str
    url: str = ""
    breakfast_label: str = ""
    stars: int | str = ""
    stars_source: str = ""
    osm_pid: str = ""
    drive_hours: float | str = ""
    road_km: float | str = ""
    country: str = ""
    city: str = ""

    def row(self) -> dict:
        return {f: ("" if getattr(self, f) is None else getattr(self, f)) for f in FIELDS}


URL_IDS = re.compile(r"-g(\d+)-d(\d+)-")


def ids(item: dict) -> tuple[str, str] | None:
    """(city key, hotel key) of a listed property. Country-level lists prefix keys with the country
    ("g187768-d202858"), so the property's own city comes from its TripAdvisor URL ("...-g187849-d202858-...")."""
    m = URL_IDS.search(item.get("url") or "")
    if m:
        return f"g{m[1]}", f"g{m[1]}-d{m[2]}"
    parts = (item.get("key") or "").split("-")
    return (parts[0], item["key"]) if len(parts) == 2 else None


def kind_of(ta_type: str, types: dict) -> str | None:
    return next((k for k, names in types.items() if ta_type in names), None)


def to_property(item: dict, list_key: str, origin: tuple, radius_km: float, types: dict) -> TAProperty | None:
    g = item.get("geo") or {}
    kind = kind_of(item.get("accommodation_type", ""), types)
    k = ids(item)
    if not kind or not k or g.get("latitude") is None:
        return None
    dist = geo.haversine_km(origin[0], origin[1], g["latitude"], g["longitude"])
    if dist > radius_km:
        return None
    rs = item.get("review_summary") or {}
    return TAProperty(
        pid=f"ta:{k[1]}", kind=kind, name=item.get("name", "").strip(), ta_type=item.get("accommodation_type", ""),
        review_score=rs.get("rating", ""), review_count=rs.get("count", ""), lat=round(g["latitude"], 6),
        lon=round(g["longitude"], 6), distance_km=round(dist, 1), list_key=list_key, url=item.get("url", ""),
        breakfast_label="yes" if "Breakfast included" in (item.get("merchandising_labels") or []) else "")


def city_key(item: dict) -> str:
    k = ids(item)
    return k[0] if k else ""


# ---------- state: which TripAdvisor keys exist and when each was listed ----------

def load_state(path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"seeds": {}, "cities": {}}


def save_state(state: dict, path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    out = {"seeds": dict(sorted(state["seeds"].items())), "cities": dict(sorted(state["cities"].items()))}
    Path(path).write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")


def list_all(x: Xotelo, key: str, max_pages: int, page_size: int, sort: str, budget) -> tuple[list[dict], bool, str]:
    """All pages of one key. Returns (items, complete, error). complete=False if a page failed or a cap stopped it."""
    items, offset, total = [], 0, None
    for _ in range(max_pages):
        if not budget():
            return items, False, "run budget reached"
        r: Result = x.list(key, offset, page_size, sort)
        if not r.ok:
            return items, False, r.error
        page = r.data.get("list") or []
        total = r.data.get("total_count") or 0
        items.extend(page)
        offset += page_size
        if not page or offset >= total:
            return items, True, ""
    return items, (total is not None and offset >= total), "" if total is None or offset >= total else "page cap"


# ---------- official stars from the OpenStreetMap catalog ----------

def add_official_stars(props: list[TAProperty], osm_rows: list[dict], max_km: float, min_sim: float) -> int:
    grid: dict[tuple, list] = {}
    for r in osm_rows:
        if r.get("stars") and not r.get("gone_since"):
            lat, lon = float(r["lat"]), float(r["lon"])
            grid.setdefault((round(lat, 2), round(lon, 2)), []).append((lat, lon, _norm_name(r["name"]), r))
    matched = 0
    for p in props:
        best = None
        n = _norm_name(p.name)
        for dlat in (-0.01, 0, 0.01):
            for dlon in (-0.01, 0, 0.01):
                for lat, lon, on, r in grid.get((round(p.lat + dlat, 2), round(p.lon + dlon, 2)), []):
                    if geo.haversine_km(p.lat, p.lon, lat, lon) <= max_km:
                        sim = SequenceMatcher(None, n, on).ratio()
                        if sim >= min_sim and (best is None or sim > best[0]):
                            best = (sim, r)
        if best:
            p.stars, p.stars_source, p.osm_pid = int(best[1]["stars"]), "osm", best[1]["pid"]
            matched += 1
    return matched


# ---------- pipeline ----------

def run(cfg: dict, x: Xotelo, today: str, log=log, clock=time.monotonic, route=True) -> dict:
    t, p, o = cfg["tripadvisor"], cfg["paths"], cfg["origin"]
    origin, radius = (o["lat"], o["lon"]), cfg["universe"]["radius_km"]
    started, now_iso = clock(), datetime.now(timezone.utc).isoformat(timespec="seconds")
    state = load_state(p["ta_state"])
    listed_before = {k for k, v in state["cities"].items() if v.get("listed")}

    def budget() -> bool:
        return x.requests < t["max_requests_per_run"] and clock() - started < t["max_minutes"] * 60

    props: list[TAProperty] = []
    complete_keys = set()

    # 1. seeds: their (capped) country lists give properties right away and reveal the city keys (monthly)
    for seed in t["seed_keys"]:
        s = state["seeds"].get(seed, {})
        if s.get("listed") and (date.today() - date.fromisoformat(s["listed"][:10])).days < t["seed_refresh_days"]:
            continue
        if not budget():
            break
        items, complete, err = list_all(x, seed, t["max_pages_per_seed"], t["page_size"], t["sort"], budget)
        found = 0
        props.extend(pp for pp in (to_property(it, seed, origin, radius, t["types"]) for it in items) if pp)
        if complete:
            complete_keys.add(seed)
        for it in items:
            g = it.get("geo") or {}
            if g.get("latitude") is None or not city_key(it):
                continue
            d = geo.haversine_km(origin[0], origin[1], g["latitude"], g["longitude"])
            if d <= radius:
                found += 1
                c = state["cities"].setdefault(city_key(it), {"from": seed, "distance_km": round(d, 1)})
                c["distance_km"] = min(c.get("distance_km", d), round(d, 1))
        state["seeds"][seed] = {"listed": now_iso if items else s.get("listed", ""), "items": len(items),
                                "in_radius": found, "complete": complete, "error": err}
        log(f"seed {seed}: {len(items)} listed, {found} inside the radius" + (f" ({err})" if err else ""))

    # 2. cities: never listed (nearest first), then oldest
    cities = state["cities"]
    order = sorted(cities, key=lambda k: (bool(cities[k].get("listed")), cities[k].get("listed") or "",
                                          cities[k].get("distance_km", 1e9)))
    done = 0
    for key in order:
        if not budget():
            break
        items, complete, err = list_all(x, key, t["max_pages_per_city"], t["page_size"], t["sort"], budget)
        kept = [pp for pp in (to_property(it, key, origin, radius, t["types"]) for it in items) if pp]
        props.extend(kept)
        cities[key].update(items=len(items), kept=len(kept), complete=complete, error=err)
        if items and (complete or err == "page cap"):
            cities[key]["listed"] = now_iso
        if items and complete:
            complete_keys.add(key)
        done += 1
        log(f"city {key}: {len(items)} listed, {len(kept)} kept" + (f" ({err})" if err else ""))
    never_left = sum(1 for v in cities.values() if not v.get("listed"))

    # 3. dedupe (a property can appear under two keys), places, drive time, official stars
    uniq = {}
    for pp in props:  # city listings come after seeds and win (they can be complete, country lists can't)
        uniq[pp.pid] = pp
    props = list(uniq.values())
    add_places(props)
    dts = None
    if route and props:
        props, dts = drive_time.update(props, cfg, today, log=log)
    osm_rows = cat.read(p["osm_catalog"])
    m = t["stars_match"]
    star_matches = add_official_stars(props, osm_rows, m["max_km"], m["min_name_similarity"])

    # 4. merge, watchlist, reports
    previous = cat.read(p["catalog"])
    rows, ch = cat.merge(previous, [pp.row() for pp in props], today,
                         covered=lambda r: r.get("list_key") in complete_keys)
    if p.get("landscape"):
        from .landscape import apply_tags
        apply_tags(rows, p["landscape"])
    cat.write(rows, p["catalog"], fields=FIELDS)
    save_state(state, p["ta_state"])
    watched = selection.resolve_from_files(cfg)
    coverage = coverage_md(rows, osm_rows, cfg["universe"]["drive_time"]["max_hours"])
    news = cat.Changes(new=[r for r in ch.new if r.get("list_key") in listed_before], gone=ch.gone, returned=ch.returned)
    return {"rows": rows, "changes": ch, "news": news, "watched": watched, "drive": dts, "requests": x.requests,
            "cities_done": done, "cities_never_left": never_left, "cities_total": len(cities),
            "star_matches": star_matches, "seconds": round(clock() - started), "coverage": coverage}


def coverage_md(rows: list[dict], osm_rows: list[dict], max_hours: float, top: int = 40) -> str:
    """Official 4-5* hotels (OpenStreetMap) with no TripAdvisor match yet: shows which towns the catalog misses."""
    from collections import Counter
    matched = {r.get("osm_pid") for r in rows if r.get("osm_pid") and not r.get("gone_since")}

    def near(r):
        try:
            return float(r.get("drive_hours") or 0) <= max_hours
        except ValueError:
            return False
    starred = [r for r in osm_rows if r.get("kind") == "hotel" and r.get("stars") and not r.get("gone_since") and near(r)]
    missing = [r for r in starred if r["pid"] not in matched]
    towns = Counter((r.get("city") or "?", r.get("country") or "?") for r in missing)
    lines = [f"## Coverage: {len(starred) - len(missing)} of {len(starred)} official 4-5* hotels matched to TripAdvisor", "",
             "| Town | Country | Unmatched 4-5* hotels |", "|---|---|---|"]
    lines += [f"| {t} | {c} | {n} |" for (t, c), n in towns.most_common(top)]
    return "\n".join(lines) + "\n"


def summary_md(res: dict) -> str:
    rows, watched = res["rows"], res["watched"]
    active = [r for r in rows if not r.get("gone_since")]
    d = res["drive"]
    lines = [f"## Catalog: {len(active)} active properties, {len(watched)} watched", "",
             f"TripAdvisor keys this run: {res['cities_done']} listed, {res['cities_never_left']} never listed yet "
             f"(of {res['cities_total']}); {res['requests']} Xotelo requests; {res['seconds']} s.", "",
             f"Official stars matched from OpenStreetMap: {res['star_matches']} properties this run.", ""]
    if d:
        lines += [f"Drive times: {d.routed} routed now, {d.cached} from cache, {d.pending} pending, {d.unroutable} "
                  f"unroutable, {d.dropped} left out (over the limit)." + (f" Router error: {d.error}" if d.error else ""), ""]
    from collections import Counter
    reasons = Counter(r.get("watch_reason", "") for r in watched)
    lines += ["Watched by reason: " + ", ".join(f"{k}: {v}" for k, v in sorted(reasons.items())) +
              " (rule 1 = official 4-5*, rule 2 = hotel rated >= 4.5, rule 3 = B&B rated >= 4.5)", ""]
    kinds = sorted({r["kind"] for r in active})
    lines += ["| Country | " + " | ".join(kinds) + " | Watched |", "|---|" + "---|" * (len(kinds) + 1)]
    by = Counter((r.get("country") or "??", r["kind"]) for r in active)
    w = Counter(r.get("country") or "??" for r in watched)
    for cc in sorted({c for c, _ in by}, key=lambda c: -sum(by[(c, k)] for k in kinds)):
        lines.append(f"| {cc} | " + " | ".join(str(by[(cc, k)]) for k in kinds) + f" | {w[cc]} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    from .refresh import changes_md
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(config.DEFAULT_PATH))
    ap.add_argument("--fixture", help="Offline: answer every /list call with this JSON (no drive times)")
    ap.add_argument("--today")
    ap.add_argument("--max-minutes", type=float, help="Override tripadvisor.max_minutes for this run")
    ap.add_argument("--max-requests", type=int, help="Override tripadvisor.max_requests_per_run for this run")
    args = ap.parse_args(argv)
    cfg = config.load(args.config)
    t = cfg["tripadvisor"]
    if args.max_minutes:
        t["max_minutes"] = args.max_minutes
    if args.max_requests:
        t["max_requests_per_run"] = args.max_requests
    today = args.today or date.today().isoformat()
    if args.fixture:
        payload = json.loads(Path(args.fixture).read_text(encoding="utf-8"))

        class FakeSession:
            def get(self, url, headers=None):
                from .http import Response
                return Response(200, {}, json.dumps(payload).encode(), 0.0, url)
        x = Xotelo(t["base_url"], FakeSession(), pause_seconds=(0, 0), sleep=lambda s: None)
    else:
        x = Xotelo(t["base_url"], user_agent=t["user_agent"], pause_seconds=tuple(t["pause_seconds"]))
    res = run(cfg, x, today, route=not args.fixture)
    out = Path(cfg["paths"]["out_dir"])
    out.mkdir(parents=True, exist_ok=True)
    (out / "catalog_summary.md").write_text(summary_md(res), encoding="utf-8")
    (out / "coverage.md").write_text(res["coverage"], encoding="utf-8")
    changes = out / "catalog_changes.md"
    changes.unlink(missing_ok=True)
    if res["news"].any() and cfg["alerts"].get("catalog_changes_issue", True):
        changes.write_text(changes_md(res["news"], {r["pid"] for r in res["watched"]},
                                      cfg["alerts"]["max_listed_changes"], today), encoding="utf-8")
    print(summary_md(res), flush=True)
    log(f"new {len(res['changes'].new)} (reported {len(res['news'].new)}), back {len(res['changes'].returned)}, "
        f"gone {len(res['changes'].gone)}")
    return 0 if res["requests"] else 1


if __name__ == "__main__":
    sys.exit(main())
