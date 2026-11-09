"""Which map tiles to refresh this run, and when each was last refreshed (state in paths.tile_state).

Tiles never refreshed come first (all of them, limited only by the run's time budget). Once every tile has been
refreshed at least once, each run takes the `overpass.tiles_per_run` oldest ones, so the whole map is renewed
on a rolling basis and every run stays short.
"""
from __future__ import annotations

import json
from pathlib import Path


def key(bbox: tuple) -> str:
    return ",".join(f"{x:.4f}" for x in bbox)


def load(path: Path | str) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save(state: dict, path: Path | str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(dict(sorted(state.items())), indent=1) + "\n", encoding="utf-8")


def choose(all_tiles: list[tuple], state: dict, per_run: int) -> list[tuple]:
    never = [t for t in all_tiles if key(t) not in state]
    if never:
        return never
    return sorted(all_tiles, key=lambda t: state[key(t)]["done"])[:per_run]


def record(state: dict, results: list, now_iso: str) -> None:
    for r in results:
        if r.ok:
            state[key(r.bbox)] = {"done": now_iso, "elements": r.elements, "kept": r.kept, "seconds": r.seconds}


def covered_fn(results: list):
    boxes = [r.bbox for r in results if r.ok]

    def covered(row: dict) -> bool:
        try:
            lat, lon = float(row["lat"]), float(row["lon"])
        except (KeyError, ValueError):
            return False
        return any(s <= lat <= n and w <= lon <= e for s, w, n, e in boxes)
    return covered
