"""Vio.com and the sites built on it (Etrip.net, goSeek...: the same search service, sapi.fih.io, with their own
brand): each watched hotel's own price where the booking is made on Vio's checkout (settings: vio). Owner,
2026-11-23: every vendor that sells the room itself.

Vio also shows other sites' prices (it began as a comparison site, FindHotel); only its own sales count here: offers
whose intermediary is Vio itself ("FHT", what its own site pre-loads the checkout for).

    ids     Vio's own search-box suggestions (suggest) give hotels; each candidate's position (hotel) is checked by name
            and place (same rules as Agoda)
    prices  Vio's own price calendar (availability): up to 61 check-in nights for a list of hotels in one request

Prices per room per night in euros: price = with taxes (what Booking.com shows), price_excl = before taxes,
price_total = with the fees paid at the hotel too.

    python -m hotel_scraper.vio [--part ids|prices|all] [--max-minutes 30]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from . import config
from .agoda import ID_FIELDS, PRICE_FIELDS, ids_due, judge, merge_prices
from .http import Session
from .scan import read_csv, write_csv

SELLER = "vio"


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%H:%M:%S} {msg}", flush=True)


def suggestions(doc) -> list[dict]:
    """Hotels among Vio's search-box suggestions: [{id, name, where}]."""
    items = doc if isinstance(doc, list) else (doc.get("hits") or doc.get("results") or doc.get("suggestions") or [])
    return [{"id": str(x["objectID"]), "name": x.get("value") or x.get("hotelName") or "",
             "where": x.get("placeDisplayName") or ""}
            for x in items if x.get("objectType") == "hotel" and x.get("objectID")]


def hotel_position(doc) -> tuple[float | None, float | None]:
    h = doc[0] if isinstance(doc, list) and doc else doc
    g = (h or {}).get("_geoloc") or {}
    return (g.get("lat"), g.get("lon")) if g else (None, None)


def own_offer(o: dict, tiers: list[str]) -> bool:
    """An offer Vio sells itself (its own checkout), in a tier a visitor sees without signing in."""
    return o.get("intermediaryProvider") == "FHT" and (o.get("accessTier") or "") in tiers


def availability(doc: dict, tiers: list[str]) -> dict[tuple[str, str], dict]:
    """{(hotel id, night): {available, price, price_excl, price_total, breakfast, cancellation, url, provider}} from a
    price-calendar answer: the cheapest offer Vio sells itself; a night it answered with none of its own is not
    available."""
    out = {}
    for hid, by_night in (doc.get("availability") or {}).items():
        for night, x in (by_night or {}).items():
            own = [o for o in (x or {}).get("offers") or [] if own_offer(o, tiers) and o.get("currency", "EUR") == "EUR"]
            if not own:
                out[(str(hid), night)] = {"available": False}
                continue
            o = min(own, key=lambda o: (o["rate"].get("base") or 0) + (o["rate"].get("taxes") or 0))
            r = o["rate"]
            base, taxes, fees = r.get("base") or 0, r.get("taxes") or 0, r.get("hotelFees") or 0
            pk = o.get("package") or {}
            free = [c for c in o.get("cancellationPenalties") or [] if c.get("amount") == 0]
            out[(str(hid), night)] = {"available": True, "price": round(base + taxes, 2), "price_excl": round(base, 2),
                                      "price_total": round(base + taxes + fees, 2),
                                      "breakfast": "included" if "breakfast" in (pk.get("amenities") or []) else "",
                                      "cancellation": f"free until {free[0]['end'][:10]}" if free else "",
                                      "url": o.get("url") or "", "provider": o.get("providerCode") or ""}
    return out


def windows(start: date, days: int, span: int) -> list[tuple[date, date]]:
    """(first, last) check-in nights per request: Vio's calendar takes at most `span` nights at a time."""
    out, d, end = [], start, start + timedelta(days=days)
    while d <= end:
        out.append((d, min(d + timedelta(days=span - 1), end)))
        d += timedelta(days=span)
    return out


