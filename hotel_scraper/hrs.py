"""HRS (and hotel.de, the same company: same search service, same offers): each watched hotel's own price on HRS
(settings: hrs). Owner, 2026-11-23: every vendor that sells the room itself.

    ids     HRS's own search-box suggestions (services.hrs-api.com/locations: what the box asks while you type) give
            hotels with their position; each candidate is checked by name and place (same rules as Agoda)
    prices  HRS's own hotel search (the call its results list makes) for a list of hotel ids and one night: many
            hotels per call, a few nights per pass rotating through the horizon

Prices per room per night in euros with VAT (price), before VAT (price_excl); breakfast included or not as HRS says.

    python -m hotel_scraper.hrs [--part ids|prices|all] [--max-minutes 30]
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from . import config
from .agoda import ID_FIELDS, PRICE_FIELDS, ids_due, judge, merge_prices
from .engines import rotating_nights
from .scan import read_csv, write_csv

SELLER = "hrs"


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%H:%M:%S} {msg}", flush=True)


def locations(doc: list) -> list[dict]:
    """Hotels among HRS's suggestions: [{id, name, lat, lon, where}]."""
    out = []
    for x in doc or []:
        if x.get("group") != "HOTEL" or not x.get("hotelId"):
            continue
        out.append({"id": int(x["hotelId"]), "name": x.get("name") or "", "lat": x.get("latitude"),
                    "lon": x.get("longitude"), "where": x.get("additionalName") or "",
                    "city_id": x.get("locationId") or ""})
    return out


def room_type(adults: int) -> str:
    return "SINGLEROOM" if adults == 1 else "DOUBLEROOM"


def search_body(template: dict, hotel_ids: list[int], night: date, adults: int) -> dict:
    """HRS's own hotel search (captured from its results page, config hrs.search_template) for these hotels, one night."""
    b = copy.deepcopy(template)
    rp = b["requestParameter"]
    rp["dateRange"] = {"fromDate": night.isoformat(), "toDate": (night + timedelta(days=1)).isoformat()}
    rp["roomRequests"] = [{"roomNumber": 1, "roomType": room_type(adults),
                           "occupancy": {"adults": [{"extraBed": False} for _ in range(adults)], "children": []}}]
    b["findParameter"] = {"matchGroup": "CITY", "perimeter": 0, "maxPerimeter": 0, "matchType": "HOTEL_IDS",
                          "hotelIds": [int(i) for i in hotel_ids]}
    b.setdefault("options", {})["skipHotelsIfNoValidOffer"] = False
    return b


def offers(doc: dict, adults: int) -> dict[int, dict]:
    """{hotel id: {available, price, price_excl, breakfast}} for every hotel asked: an offer for the room asked, or
    not available (HRS: NO_VALID_OFFER, or no offer for that room)."""
    out: dict[int, dict] = {}
    want = room_type(adults)
    for hid in doc.get("requestedHotels") or []:
        out[int(hid)] = {"available": False, "price": None, "price_excl": None, "breakfast": ""}
    for h in doc.get("hotels") or []:
        hid = int(h.get("hotelId") or 0)
        o = ((h.get("hotelOffer") or {}).get("avgOffers") or {}).get(want)
        if not o or (o.get("pricePerNightAndRoom") or {}).get("currency", "EUR") != "EUR":
            out[hid] = {"available": False, "price": None, "price_excl": None, "breakfast": ""}
            continue
        if len((o.get("roomOccupancies") or [{}])[0].get("adults") or []) < adults:
            out[hid] = {"available": False, "price": None, "price_excl": None, "breakfast": ""}
            continue
        p = o["pricePerNightAndRoom"]
        meal = o.get("meal") or {}
        out[hid] = {"available": True, "price": round(float(p["amount"]), 2),
                    "price_excl": round(float(p["amountNet"]), 2) if p.get("amountNet") is not None else None,
                    "breakfast": "included" if meal.get("availability") == "INCLUSIVE" else
                    ("extra" if meal.get("availability") else "")}
    return out


def booking_url(tpl: str, hotel_id, night: date, adults: int) -> str:
    out = night + timedelta(days=1)
    return tpl.format(id=hotel_id, d=night.day, m=night.month, y=night.year, d2=out.day, m2=out.month, y2=out.year,
                      adults=adults, room=room_type(adults), singles=1 if adults == 1 else 0,
                      doubles=0 if adults == 1 else 1)


