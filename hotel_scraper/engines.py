"""Hotels' own websites: prices from their booking engines (settings: engines). Owner, 2026-11-23: every vendor that
sells the room itself, the hotel's official site first among them.

Many official sites take bookings through the same few engines (data/official_sites.csv, from the seller probe's
survey). Each engine has its own price calendar, the one its booking page shows:

    seekda         s.seekda.com/api/calendar.json        price per night for the party, a month per call
    d_edge         websdk.d-edge.com/bestprice           cheapest room per night, with the booking link, a month per call
    simplebooking  simplebooking.it GraphQL availability one stay per call: a few nights per hotel per pass, rotating

    ids     once per hotel: open its booking page in a browser and note the engine's own hotel code from the calls the
            page makes (seller_ids.csv, seller "official", seller_id "<engine>:<code>...")
    prices  the engine's calendar for the next 12 months over plain HTTP (no browser), every night at once

    python -m hotel_scraper.engines [--part ids|prices|all] [--max-minutes 30]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from . import config
from .agoda import ID_FIELDS, PRICE_FIELDS, merge_prices
from .http import Session
from .scan import read_csv, write_csv

SELLER = "official"


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%H:%M:%S} {msg}", flush=True)


# ---------- the engine's hotel code, from the calls its booking page makes ----------

def code_from_requests(engine: str, urls: list[str]) -> dict | None:
    """The engine's own parameters for this hotel, from the addresses its booking page called."""
    for u in urls:
        if engine == "seekda":
            m = re.search(r"[?&]propertyCode=([A-Z0-9_]+)", u) or re.search(r"/properties/([A-Z0-9_]+)/", u) \
                or re.search(r"skd-property-code=([A-Z0-9_]+)", u)
            if m:
                return {"code": m.group(1)}
        elif engine == "simplebooking":
            m = re.search(r"simplebooking\.(?:it|travel)/.*?(?:[?&]hid=|/ibe2/hotel/|/hotel/)(\d+)", u)
            if m:
                return {"code": m.group(1)}
        elif engine == "d_edge":
            if "websdk.d-edge.com/" in u and "property=" in u:
                q = parse_qs(urlsplit(u).query)
                if q.get("property") and q.get("_authCode"):
                    return {"code": q["property"][0], "group": (q.get("groupId") or [""])[0], "auth": q["_authCode"][0]}
    return None


def seller_id(engine: str, p: dict) -> str:
    return f"{engine}:" + json.dumps(p, separators=(",", ":"), sort_keys=True)


def parse_seller_id(sid: str) -> tuple[str, dict]:
    engine, _, rest = sid.partition(":")
    return engine, json.loads(rest)


# ---------- calendars ----------

def months(start: date, days: int) -> list[tuple[date, date]]:
    """(first, last) day of each calendar month from start to start + days."""
    out, d, end = [], start, start + timedelta(days=days)
    while d <= end:
        nxt = (d.replace(day=1) + timedelta(days=32)).replace(day=1)
        out.append((d, min(nxt - timedelta(days=1), end)))
        d = nxt
    return out


def seekda_url(ec: dict, code: str, first: date, last: date, adults: int) -> str:
    q = {"propertyCode": code, "includeInformativeTaxesAndFees": "true", "calculateTaxes": "false",
         "returnNetPrices": "false", "channelID": "ibe", "priceMode": "FAIR", "currentRoom": "0", "promotionCodes": "null",
         "occupancy": adults, "lastDate": last.isoformat(), "firstDate": first.isoformat(), "currencyCode": "EUR",
         "targetCurrency": "EUR"}
    return ec["seekda"]["calendar_url"] + "?" + urlencode(q)


def seekda_nights(doc: dict) -> dict[str, dict]:
    """{night: {price, min_stay}} from a seekda calendar answer; nights without a price are not bookable."""
    out = {}
    for r in doc.get("result") or []:
        if r.get("price") and "ARRIVAL" in (r.get("scope") or []):
            stays = r.get("possibleStays") or [1]
            out[r["date"]] = {"price": float(r["price"]), "min_stay": min(stays), "url": ""}
    return out


