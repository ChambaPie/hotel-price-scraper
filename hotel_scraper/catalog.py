"""The catalog: every property ever discovered, refreshed daily from the live source.

Rows are never deleted. A property that disappears gets `gone_since`; if it comes back it is reactivated.
Columns added later by enrichment (e.g. booking_id, review_score) are preserved across refreshes.
No field changes on an ordinary day, so the daily commit only shows real changes.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from .discover_osm import FIELDS as DISCOVERY_FIELDS

META_FIELDS = ["first_seen", "gone_since"]


@dataclass
class Changes:
    new: list[dict] = field(default_factory=list)
    gone: list[dict] = field(default_factory=list)
    returned: list[dict] = field(default_factory=list)

    def any(self) -> bool:
        return bool(self.new or self.gone or self.returned)


def read(path: Path | str) -> list[dict]:
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


def merge(previous: list[dict], found: list[dict], today: str, covered=None) -> tuple[list[dict], Changes]:
    """covered(row) -> bool: was this row's position re-queried this run? Rows outside the refreshed area are left
    untouched, so a partial refresh never marks properties as gone."""
    prev = {r["pid"]: r for r in previous}
    now = {r["pid"]: r for r in found}
    ch = Changes()
    rows = []
    for pid, fresh in now.items():
        old = prev.get(pid)
        if old is None:
            row = {**fresh, "first_seen": today, "gone_since": ""}
            ch.new.append(row)
        else:
            row = {**old, **{k: str(v) for k, v in fresh.items()}}
            if old.get("gone_since"):
                row["gone_since"] = ""
                ch.returned.append(row)
        rows.append(row)
    for pid, old in prev.items():
        if pid not in now:
            row = dict(old)
            if covered is not None and not covered(old):
                rows.append(row)
                continue
            if not row.get("gone_since"):
                row["gone_since"] = today
                ch.gone.append(row)
            rows.append(row)
    return rows, ch


def columns(rows: list[dict], fields: list[str] | None = None) -> list[str]:
    """Base fields (default: OpenStreetMap discovery fields), then first_seen/gone_since, then any extra columns."""
    base = fields or DISCOVERY_FIELDS
    extra = sorted({k for r in rows for k in r} - set(base) - set(META_FIELDS))
    return base + META_FIELDS + extra


def sort_key(r: dict):
    return (r.get("country", ""), r.get("kind", ""), float(r.get("distance_km") or 0), r["pid"])


def write(rows: list[dict], path: Path | str, fields: list[str] | None = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, columns(rows, fields), lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(rows, key=sort_key))