class HrsPage:
    """A browser page on hrs.com: its own suggestion and hotel-search calls, asked again from inside the page."""
    def __init__(self, pw, cfg: dict, log=log):
        self.hc, self.log = cfg["hrs"], log
        bc = self.hc.get("browser") or {}
        if bc.get("headed") or bc.get("channel"):         # Google Chrome with a window, like a person's (Akamai)
            from .seller_probe import open_browser
            self.br, ctx = open_browser(pw, None, bc, {"accept_language": self.hc["accept_language"],
                                                       "user_agent": self.hc["user_agent"]})
        else:
            from .booking_session import launch_browser
            self.br = launch_browser(pw, True)
            ctx = self.br.new_context(user_agent=self.hc["user_agent"], locale="en-GB",
                                      viewport={"width": 1366, "height": 900},
                                      extra_http_headers={"Accept-Language": self.hc["accept_language"]})
        self.page = ctx.new_page()
        self.headers: dict = {}
        self.search_headers: dict = {}
        self.captured = None
        self.page.on("request", self._note)
        self.page.goto(self.hc["home"], wait_until="domcontentloaded", timeout=60000)
        self.page.wait_for_timeout(self.hc["wait_ms"])
        self.template = json.loads(Path(self.hc["search_template"]).read_text())

    def _note(self, r):
        """The headers HRS's own page sends to its services (kept to send the same); its own hotel search kept whole."""
        if self.hc["search_url"] in r.url or "hrs-api.com" in r.url:
            self.headers = {k: v for k, v in r.headers.items() if k.lower() not in ("content-length", "cookie")}
        if self.hc["search_url"] in r.url and r.method == "POST":
            try:
                self.captured = json.loads(r.post_data or "{}")
                self.search_headers = {k: v for k, v in r.headers.items()
                                       if k.lower() not in ("content-length", "cookie", "host")}
            except Exception:  # noqa: BLE001
                pass

    def warm_up(self, hotel_id, night: date, adults: int) -> bool:
        """Open HRS's own results page for one hotel and night, so the page makes its hotel search (run 45370159785:
        the stored request was refused, HTTP 400): its body and headers are then the ones repeated."""
        self.page.goto(booking_url(self.hc["hotel_url"], hotel_id, night, adults), wait_until="domcontentloaded",
                       timeout=60000)
        for _ in range(int(self.hc["warm_up_wait_s"] * 2)):
            if self.captured:
                break
            self.page.wait_for_timeout(500)
        if self.captured:
            self.template = self.captured
            self.log("took HRS's own hotel search from its results page")
            return True
        return False

    def close(self):
        self.br.close()

    def _fetch(self, method: str, url: str, body: dict | None = None):
        if method == "POST":
            # the search service refuses the page's own fetch from a script ("Failed to fetch", runs 45280616865 and
            # 45333656075): send it from the browser's network stack instead, with the page's cookies and origin
            hdr = self.search_headers or {"content-type": "application/json", "accept": "application/json",
                                          "origin": self.hc["origin"], "referer": self.hc["home"],
                                          **{k: v for k, v in self.headers.items() if k.lower().startswith("x-")}}
            r = self.page.request.post(url, data=json.dumps(body), timeout=45000, headers=hdr)
            if r.status != 200:
                raise RuntimeError(f"HRS HTTP {r.status}: {r.text()[:150]}")
            return r.json()
        status, text = self.page.evaluate("""async ([m, url, body, extra]) => {
            const h = Object.assign({'content-type': 'application/json', 'accept': 'application/json'}, extra || {});
            const r = await fetch(url, {method: m, body: body, headers: h});
            return [r.status, await r.text()]; }""",
            [method, url, json.dumps(body) if body is not None else None,
             {k: v for k, v in self.headers.items() if k.lower().startswith("x-")}])
        if status != 200:
            raise RuntimeError(f"HRS HTTP {status}: {text[:150]}")
        return json.loads(text)

    def suggest(self, query: str) -> list[dict]:
        return locations(self._fetch("GET", self.hc["locations_url"].format(q=quote(query))))

    def prices(self, hotel_ids: list[int], night: date, adults: int) -> dict[int, dict]:
        return offers(self._fetch("POST", self.hc["search_url"], search_body(self.template, hotel_ids, night, adults)),
                      adults)


def find_id(page, w: dict, hc: dict, stats: dict, sleep=time.sleep, log=log) -> dict | None:
    best = None
    for tpl in hc["queries"]:
        q = tpl.format(name=w["name"], city=w.get("city", "")).strip()
        try:
            for c in page.suggest(q):
                m = judge(w, c, hc)
                if m and m["ok"]:
                    return m
                if m and (best is None or m["km"] < best["km"]):
                    best = m
        except Exception as e:  # noqa: BLE001
            stats["errors"] += 1
            log(f"{q[:40]}: {str(e)[:120]}")
        sleep(hc["pause_seconds"])
    return best