def dedge_url(ec: dict, p: dict, first: date, last: date, adults: int) -> str:
    q = {"property": p["code"], "fromDate": first.isoformat(), "toDate": last.isoformat(), "adults": adults,
         "behavior": "lowerMinstay", "currency": "EUR", "groupId": p.get("group", ""), "locale": "en_GB", "output": "json",
         "origin": "websdk-widget", "version": "2.4.0", "_authCode": p["auth"]}
    return ec["d_edge"]["bestprice_url"] + "?" + urlencode(q)


def dedge_nights(doc: dict) -> dict[str, dict]:
    out = {}
    for r in ((doc.get("data") or {}).get("data") or []):
        st = (r.get("restrictions") or {})
        if st.get("status") == "Available" and r.get("pricePerNight") and (r.get("nights") or 1) == 1:
            night = (r.get("bookingParams") or {}).get("from")
            if night:
                out[night] = {"price": float(r["pricePerNight"]), "min_stay": st.get("minstay") or 1,
                              "url": r.get("plainBookLink", ""), "meal": r.get("meal", "")}
    return out


def simplebooking_body(template: dict, hotel_id: str, night: date, adults: int) -> dict:
    """SimpleBooking's own availability search (captured from its booking page) for another hotel and night."""
    import copy
    b = copy.deepcopy(template)
    v = b["variables"]
    v["hotelId"] = str(hotel_id)
    v["query"].update(checkIn=night.isoformat(), checkOut=(night + timedelta(days=1)).isoformat(),
                      allocations=[{"adults": adults, "children": []}])
    return b


def simplebooking_night(doc: dict) -> dict | None:
    """The cheapest room for the night (taxes included), or None when nothing is bookable."""
    res = (((doc.get("data") or {}).get("hotel") or {}).get("result") or {})
    prices = [o["price"]["amount"] for o in res.get("options") or []
              if (o.get("price") or {}).get("amount") and (o.get("price") or {}).get("currencyCode", "EUR") == "EUR"]
    return {"price": float(min(prices)), "min_stay": 1, "url": ""} if prices else None


