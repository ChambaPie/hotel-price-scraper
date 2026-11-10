"""Booking.com collection (public scraper; settings: booking_scan). Deal identification lives in the private repo.

1. Map: watched properties without a Booking.com id get a search ("name town", then name, then the town's results),
   best name match kept (data/booking_ids.csv; 'not found' retried after remap_after_days).
2. Calendars: properties collected longest ago, 6 calendar calls each = every night's price for 12 months, written
   to paths.booking_calendars (one row per property; published to the `snapshots` branch).
3. Last-minute: Booking.com deal badges / advertised discounts for the next nights (last_minute.py).

    python -m hotel_scraper.booking_collect
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

import yaml

from . import catalog as cat
from . import config
from .discover_osm import _norm_name
from .scan import read_csv, write_csv

ID_FIELDS = ["pid", "booking_id", "booking_name", "similarity", "status", "checked_at"]
CAL_FIELDS = ["pid", "booking_id", "scanned_at", "first_date", "currency", "nights", "status", "error"]


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%H:%M:%S} {msg}", flush=True)


def best_match(name: str, cards: list[dict], min_sim: float) -> tuple[dict | None, float]:
    n = _norm_name(name)
    best, score = None, 0.0
    for c in cards:
        s = SequenceMatcher(None, n, _norm_name(c["name"])).ratio()
        if s > score:
            best, score = c, s
    return (best, round(score, 3)) if best is not None and score >= min_sim else (None, round(score, 3))


def clean_query(name: str) -> str:
    """Hotel names as search text: drop star marks ("****s"), quotes and symbols that send Booking.com's search to its
    home page."""
    import re
    s = re.sub(r"\*+\s*[sS]?\b", " ", name)
    s = re.sub(r"[\"'«»“”*&|/()\[\]–-]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def encode_nights(nights: dict[str, dict], first: date, days: int) -> str:
    """One token per night from `first`: "" = no price / unavailable, "184" = price, "184/3" = price with a 3-night
    minimum stay. Tokens joined by ";"."""
    out = []
    for i in range(days):
        n = nights.get((first + timedelta(days=i)).isoformat()) or {}
        p = n.get("price") if n.get("available", True) else None
        if not p:
            out.append("")
            continue
        tok = f"{p:g}"
        if (n.get("min_stay") or 1) > 1:
            tok += f"/{n['min_stay']}"
        out.append(tok)
    return ";".join(out)


def decode_nights(first: str, encoded: str) -> dict[str, dict]:
    d0, out = date.fromisoformat(first), {}
    for i, tok in enumerate(encoded.split(";") if encoded else []):
        if not tok:
            continue
        price, _, ms = tok.partition("/")
        out[(d0 + timedelta(days=i)).isoformat()] = {"price": float(price), "available": True, "min_stay": int(ms or 1)}
    return out


@dataclass
class Result:
    mapped: int = 0
    not_found: int = 0
    collected: int = 0
    failed: int = 0
    calls: int = 0
    seconds: float = 0.0
    errors: dict = field(default_factory=dict)


def run(cfg: dict, session, today: date, log=log, clock=time.monotonic) -> Result:
    p, b = cfg["paths"], cfg["booking_scan"]
    started = clock()
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    res = Result()

    def left(share: float) -> bool:
        return clock() - started < b["max_minutes"] * 60 * share

    watched = {r["pid"]: r for r in cat.read(p["watchlist"]) if r["pid"].startswith("ta:")}
    ids = {r["pid"]: r for r in read_csv(p["booking_ids"])}
    warm = b.get("warm_up_page")
    if warm and not session.token:
        try:
            session.warm_up(*warm.split("/", 1), today + timedelta(days=30))
        except Exception as e:  # noqa: BLE001
            log(f"warm-up failed: {e}")
    log(f"session token: {'yes' if session.token else 'NO'}")

    retry_before = (datetime.now(timezone.utc) - timedelta(days=b["remap_after_days"])).isoformat(timespec="seconds")
    todo = [pid for pid in watched if pid not in ids or (ids[pid]["status"] != "ok" and ids[pid]["checked_at"] < retry_before)]
    share = b["map_share_backlog"] if len(todo) > b["map_backlog"] else b["map_share"]
    checkin = today + timedelta(days=30)

    def record(pid, card, sim):
        ids[pid] = {"pid": pid, "booking_id": f"{card['cc']}/{card['slug']}" if card else "",
                    "booking_name": card["name"] if card else "", "similarity": sim,
                    "status": "ok" if card else "not found", "checked_at": now_iso}

    # a) town first: one or two filtered town searches match every unmapped watched property of that town at once
    by_town: dict[str, list[str]] = {}
    for pid in todo:
        by_town.setdefault(watched[pid].get("city", ""), []).append(pid)
    towns = sorted((t for t in by_town if t and len(by_town[t]) >= b["town_min_properties"]), key=lambda t: -len(by_town[t]))
    for town in towns:
        if not left(share):
            break
        cards = []
        for nflt in b["town_filters"]:
            try:
                cards += session.search(town, checkin, wait=12, scrolls=b["town_scrolls"], nflt=nflt)
            except Exception as e:  # noqa: BLE001
                res.errors[str(e)[:80]] = res.errors.get(str(e)[:80], 0) + 1
        found = 0
        for pid in by_town[town]:
            card, sim = best_match(watched[pid]["name"], cards, b["match_min_similarity"])
            if card:
                record(pid, card, sim)
                res.mapped += 1
                found += 1
        log(f"town {town}: {len(cards)} cards, {found} of {len(by_town[town])} matched")
    # b) the rest one by one (name + town, then name)
    for pid in [x for x in todo if x not in ids or ids[x]["status"] != "ok" or ids[x]["checked_at"] != now_iso][: b["map_per_run"]]:
        if pid in ids and ids[pid]["checked_at"] == now_iso:
            continue
        if not left(share):
            break
        w = watched[pid]
        try:
            name, town = clean_query(w["name"]), w.get("city", "")
            card, sim = best_match(w["name"], session.search(f"{name} {town}".strip(), checkin, wait=12), b["match_min_similarity"])
            if card is None:
                card, sim = best_match(w["name"], session.search(name, checkin, wait=12), b["match_min_similarity"])
        except Exception as e:  # noqa: BLE001
            ids[pid] = {"pid": pid, "booking_id": "", "booking_name": "", "similarity": "", "status": "error",
                        "checked_at": now_iso}
            res.errors[str(e)[:80]] = res.errors.get(str(e)[:80], 0) + 1
            continue
        record(pid, card, sim)
        res.mapped += 1 if card else 0
        res.not_found += 0 if card else 1
    write_csv(sorted(ids.values(), key=lambda r: r["pid"]), p["booking_ids"], ID_FIELDS)
    log(f"mapping: {res.mapped} found, {res.not_found} not found; {sum(1 for r in ids.values() if r['status'] == 'ok')} mapped")

    cals = {r["pid"]: r for r in read_csv(p["booking_calendars"])}
    mapped = [pid for pid in watched if ids.get(pid, {}).get("status") == "ok"]
    order = sorted(mapped, key=lambda pid: cals.get(pid, {}).get("scanned_at", ""))[: b["properties_per_run"]]
    if not session.token and order:
        session.search("Trieste", today + timedelta(days=30))
    days = 61 * b["windows"]
    for pid in order:
        if not left(1.0):  # calendars get whatever the mapping left
            log("time budget reached")
            break
        bid = ids[pid]["booking_id"]
        cc, slug = bid.split("/", 1)
        try:
            nights = session.year(cc, slug, today, b["windows"])
        except Exception as e:  # noqa: BLE001
            res.failed += 1
            res.errors[str(e)[:80]] = res.errors.get(str(e)[:80], 0) + 1
            cals[pid] = {**cals.get(pid, {}), "pid": pid, "booking_id": bid, "scanned_at": now_iso, "status": "error",
                         "error": str(e)[:120]}
            continue
        cals[pid] = {"pid": pid, "booking_id": bid, "scanned_at": now_iso, "first_date": today.isoformat(),
                     "currency": session.currency_seen, "nights": encode_nights(nights, today, days), "status": "ok", "error": ""}
        res.collected += 1
    res.calls, res.seconds = session.calls, round(clock() - started)
    keep = set(watched)
    write_csv(sorted((v for k, v in cals.items() if k in keep), key=lambda r: r["pid"]), p["booking_calendars"], CAL_FIELDS)
    return res


def main(argv=None) -> int:
    from .booking_session import BookingSession
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(config.DEFAULT_PATH))
    ap.add_argument("--minutes", type=float)
    args = ap.parse_args(argv)
    cfg = config.load(args.config)
    if args.minutes:
        cfg["booking_scan"]["max_minutes"] = args.minutes
    bk = yaml.safe_load(Path(cfg["paths"]["spike"]).read_text(encoding="utf-8"))["booking"]
    s = cfg["scan"]
    out = Path(cfg["paths"]["out_dir"])
    out.mkdir(parents=True, exist_ok=True)
    with BookingSession(bk["base_url"], bk["user_agent"], bk["accept_language"], s["adults"], s["rooms"], s["currency"],
                        log=log) as session:
        session.diag_dir = out
        res = run(cfg, session, date.today())
        lm = 0
        if cfg.get("last_minute", {}).get("enabled"):
            from . import last_minute
            try:
                lm = last_minute.collect(cfg, session, date.today(), log=log)
            except Exception as e:  # noqa: BLE001
                log(f"last-minute collection failed: {e}")
    md = (f"## Booking.com collection: {res.collected} calendars, {res.calls} calls, {res.seconds} s\n\n"
          f"Mapping: {res.mapped} found, {res.not_found} not found. Failed: {res.failed}. Last-minute cards: {lm}.\n")
    if res.errors:
        md += "\nErrors: " + "; ".join(f"{k} ×{v}" for k, v in res.errors.items()) + "\n"
    (out / "booking_summary.md").write_text(md, encoding="utf-8")
    print(md, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
