"""Turn the scan settings into concrete stays. Nothing about dates is hardcoded: it all comes from `scan:`."""
from __future__ import annotations

from datetime import date, timedelta

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _d(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v))


def _in(d: date, windows: list[dict]) -> bool:
    return any(_d(w["from"]) <= d <= _d(w["to"]) for w in windows)


def checkin_dates(scan: dict, today: date) -> list[date]:
    start = today + timedelta(days=scan.get("min_lead_days", 0))
    end = today + timedelta(days=scan["horizon_days"] - 1)
    allowed_days = {WEEKDAYS.index(d.lower()[:3]) for d in scan.get("checkin_weekdays") or WEEKDAYS}
    windows, excluded = scan.get("date_windows") or [], scan.get("exclude_windows") or []
    out, d = [], start
    while d <= end:
        if d.weekday() in allowed_days and (not windows or _in(d, windows)) and not _in(d, excluded):
            out.append(d)
        d += timedelta(days=1)
    return out


def stays(scan: dict, today: date) -> list[tuple[date, date]]:
    """(check-in, check-out) pairs. A stay is dropped if any of its nights falls in an excluded window."""
    excluded = scan.get("exclude_windows") or []
    out = []
    for ci in checkin_dates(scan, today):
        for n in scan.get("stay_nights") or [1]:
            nights = [ci + timedelta(days=i) for i in range(n)]
            if not any(_in(x, excluded) for x in nights):
                out.append((ci, ci + timedelta(days=n)))
    return out
