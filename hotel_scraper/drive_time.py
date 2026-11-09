"""Phase 1c: real driving time from the origin to each property (OSRM table service).

Settings: universe.drive_time in config/settings.yaml. Results are cached in paths.drive_times, so each
property is routed once; it is re-routed only if its map position moves. The public OSRM demo server
allows reasonable non-commercial use at no more than 1 request per second, so requests are throttled
and capped per run; whatever is left over is routed on the next run.
"""
from __future__ import annotations

import csv
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import geo

CACHE_FIELDS = ["pid", "lat", "lon", "drive_hours", "road_km", "computed_on"]


@dataclass
class Stats:
    routed: int = 0          # newly routed this run
    cached: int = 0          # reused from cache
    pending: int = 0         # not routed yet (cap reached or server error); kept, retried next run
    unroutable: int = 0      # server found no road route
    dropped: int = 0         # drive time above the limit
    error: str = ""


def load_cache(path: Path | str) -> dict[str, dict]:
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return {r["pid"]: r for r in csv.DictReader(f)}
    except FileNotFoundError:
        return {}


def save_cache(cache: dict[str, dict], path: Path | str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, CACHE_FIELDS, lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        w.writerows(cache[k] for k in sorted(cache))


def _fresh(entry: dict | None, lat: float, lon: float, moved_km: float) -> bool:
    if not entry:
        return False
    try:
        return geo.haversine_km(float(entry["lat"]), float(entry["lon"]), lat, lon) <= moved_km
    except (KeyError, ValueError):
        return False


def osrm_get(url: str, timeout: int) -> dict:
    req = Request(url, headers={"User-Agent": "hotel-deal-radar/0.3 (personal project)"})
    try:
        with urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except HTTPError as e:  # OSRM reports errors such as TooBig as JSON with a 4xx status
        try:
            return json.load(e)
        except Exception:
            raise e


def table_url(endpoint: str, profile: str, origin: tuple[float, float], dests: list[tuple[float, float]]) -> str:
    coords = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in [origin, *dests])
    return f"{endpoint.rstrip('/')}/table/v1/{profile}/{coords}?sources=0&annotations=duration,distance"


def route_batch(origin, dests, dt: dict, fetch: Callable[[str], dict]) -> list[tuple[float | None, float | None]]:
    data = fetch(table_url(dt["endpoint"], dt["profile"], origin, dests))
    if data.get("code") != "Ok":
        raise RuntimeError(f"OSRM {data.get('code')}: {data.get('message', '')}")
    durs, dists = data["durations"][0][1:], (data.get("distances") or [[None] * (len(dests) + 1)])[0][1:]
    return [(None if s is None else s / 3600, None if m is None else m / 1000) for s, m in zip(durs, dists)]


def update(props: list, cfg: dict, today: str, fetch: Callable[[str], dict] | None = None,
           sleep: Callable[[float], None] = time.sleep, log=print) -> tuple[list, Stats]:
    """Attach drive_hours/road_km to each property and drop those above the limit. Never raises on server
    trouble: unrouted properties are kept (blank drive time) and retried next run."""
    dt, paths, o = cfg["universe"]["drive_time"], cfg["paths"], cfg["origin"]
    stats = Stats()
    if not dt.get("enabled", True):
        return props, stats
    fetch = fetch or (lambda url: osrm_get(url, dt.get("timeout_seconds", 60)))
    cache = load_cache(paths["drive_times"])
    origin = (o["lat"], o["lon"])

    todo = [p for p in props if not _fresh(cache.get(p.pid), p.lat, p.lon, dt.get("reroute_if_moved_km", 0.5))]
    stats.cached = len(props) - len(todo)
    batch_size, requests = dt["batch_size"], 0
    i = 0
    while i < len(todo):
        if requests >= dt["max_requests_per_run"]:
            break
        batch = todo[i:i + batch_size]
        try:
            results = route_batch(origin, [(p.lat, p.lon) for p in batch], dt, fetch)
        except Exception as e:  # noqa: BLE001
            if "TooBig" in str(e) and batch_size > 1:
                batch_size = max(1, batch_size // 2)
                log(f"OSRM: batch too big, retrying with {batch_size}")
                continue
            stats.error = str(e)
            log(f"OSRM unavailable, stopping drive-time routing for this run: {e}")
            break
        finally:
            requests += 1
            sleep(dt["pause_seconds"])
        for p, (hours, km) in zip(batch, results):
            cache[p.pid] = {"pid": p.pid, "lat": p.lat, "lon": p.lon, "computed_on": today,
                            "drive_hours": "" if hours is None else round(hours, 2),
                            "road_km": "" if km is None else round(km, 1)}
            stats.routed += 1
        i += len(batch)
    stats.pending = len(todo) - stats.routed
    save_cache(cache, paths["drive_times"])

    kept, move_km = [], dt.get("reroute_if_moved_km", 0.5)
    for p in props:
        entry = cache.get(p.pid)
        fresh = _fresh(entry, p.lat, p.lon, move_km)
        hours = entry.get("drive_hours", "") if fresh else ""
        if fresh and hours in ("", None):
            stats.unroutable += 1
            if not dt.get("keep_unroutable", True):
                continue
        p.drive_hours = "" if hours in ("", None) else float(hours)
        p.road_km = float(entry["road_km"]) if fresh and entry.get("road_km") not in ("", None) else ""
        if p.drive_hours != "" and p.drive_hours > dt["max_hours"]:
            stats.dropped += 1
            continue
        kept.append(p)
    return kept, stats