class Vio:
    """Vio's own search service, called the way its website calls it (its public website key)."""
    def __init__(self, vc: dict, get=None, log=log):
        self.vc, self.log = vc, log
        self.anon = str(uuid.uuid4())
        if get is None:
            sess = Session(vc["user_agent"], timeout=45, headers={
                "X-API-KEY": vc["api_key"], "Origin": vc["origin"], "Referer": vc["origin"] + "/",
                "Accept": "application/json", "Accept-Language": "en-GB,en;q=0.9"})
            get = lambda u: sess.get(u)  # noqa: E731
        self.get = get
        self.samples = 0

    def _json(self, url: str):
        r = self.get(url)
        if r.status != 200:
            raise RuntimeError(f"Vio HTTP {r.status}: {r.text()[:150]}")
        doc = r.json()
        if self.vc.get("sample_dir") and self.samples < 4:        # real answers kept to build tests from
            self.samples += 1
            out = Path(self.vc["sample_dir"])
            out.mkdir(parents=True, exist_ok=True)
            (out / f"vio_sample_{self.samples}.json").write_text(json.dumps({"url": url, "answer": doc})[:3_000_000])
        return doc

    def suggest(self, query: str) -> list[dict]:
        q = urlencode({"query": query, "language": "en", "limit": self.vc["suggestions"]})
        return suggestions(self._json(f"{self.vc['api']}/suggest?{q}"))

    def position(self, hotel_id: str) -> tuple[float | None, float | None]:
        q = urlencode({"id": hotel_id, "language": "en", "currency": "EUR",
                       "attributes": "objectID,hotelName,_geoloc,placeDisplayName"})
        return hotel_position(self._json(f"{self.vc['api']}/hotel?{q}"))

    def calendar(self, hotel_ids: list[str], first: date, last: date, adults: int) -> dict:
        """One price calendar (polled until Vio says it is complete)."""
        params = {"hotelIds": ",".join(hotel_ids), "startDate": first.isoformat(), "endDate": last.isoformat(),
                  "nights": 1, "rooms": str(adults), "cugDeals": ",".join(self.vc["cug_deals"]),
                  "deviceType": "desktop", "language": "en", "currency": "EUR", "anonymousId": self.anon,
                  "brand": self.vc["brand"]}
        merged: dict = {"availability": {}}
        token = None
        for _ in range(self.vc["max_polls"]):
            doc = self._json(f"{self.vc['api']}/availability?" + urlencode({**params, **({"token": token} if token else {})}))
            for hid, nights in (doc.get("availability") or {}).items():
                merged["availability"].setdefault(hid, {}).update(nights or {})
            st = doc.get("status") or {}
            if st.get("complete", True) or not st.get("nextToken"):
                break
            token = st["nextToken"]
            time.sleep(self.vc["poll_seconds"])
        return merged


def find_id(api, w: dict, vc: dict, stats: dict, sleep=time.sleep, log=log) -> dict | None:
    best = None
    for tpl in vc["queries"]:
        q = tpl.format(name=w["name"], city=w.get("city", "")).strip()
        try:
            for c in api.suggest(q)[: vc["candidates_checked"]]:
                lat, lon = api.position(c["id"])
                m = judge(w, {**c, "lat": lat, "lon": lon}, vc)
                if m and m["ok"]:
                    return m
                if m and (best is None or m["km"] < best["km"]):
                    best = m
        except Exception as e:  # noqa: BLE001
            stats["errors"] += 1
            log(f"{q[:40]}: {str(e)[:120]}")
        sleep(vc["pause_seconds"])
    return best