def run(cfg: dict, part: str, max_minutes: float, page, today: date | None = None, log=log, clock=time.monotonic,
        sleep=time.sleep, now_dt: datetime | None = None) -> dict:
    hc, p = cfg["hrs"], cfg["paths"]
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
        todo = ids_due(watch, known, today, hc)[: hc["ids_per_run"]]
        stats["ids_due"] = len(todo)
        budget = max_minutes * 60 * (hc["ids_share"] if part == "all" else 1)
        for w in todo:
            if clock() - started > budget or stats["errors"] >= hc["stop_after_errors"]:
                break
            b = find_id(page, w, hc, stats, sleep, log) or {}
            ok = bool(b.get("ok"))
            stats["ids_found" if ok else "ids_missed"] += 1
            known[w["pid"]] = {"pid": w["pid"], "seller": SELLER, "seller_id": b.get("id", ""),
                               "city_id": b.get("city_id", ""), "seller_name": b.get("name", ""),
                               "lat": b.get("lat", ""), "lon": b.get("lon", ""), "km": b.get("km", ""),
                               "similarity": b.get("similarity", ""), "status": "ok" if ok else "not found",
                               "checked_at": now}
            log(f"id {w['name'][:35]}: {known[w['pid']]['status']} {b.get('id', '')} {b.get('name', '')[:30]} "
                f"{b.get('km', '')} km")
        others = [r for r in all_ids if r["seller"] != SELLER]
        write_csv(sorted(others + list(known.values()), key=lambda r: (r["seller"], r["pid"])), p["seller_ids"],
                  ID_FIELDS)

    if part in ("prices", "all"):
        new, adults = [], cfg["scan"]["adults"]
        mapped = sorted((r for r in known.values() if r["status"] == "ok" and r["seller_id"]), key=lambda r: r["pid"])
        by_id = {int(r["seller_id"]): r for r in mapped}
        start = today + timedelta(days=hc["min_lead_days"])
        days = hc["horizon_days"] - hc["min_lead_days"]
        ids = list(by_id)
        if ids and hasattr(page, "warm_up"):
            try:
                page.warm_up(ids[0], start + timedelta(days=30), adults)
            except Exception as e:  # noqa: BLE001
                log(f"HRS results page: {str(e)[:120]}")
        batches = [ids[i: i + hc["hotels_per_call"]] for i in range(0, len(ids), hc["hotels_per_call"])]
        # one call prices many hotels for one night: every hotel gets the same nights this pass, next pass the next ones
        for night in rotating_nights("hrs", start, days, hc["nights_per_pass"], hc["night_stride"], now_dt):
            for batch in batches:
                if clock() - started > max_minutes * 60 or stats["errors"] >= hc["stop_after_errors"]:
                    break
                try:
                    got = page.prices(batch, night, adults)
                    stats["calls"] += 1
                except Exception as e:  # noqa: BLE001
                    stats["errors"] += 1
                    log(f"{night} ({len(batch)} hotels): {str(e)[:120]}")
                    sleep(hc["pause_seconds"])
                    continue
                for hid in batch:
                    x = got.get(hid)
                    if x is None:
                        continue
                    r = by_id[hid]
                    stats["priced"] += 1
                    stats["available"] += 1 if x["available"] else 0
                    new.append({"pid": r["pid"], "seller": SELLER, "seller_id": hid, "night": night.isoformat(),
                                "price": "" if x["price"] is None else x["price"],
                                "price_excl": "" if x["price_excl"] is None else x["price_excl"], "price_total": "",
                                "currency": "EUR", "available": "yes" if x["available"] else "no",
                                "cancellation": "", "rooms_left": "",
                                "url": booking_url(hc["hotel_url"], hid, night, adults), "seen_at": now})
                sleep(hc["pause_seconds"])
        old_all = read_csv(p["seller_prices"])
        merged = merge_prices([x for x in old_all if x.get("seller") == SELLER], new, today)
        write_csv(sorted([x for x in old_all if x.get("seller") != SELLER] + merged,
                         key=lambda x: (x["seller"], x["pid"], x["night"])), p["seller_prices"], PRICE_FIELDS)
        stats["stored_nights"] = len(merged)
    stats["ids_coverage"] = f"{sum(1 for w in watch if known.get(w['pid'], {}).get('status') == 'ok')}/{len(watch)}"
    return stats


def summary_md(s: dict) -> str:
    return ("## HRS (and hotel.de)\n\n" + f"Hotels found on HRS: {s['ids_coverage']} (this pass: {s['ids_found']} found, "
            f"{s['ids_missed']} not found of {s['ids_due']} looked up). Prices: {s['calls']} calls, {s['priced']} "
            f"hotel-nights answered, {s['available']} available; {s.get('stored_nights', 0)} nights stored; "
            f"{s['errors']} errors.\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(config.DEFAULT_PATH))
    ap.add_argument("--part", choices=["ids", "prices", "all"], default="all")
    ap.add_argument("--max-minutes", type=float)
    args = ap.parse_args(argv)
    cfg = config.load(args.config)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        page = HrsPage(pw, cfg)
        try:
            stats = run(cfg, args.part, args.max_minutes or cfg["hrs"]["max_minutes"], page)
        finally:
            page.close()
    log(f"hrs: {stats}")
    out = Path(cfg["paths"]["out_dir"])
    out.mkdir(parents=True, exist_ok=True)
    (out / "hrs_summary.md").write_text(summary_md(stats), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