def rotating_nights(key: str, start: date, days: int, per_pass: int, stride: int, now: datetime) -> list[date]:
    """A few nights per pass without stored state: each 4-hour pass shifts the walk by a day, so every night of the
    horizon comes round; the hotel's key spreads hotels over different nights."""
    import zlib
    shift = (int(now.timestamp() // (4 * 3600)) + zlib.crc32(key.encode())) % max(1, days)
    return sorted({start + timedelta(days=(shift + i * stride) % days) for i in range(per_pass)})


def calendar(engine: str, p: dict, ec: dict, get, today: date, days: int, adults: int, sleep=time.sleep,
             post=None, now: datetime | None = None) -> dict[str, dict]:
    nights = {}
    if engine == "simplebooking":
        sb = ec["simplebooking"]
        template = json.loads(Path(sb["request_template"]).read_text())
        for night in rotating_nights(p["code"], today, days, sb["nights_per_pass"], sb["night_stride"],
                                     now or datetime.now(timezone.utc)):
            r = post(sb["search_url"].format(hid=p["code"]), simplebooking_body(template, p["code"], night, adults))
            if r.status != 200:
                raise RuntimeError(f"simplebooking HTTP {r.status} {r.error[:80]}")
            got = simplebooking_night(r.json())
            if got:
                got["url"] = sb["book_url"].format(hid=p["code"], checkin=night.isoformat(),
                                                   checkout=(night + timedelta(days=1)).isoformat())
                nights[night.isoformat()] = got
            else:
                nights[night.isoformat()] = None     # asked, nothing bookable
            sleep(ec["pause_seconds"])
        return nights
    for first, last in months(today, days):
        url = seekda_url(ec, p["code"], first, last, adults) if engine == "seekda" else dedge_url(ec, p, first, last, adults)
        r = get(url)
        if r.status != 200:
            raise RuntimeError(f"{engine} HTTP {r.status} {r.error[:80]}")
        doc = r.json()
        nights.update(seekda_nights(doc) if engine == "seekda" else dedge_nights(doc))
        sleep(ec["pause_seconds"])
    return nights


def booking_link(engine: str, row: dict, p: dict, night: date, adults: int, ec: dict) -> str:
    """Where to book that night: D-EDGE gives its own link; seekda's booking page takes the dates."""
    if engine == "seekda":
        base = (row.get("booking_link") or row.get("website") or "").split("?")[0]
        return f"{base}?" + urlencode({"skd-property-code": p["code"], "skd-checkin": night.isoformat(),
                                        "skd-checkout": (night + timedelta(days=1)).isoformat(),
                                        "skd-total-adults": adults}) if base else ""
    return ""


# ---------- run ----------

def ids_due(sites: list[dict], known: dict, engines: list[str], today: date, retry_days: int) -> list[dict]:
    out = []
    for s in sites:
        if s.get("engine") not in engines or not (s.get("booking_link") or s.get("website")):
            continue
        k = known.get(s["pid"])
        if not k or (k["status"] != "ok" and (today - date.fromisoformat(k["checked_at"][:10])).days >= retry_days):
            out.append(s)
    return out


def find_codes(sites: list[dict], ec: dict, log=log, clock=time.monotonic, budget_s: float = 1e9) -> dict[str, dict | None]:
    """Open each hotel's booking page and note the engine's code from the calls it makes."""
    from playwright.sync_api import sync_playwright
    from .booking_session import launch_browser
    out, started = {}, clock()
    with sync_playwright() as pw:
        br = launch_browser(pw, True)
        ctx = br.new_context(user_agent=ec["user_agent"], locale="en-GB", viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        urls: list[str] = []
        page.on("request", lambda r: urls.append(r.url))
        for s in sites:
            if clock() - started > budget_s:
                break
            urls.clear()
            link = (s.get("booking_link") or s.get("website")).replace("&amp;", "&").replace("&#038;", "&")
            try:
                page.goto(link, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(ec["wait_ms"])
                urls.append(page.url)
            except Exception as e:  # noqa: BLE001
                log(f"{s['name'][:35]}: {str(e)[:80]}")
            out[s["pid"]] = code_from_requests(s["engine"], urls)
            log(f"{s['engine']:8} {s['name'][:35]:35} {out[s['pid']] and {k: v[:24] for k, v in out[s['pid']].items()}}")
        br.close()
    return out


def run(cfg: dict, part: str, max_minutes: float, get=None, finder=find_codes, today: date | None = None, log=log,
        clock=time.monotonic, sleep=time.sleep, post=None, now_dt: datetime | None = None) -> dict:
    ec, p = cfg["engines"], cfg["paths"]
    today = today or date.today()
    now_dt = now_dt or datetime.now(timezone.utc)
    now = now_dt.isoformat(timespec="seconds")
    watched = {w["pid"] for w in read_csv(p["watchlist"]) if not w.get("gone_since")}
    sites = [s for s in read_csv(p["official_sites"]) if s["pid"] in watched]
    by_pid = {s["pid"]: s for s in sites}
    all_ids = read_csv(p["seller_ids"])
    known = {r["pid"]: r for r in all_ids if r["seller"] == SELLER}
    stats = {"ids_due": 0, "ids_found": 0, "hotels_priced": 0, "nights": 0, "errors": 0}
    started = clock()
    if part in ("ids", "all"):
        todo = ids_due(sites, known, ec["live"], today, ec["retry_days"])[: ec["ids_per_run"]]
        stats["ids_due"] = len(todo)
        direct = {s["pid"]: code_from_requests(s["engine"], [s.get("booking_link") or "", s.get("website") or ""])
                  for s in todo}                     # some links name the engine's hotel code already
        need = [s for s in todo if not direct.get(s["pid"])]
        codes = finder(need, ec, log=log, budget_s=max_minutes * 60 * (0.5 if part == "all" else 1)) if need else {}
        codes.update({k: v for k, v in direct.items() if v})
        for s in todo:
            if s["pid"] not in codes:
                continue
            c = codes[s["pid"]]
            known[s["pid"]] = {"pid": s["pid"], "seller": SELLER, "seller_id": seller_id(s["engine"], c) if c else "",
                               "city_id": "", "seller_name": s["name"], "lat": "", "lon": "", "km": "", "similarity": "",
                               "status": "ok" if c else "not found", "checked_at": now}
            stats["ids_found"] += 1 if c else 0
        others = [r for r in all_ids if r["seller"] != SELLER]
        write_csv(sorted(others + list(known.values()), key=lambda r: (r["seller"], r["pid"])), p["seller_ids"], ID_FIELDS)
    if part in ("prices", "all"):
        sess = Session(ec["user_agent"], timeout=30)
        get = get or (lambda u: sess.request("GET", u))
        post = post or (lambda u, body: sess.post_json(u, body))
        adults = cfg["scan"]["adults"]
        new = []
        for r in sorted((r for r in known.values() if r["status"] == "ok" and r["pid"] in watched),
                        key=lambda r: r["pid"]):
            if clock() - started > max_minutes * 60:
                break
            engine, prm = parse_seller_id(r["seller_id"])
            try:
                nights = calendar(engine, prm, ec, get, today + timedelta(days=ec["min_lead_days"]), ec["horizon_days"],
                                  adults, sleep, post=post, now=now_dt)
            except Exception as e:  # noqa: BLE001
                stats["errors"] += 1
                log(f"{r['seller_name'][:35]}: {str(e)[:120]}")
                continue
            stats["hotels_priced"] += 1
            stats["nights"] += sum(1 for v in nights.values() if v)
            for night, v in nights.items():
                d = date.fromisoformat(night)
                if v is None:                        # asked and not bookable (per-stay engines)
                    new.append({"pid": r["pid"], "seller": SELLER, "seller_id": engine, "night": night, "price": "",
                                "price_excl": "", "price_total": "", "currency": "EUR", "available": "no",
                                "cancellation": "", "rooms_left": "", "url": "", "seen_at": now})
                    continue
                new.append({"pid": r["pid"], "seller": SELLER, "seller_id": engine, "night": night, "price": v["price"],
                            "price_excl": "", "price_total": "", "currency": "EUR", "available": "yes",
                            "cancellation": "", "rooms_left": "",
                            "url": v.get("url") or booking_link(engine, by_pid.get(r["pid"], {}), prm, d, adults, ec),
                            "seen_at": now})
            log(f"{engine:8} {r['seller_name'][:35]:35} {len(nights)} nights priced")
        old_all = read_csv(p["seller_prices"])
        mine = [x for x in old_all if x.get("seller") == SELLER]
        others = [x for x in old_all if x.get("seller") != SELLER]
        # a fresh full calendar replaces the hotel's old rows; per-stay engines add to them (a few nights per pass)
        full = {x["pid"] for x in new if x["seller_id"] in ("seekda", "d_edge")}
        merged = merge_prices([x for x in mine if x["pid"] not in full], new, today)
        write_csv(sorted(others + merged, key=lambda x: (x["seller"], x["pid"], x["night"])), p["seller_prices"], PRICE_FIELDS)
    stats["ids_coverage"] = f"{sum(1 for r in known.values() if r['status'] == 'ok')}/" \
                            f"{sum(1 for s in sites if s.get('engine') in ec['live'])} hotels on {', '.join(ec['live'])}"
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(config.DEFAULT_PATH))
    ap.add_argument("--part", choices=["ids", "prices", "all"], default="all")
    ap.add_argument("--max-minutes", type=float)
    args = ap.parse_args(argv)
    cfg = config.load(args.config)
    stats = run(cfg, args.part, args.max_minutes or cfg["engines"]["max_minutes"])
    log(f"engines: {stats}")
    out = Path(cfg["paths"]["out_dir"])
    out.mkdir(parents=True, exist_ok=True)
    (out / "engines_summary.md").write_text("## Official sites (booking engines)\n\n" +
                                            "\n".join(f"- {k}: {v}" for k, v in stats.items()) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
