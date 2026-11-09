"""Last-minute cards (public collection; settings: last_minute). Filters and alerts live in the private repo.

Each run searches a few watchlist towns (rotating, busiest first) for check-in on each night in
last_minute.nights_ahead and keeps every card with a Booking.com deal badge ("Late Escape Deal", "Limited-time Deal",
"Getaway Deal", ...) or an advertised original-vs-current price. Rows for past nights are dropped.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import catalog as cat
from .booking_browser import parse_card
from .scan import read_csv, write_csv

FIELDS = ["seen_at", "night", "town", "booking_id", "pid", "name", "badge", "price", "original_price",
          "booking_discount", "currency", "score", "breakfast", "members_only", "room", "url"]


def is_offer(f: dict) -> bool:
    return bool(f.get("badge")) or bool(f.get("booking_discount"))


def towns_to_search(watched: list[dict], state: dict, n: int) -> list[str]:
    counts = Counter(r.get("city") for r in watched if r.get("city"))
    towns = [t for t, _ in counts.most_common()]
    return sorted(towns, key=lambda t: (state.get(t, ""), -counts[t]))[:n]


def collect(cfg: dict, session, today: date, log=print) -> int:
    lm, p, s = cfg["last_minute"], cfg["paths"], cfg["scan"]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    watched = cat.read(p["watchlist"])
    by_booking = {r["booking_id"]: r["pid"] for r in read_csv(p["booking_ids"]) if r.get("booking_id")}
    try:
        state = json.loads(Path(p["last_minute_state"]).read_text())
    except (FileNotFoundError, ValueError):
        state = {}
    rows = {(r["booking_id"], r["night"]): r for r in read_csv(p["last_minute"]) if r["night"] >= today.isoformat()}
    seen = 0
    for town in towns_to_search(watched, state, lm["towns_per_run"]):
        for n in lm["nights_ahead"]:
            night = today + timedelta(days=n)
            try:
                cards = session.search(town, night, scrolls=lm["scrolls"], raw=True)
            except Exception as e:  # noqa: BLE001
                log(f"last-minute search {town} {night}: {str(e)[:80]}")
                continue
            for c in cards:
                f = parse_card(c)
                if not is_offer(f):
                    continue
                bid = f"{c['cc']}/{c['slug']}"
                rows[(bid, night.isoformat())] = {
                    "seen_at": now, "night": night.isoformat(), "town": town, "booking_id": bid,
                    "pid": by_booking.get(bid, ""), "name": c["name"], "badge": f["badge"], "price": f["price"],
                    "original_price": f["original_price"] or "", "booking_discount": f["booking_discount"] or "",
                    "currency": f["currency"], "score": f["score"] or "", "breakfast": "yes" if f["breakfast"] else "",
                    "members_only": "yes" if f["members_only"] else "", "room": f["room"],
                    "url": f"https://www.booking.com/hotel/{c['cc']}/{c['slug']}.html?checkin={night.isoformat()}"
                           f"&checkout={(night + timedelta(days=1)).isoformat()}&group_adults={s['adults']}&no_rooms={s['rooms']}"}
                seen += 1
        state[town] = now
    write_csv(sorted(rows.values(), key=lambda r: (r["night"], r["booking_id"])), p["last_minute"], FIELDS)
    Path(p["last_minute_state"]).write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")
    return seen
