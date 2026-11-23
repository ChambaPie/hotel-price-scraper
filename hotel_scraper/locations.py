"""Where each watched hotel really is (settings: locations). A small store, data/locations.csv, that every later run
and the front end read: new hotels are located once, known ones reused, and a hotel is looked at again only when
its listed position changes or after refresh_days (owner, 2026-11-23: "a small DB ... so that you don't always have
to refind the same location").

Independent sources, best first:
  manual   config/location_fixes.csv (pid, lat, lon, note): a position set by hand always wins
  osm      the hotel's own OpenStreetMap object: the one the catalog linked (osm_pid), else the same name nearby
  photon   OpenStreetMap search by name and town (Photon), for hotels the OpenStreetMap catalog doesn't hold
  listing  the position in the hotel list (TripAdvisor's listing)
status: confirmed (a second source agrees within agree_km), corrected (moved: see decide), manual, check (a same-name
place elsewhere: not moved, listed in the summary), listing only (no second source yet). The type-of-place run
measures which side of the coastline each position is on (places.csv coast_side); a listed position in the sea is
moved to its name match on the next run, or listed in the summary.

    python -m hotel_scraper.locations [--max-minutes 10]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from . import config, geo
from .discover_osm import _norm_name
from .http import Session
from .scan import read_csv, write_csv

FIELDS = ["pid", "name", "lat", "lon", "source", "status", "listing_lat", "listing_lon", "listing_off_km",
          "osm_lat", "osm_lon", "osm_name", "photon_lat", "photon_lon", "photon_name", "checked_at"]


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%H:%M:%S} {msg}", flush=True)


def similarity(a: str, b: str) -> float:
    """Name similarity for positions: whole-name likeness only. Containing the other name is not enough here
    ("Palazzo Veneziano" contains "Venezia", "Hotel Eden Roc Ascona" contains "Ascona": other places)."""
    from difflib import SequenceMatcher
    na, nb = _norm_name(a), _norm_name(b)
    if not na or not nb:
        return 0.0
    return 1.0 if na == nb else round(SequenceMatcher(None, na, nb).ratio(), 3)


class OsmIndex:
    """OpenStreetMap catalog rows on a 0.1-degree grid, for "the same name nearby"."""
    def __init__(self, rows: list[dict], cell: float = 0.1):
        self.cell, self.cells, self.by_pid = cell, {}, {}
        for r in rows:
            try:
                la, lo = float(r["lat"]), float(r["lon"])
            except (KeyError, ValueError):
                continue
            self.by_pid[r["pid"]] = r
            self.cells.setdefault((int(la // cell), int(lo // cell)), []).append((la, lo, r))

    def near(self, lat: float, lon: float, km: float):
        n = int(km / (111 * self.cell)) + 1
        ci, cj = int(lat // self.cell), int(lon // self.cell)
        for i in range(ci - n, ci + n + 1):
            for j in range(cj - n, cj + n + 1):
                for la, lo, r in self.cells.get((i, j), []):
                    d = geo.haversine_km(lat, lon, la, lo)
                    if d <= km:
                        yield d, la, lo, r


def osm_match(w: dict, idx: OsmIndex, lc: dict) -> tuple[float, float, str, str] | None:
    """(lat, lon, name, how): the OpenStreetMap object the catalog linked to this hotel ("link"), else the closest
    same-name hotel within name_match_km ("name")."""
    linked = idx.by_pid.get(w.get("osm_pid") or "")
    if linked:
        return float(linked["lat"]), float(linked["lon"]), linked.get("name", ""), "link"
    la, lo = float(w["lat"]), float(w["lon"])
    best = None
    for d, la2, lo2, r in idx.near(la, lo, lc["name_match_km"]):
        s = similarity(w["name"], r.get("name", ""))
        if s >= lc["min_similarity"] and (best is None or (s, -d) > (best[0], -best[1])):
            best = (s, d, la2, lo2, r.get("name", ""))
    return (best[2], best[3], best[4], "name") if best else None


def photon_search(sess: Session, base: str, w: dict) -> list[dict]:
    q = urlencode({"q": f"{w['name']} {w.get('city', '')}".strip(), "lat": w["lat"], "lon": w["lon"], "limit": 6})
    r = sess.request("GET", f"{base}?{q}")
    if r.status != 200:
        raise RuntimeError(f"Photon HTTP {r.status} {r.error[:80]}")
    return r.json().get("features", [])


def photon_match(w: dict, features: list[dict], lc: dict) -> tuple[float, float, str] | None:
    """A Photon result that is a place to stay with the same name, within photon_max_km of the listing."""
    la, lo = float(w["lat"]), float(w["lon"])
    best = None
    for f in features:
        p, (lo2, la2) = f.get("properties", {}), f.get("geometry", {}).get("coordinates", [None, None])
        if la2 is None or p.get("osm_key") not in lc["photon_keys"]:
            continue
        d = geo.haversine_km(la, lo, la2, lo2)
        s = similarity(w["name"], p.get("name", ""))
        if s >= lc["min_similarity"] and d <= lc["photon_max_km"] and (best is None or s > best[0]):
            best = (s, la2, lo2, p.get("name", ""))
    return (best[1], best[2], best[3]) if best else None


def decide(w: dict, manual: dict | None, osm, photon, in_water: bool, lc: dict, now: str) -> dict:
    """Moves a hotel only on strong evidence: a position set by hand, the OpenStreetMap object the catalog linked, two
    name matches that agree with each other, or a listed position in the sea with a name match on land. A name match
    alone only confirms (when it agrees) or flags the hotel for a look ("check")."""
    la, lo = float(w["lat"]), float(w["lon"])
    agree = lc["agree_km"]

    def near(a, b):
        return geo.haversine_km(a[0], a[1], b[0], b[1]) <= agree
    named = [c for c in (osm if osm and osm[3] == "name" else None, photon) if c]
    if manual:
        src, pos, status = "manual", (float(manual["lat"]), float(manual["lon"])), "manual"
    elif osm and osm[3] == "link":
        src, pos = "osm", osm[:2]
        status = "confirmed" if near(pos, (la, lo)) else "corrected"
    elif any(near(c, (la, lo)) for c in named):
        c = next(c for c in named if near(c, (la, lo)))
        src, pos, status = ("osm" if c is osm else "photon"), c[:2], "confirmed"
    elif len(named) == 2 and near(named[0], named[1]):
        src, pos, status = "osm", osm[:2], "corrected"
    elif named and in_water:
        c = named[0]
        src, pos, status = ("osm" if c is osm else "photon"), c[:2], "corrected"
    else:
        src, pos, status = "listing", (la, lo), "check" if named else "listing only"
    off = geo.haversine_km(la, lo, pos[0], pos[1])
    return {"pid": w["pid"], "name": w["name"], "lat": round(pos[0], 6), "lon": round(pos[1], 6), "source": src,
            "status": status, "listing_lat": w["lat"], "listing_lon": w["lon"], "listing_off_km": round(off, 2),
            "osm_lat": osm[0] if osm else "", "osm_lon": osm[1] if osm else "", "osm_name": osm[2] if osm else "",
            "photon_lat": photon[0] if photon else "", "photon_lon": photon[1] if photon else "",
            "photon_name": photon[2] if photon else "", "checked_at": now}


def due(watch: list[dict], known: dict, manual: dict, water: set, today: date, lc: dict) -> list[dict]:
    """New hotels first, then hotels whose listing moved, whose fix changed or that sit in the water, then old checks
    (refresh_days); hotels without a second source are searched again after retry_days."""
    out = []
    for w in watch:
        k = known.get(w["pid"])
        if not k:
            out.append((0, w))
            continue
        if (k["listing_lat"], k["listing_lon"]) != (w["lat"], w["lon"]) or \
                (w["pid"] in manual) != (k["source"] == "manual") or (w["pid"] in water and k["source"] == "listing"):
            out.append((1, w))
            continue
        age = (today - date.fromisoformat(k["checked_at"][:10])).days
        if age >= (lc["retry_days"] if k["status"] in ("listing only", "check") else lc["refresh_days"]):
            out.append((2, w))
    return [w for _, w in sorted(out, key=lambda x: x[0])]


def in_water(places: dict, lc: dict) -> dict[str, tuple[str, str]]:
    """pid -> the (lat, lon) the type-of-place run found on the sea side of the coastline (beyond water_margin_m)."""
    return {pid: (r["lat"], r["lon"]) for pid, r in places.items()
            if r.get("coast_side") == "water" and float(r.get("coast_m") or 0) >= lc["water_margin_m"]}


def positions(cfg: dict) -> dict[str, tuple[str, str]]:
    """pid -> (lat, lon) from the store, for the other runs (places) and the export."""
    return {r["pid"]: (str(r["lat"]), str(r["lon"])) for r in read_csv(cfg["paths"]["locations"]) if r.get("lat")}


def run(cfg: dict, max_minutes: float, search=None, today: date | None = None, log=log, clock=time.monotonic,
        sleep=time.sleep) -> dict:
    lc, p = cfg["locations"], cfg["paths"]
    today = today or date.today()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    watch = [w for w in read_csv(p["watchlist"]) if not w.get("gone_since") and w.get("lat")]
    known = {r["pid"]: r for r in read_csv(p["locations"])}
    manual = {r["pid"]: r for r in read_csv(p["location_fixes"]) if r.get("pid") and r.get("lat")}
    idx = OsmIndex(read_csv(p["osm_catalog"]))
    if search is None:
        sess = Session(lc["user_agent"], timeout=30)
        search = lambda w: photon_search(sess, lc["photon_url"], w)  # noqa: E731
    wet = in_water({r["pid"]: r for r in read_csv(p["places"])}, lc)
    listing_wet = {pid for pid, pos in wet.items() if any(w["pid"] == pid and (w["lat"], w["lon"]) == pos for w in watch)}
    todo = due(watch, known, manual, listing_wet, today, lc)
    stats = {"due": len(todo), "done": 0, "photon_calls": 0, "photon_errors": 0}
    started = clock()
    for w in todo:
        if clock() - started > max_minutes * 60:
            break
        osm = osm_match(w, idx, lc)
        photon = None
        settled = w["pid"] in manual or (osm and (osm[3] == "link" or
                                                 geo.haversine_km(float(w["lat"]), float(w["lon"]), osm[0], osm[1]) <= lc["agree_km"]))
        if not settled and stats["photon_calls"] < lc["photon_per_run"]:
            try:
                stats["photon_calls"] += 1
                photon = photon_match(w, search(w), lc)
            except Exception as e:  # noqa: BLE001
                stats["photon_errors"] += 1
                log(f"{w['name'][:40]}: {str(e)[:100]}")
                if stats["photon_errors"] >= lc["stop_after_errors"]:
                    lc = {**lc, "photon_per_run": 0}
            sleep(lc["pause_seconds"])
        known[w["pid"]] = decide(w, manual.get(w["pid"]), osm, photon, w["pid"] in listing_wet, lc, now)
        stats["done"] += 1
    watched = {w["pid"] for w in watch}
    write_csv(sorted((r for r in known.values() if r["pid"] in watched), key=lambda r: r["pid"]), p["locations"], FIELDS)
    rows = [known[w["pid"]] for w in watch if w["pid"] in known]
    from collections import Counter
    stats["status"] = dict(Counter(r["status"] for r in rows).most_common())
    stats["source"] = dict(Counter(r["source"] for r in rows).most_common())
    stats["coverage"] = f"{len(rows)}/{len(watch)}"
    stats["in_water"] = [(r["pid"], r["name"], r["source"]) for r in rows
                         if wet.get(r["pid"]) == (str(r["lat"]), str(r["lon"]))]
    stats["check"] = [(r["pid"], r["name"], r["osm_name"] or r["photon_name"]) for r in rows if r["status"] == "check"][:30]
    stats["moved"] = sorted(((r["listing_off_km"], r["name"], r["source"]) for r in rows if r["status"] == "corrected"),
                            key=lambda x: -float(x[0]))[:20]
    return stats


def summary_md(s: dict) -> str:
    md = [f"## Hotel positions\n", f"Located {s['done']} of {s['due']} due; coverage {s['coverage']}. "
          f"Status: {', '.join(f'{k} {v}' for k, v in s['status'].items())}. "
          f"Source: {', '.join(f'{k} {v}' for k, v in s['source'].items())}. "
          f"Photon searches {s['photon_calls']} ({s['photon_errors']} failed).\n"]
    if s["moved"]:
        md += ["Moved furthest from the listed position:\n"] + [f"- {n}: {k} km ({src})" for k, n, src in s["moved"]] + [""]
    md += [f"Still in the water (sea side of the coastline, no better position found): {len(s['in_water'])}"]
    md += [f"- {n} ({pid}, {src})" for pid, n, src in s["in_water"][:30]]
    md += ["", f"To check (a same-name place elsewhere, not moved): {len(s['check'])}"]
    md += [f"- {n} ({pid}): {other}" for pid, n, other in s["check"]]
    return "\n".join(md) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(config.DEFAULT_PATH))
    ap.add_argument("--max-minutes", type=float)
    args = ap.parse_args(argv)
    cfg = config.load(args.config)
    res = run(cfg, args.max_minutes or cfg["locations"]["max_minutes"])
    md = summary_md(res)
    out = Path(cfg["paths"]["out_dir"])
    out.mkdir(parents=True, exist_ok=True)
    (out / "locations_summary.md").write_text(md, encoding="utf-8")
    log(json.dumps({k: v for k, v in res.items() if k not in ("in_water", "moved", "check")}))
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