def run(cfg: dict, part: str, max_minutes: float, api, today: date | None = None, log=log, clock=time.monotonic,
        sleep=time.sleep, now_dt: datetime | None = None) -> dict:
    vc, p = cfg["vio"], cfg["paths"]
    today = today or date.today()
    now_dt = now_dt or datetime.now(timezone.utc)
    now = now_dt.isoformat(timespec="seconds")
    pos = {}
    if p.get("locations"):
        from .locations import positions
        pos = positions(cfg)
    watch = []
    for w in read_csv(p["watchlist"]):
        if w.get("gone_since") or not w.get("lat"):
            continue
        lat, lon = pos.get(w["pid"], (w["lat"], w["lon"]))
        watch.append({**w, "lat": lat, "lon": lon})
    all_ids = read_csv(p["seller_ids"])
    known = {r["pid"]: r for r in all_ids if r["seller"] == SELLER}
    stats = {"ids_due": 0, "ids_found": 0, "ids_missed": 0, "calls": 0, "priced": 0, "available": 0, "errors": 0}
    started = clock()

    if part in ("ids", "all"):
        todo = ids_due(watch, known, today, vc)[: vc["ids_per_run"]]
        stats["ids_due"] = len(todo)
        budget = max_minutes * 60 * (vc["ids_share"] if part == "all" else 1)
        for w in todo:
            if clock() - started > budget or stats["errors"] >= vc["stop_after_errors"]:
                break
            b = find_id(api, w, vc, stats, sleep, log) or {}
            ok = bool(b.get("ok"))
            stats["ids_found" if ok else "ids_missed"] += 1
            known[w["pid"]] = {"pid": w["pid"], "seller": SELLER, "seller_id": b.get("id", ""), "city_id": "",
                               "seller_name": b.get("name", ""), "lat": b.get("lat", ""), "lon": b.get("lon", ""),
                               "km": b.get("km", ""), "similarity": b.get("similarity", ""),
                               "status": "ok" if ok else "not found", "checked_at": now}
            log(f"id {w['name'][:35]}: {known[w['pid']]['status']} {b.get('id', '')} {b.get('name', '')[:30]} "
                f"{b.get('km', '')} km")
        others = [r for r in all_ids if r["seller"] != SELLER]
        write_csv(sorted(others + list(known.values()), key=lambda r: (r["seller"], r["pid"])), p["seller_ids"],
                  ID_FIELDS)

    if part in ("prices", "all"):
        new, adults = [], cfg["scan"]["adults"]
        mapped = sorted((r for r in known.values() if r["status"] == "ok" and r["seller_id"]), key=lambda r: r["pid"])
        by_id = {r["seller_id"]: r for r in mapped}
        ids = list(by_id)
        if ids:                                  # each pass starts further on, so every hotel comes round
            k = (int(now_dt.timestamp() // (4 * 3600)) * vc["hotels_per_pass"]) % len(ids)
            ids = ids[k:] + ids[:k]
        batches = [ids[i: i + vc["hotels_per_call"]] for i in range(0, len(ids), vc["hotels_per_call"])]
        start = today + timedelta(days=vc["min_lead_days"])
        tasks = [(b, w) for b in batches
                 for w in windows(start, vc["horizon_days"] - vc["min_lead_days"], vc["nights_per_call"])]

        def one(task):
            (batch, (first, last)) = task
            if clock() - started > max_minutes * 60 or stats["errors"] >= vc["stop_after_errors"]:
                return task, None, "skipped"
            try:
                got = availability(api.calendar(batch, first, last, adults), vc["public_tiers"])
                sleep(vc["pause_seconds"])
                return task, got, ""
            except Exception as e:  # noqa: BLE001
                sleep(vc["pause_seconds"])
                return task, None, str(e)[:120]
        # Vio's calendar answers in several polls (~25 s per call, run 45378...): a few calls at once
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max(1, vc.get("workers", 1))) as ex:
            for (batch, (first, last)), got, err in ex.map(one, tasks):
                if err == "skipped":
                    continue
                if err:
                    stats["errors"] += 1
                    log(f"{first}..{last} ({len(batch)} hotels): {err}")
                    continue
                stats["calls"] += 1
                for (hid, night), x in got.items():
                    r = by_id.get(hid)
                    if r is None:
                        continue
                    stats["priced"] += 1
                    stats["available"] += 1 if x["available"] else 0
                    new.append({"pid": r["pid"], "seller": SELLER, "seller_id": hid, "night": night,
                                "price": x.get("price", ""), "price_excl": x.get("price_excl", ""),
                                "price_total": x.get("price_total", ""), "currency": "EUR",
                                "available": "yes" if x["available"] else "no",
                                "cancellation": x.get("cancellation", ""), "rooms_left": "",
                                "url": x.get("url") or vc["hotel_url"].format(
                                    id=hid, checkin=night, adults=adults,
                                    checkout=(date.fromisoformat(night) + timedelta(days=1)).isoformat()),
                                "seen_at": now})
        old_all = read_csv(p["seller_prices"])
        merged = merge_prices([x for x in old_all if x.get("seller") == SELLER], new, today)
        write_csv(sorted([x for x in old_all if x.get("seller") != SELLER] + merged,
                         key=lambda x: (x["seller"], x["pid"], x["night"])), p["seller_prices"], PRICE_FIELDS)
        stats["stored_nights"] = len(merged)
    stats["ids_coverage"] = f"{sum(1 for w in watch if known.get(w['pid'], {}).get('status') == 'ok')}/{len(watch)}"
    return stats


def summary_md(s: dict) -> str:
    return ("## Vio.com (and Etrip, goSeek...)\n\n" + f"Hotels found on Vio: {s['ids_coverage']} (this pass: "
            f"{s['ids_found']} found, {s['ids_missed']} not found of {s['ids_due']} looked up). Prices: {s['calls']} "
            f"calendar calls, {s['priced']} hotel-nights answered, {s['available']} sold by Vio itself; "
            f"{s.get('stored_nights', 0)} nights stored; {s['errors']} errors.\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(config.DEFAULT_PATH))
    ap.add_argument("--part", choices=["ids", "prices", "all"], default="all")
    ap.add_argument("--max-minutes", type=float)
    args = ap.parse_args(argv)
    cfg = config.load(args.config)
    cfg["vio"]["sample_dir"] = str(Path(cfg["paths"]["out_dir"]) / "vio_samples")
    stats = run(cfg, args.part, args.max_minutes or cfg["vio"]["max_minutes"], Vio(cfg["vio"]))
    log(f"vio: {stats}")
    out = Path(cfg["paths"]["out_dir"])
    out.mkdir(parents=True, exist_ok=True)
    (out / "vio_summary.md").write_text(summary_md(stats), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
