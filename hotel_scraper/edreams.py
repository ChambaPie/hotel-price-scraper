"""eDreams (eDreams ODIGEO; Opodo and GO Voyages sell the same hotels from the same service): each watched hotel's own
eDreams price (settings: edreams). Owner, 2026-11-23: every vendor that sells the room itself. eDreams takes the
booking and the payment itself (its suppliers are bed banks, not other websites).

    ids     eDreams' own search-box suggestions (accommodation-autocomplete): hotels with eDreams' id; each candidate's
            position (its own geoAccommodationLocation) is checked by name and place (same rules as Agoda)
    prices  eDreams' own hotel search (searchAccommodation on its accommodation GraphQL), asked "around this hotel" the
            way its results page asks: our hotel comes first, with the hotels around it (often 180+), so one search
            also prices the other mapped hotels of the area

Calls are made from inside eDreams' page in Google Chrome with a window (its cookies and visit headers).
Prices per room per night in euros, taxes included (eDreams: TAXES_INCLUDED).

    python -m hotel_scraper.edreams [--part ids|prices|all] [--max-minutes 25]
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
from .seller_merge import priced

SELLER = "edreams"
BOARDS = {"BB": "breakfast", "HB": "half board", "FB": "full board", "AI": "all inclusive"}


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%H:%M:%S} {msg}", flush=True)


def suggestions(doc: dict) -> list[dict]:
    """Hotels among eDreams' search-box suggestions: [{id, name, where}] ("Hotel Artemide, Repubblica, Roma, ...")."""
    out = []
    for s in doc.get("suggestions") or []:
        if s.get("geoType") != "ACCOMMODATION" or not (s.get("id") or {}).get("value"):
            continue
        name, _, where = (s.get("text") or "").partition(", ")
        out.append({"id": str(s["id"]["value"]), "name": name, "where": where})
    return out


def position(doc: dict) -> tuple[float | None, float | None]:
    c = (((doc.get("data") or {}).get("geoAccommodationLocation") or {}).get("coordinates")) or {}
    return c.get("latitude"), c.get("longitude")


def offers(doc: dict) -> dict[str, dict]:
    """{eDreams hotel id: {available, price, breakfast, cancellation, rooms_left, name, lat, lon}} from a hotel search
    (one night): every hotel the search lists, each with eDreams' price for the room, taxes included."""
    out = {}
    for a in ((doc.get("data") or {}).get("searchAccommodation") or {}).get("accommodations") or []:
        pb, deal = a.get("priceBreakdown") or {}, a.get("accommodationDeal") or {}
        price = pb.get("pricePerNight") or pb.get("price")
        if not a.get("dedupId") or not price or price <= 0:
            continue
        c = deal.get("coordinates") or {}
        out[str(a["dedupId"])] = {
            "available": True, "price": round(price, 2), "name": deal.get("name") or "",
            "lat": c.get("latitude"), "lon": c.get("longitude"),
            "breakfast": BOARDS.get(a.get("boardType") or "", ""),
            "cancellation": "free cancellation" if deal.get("cancellationFree") == "TRUE" else "",
            "rooms_left": a.get("roomsLeft") if (a.get("roomsLeft") or 99) < 10 else "",
            "taxes": ((a.get("roomOffer") or {}).get("taxesAndFeesDescription")) or ""}
    return out


def search_body(template: dict, geo_id: str, night: date, adults: int) -> dict:
    """eDreams' own search request (captured from its results page, config edreams.search_template) around a hotel."""
    b = copy.deepcopy(template)
    r = b["variables"]["accommodationSearchRequest"]
    r.update({"checkInDate": night.isoformat(), "checkOutDate": (night + timedelta(days=1)).isoformat(),
              "roomRequests": [{"numAdults": adults, "childrenAges": []}], "buyPath": None,
              "geoAccommodation": {"geoId": str(geo_id), "geoType": "ACCOMMODATION"}})
    r.pop("dealsRecommendedId", None)
    return b


def location_body(template: dict, geo_id: str) -> dict:
    b = copy.deepcopy(template)
    b["variables"]["geoAccommodationLocationRequest"]["geoId"] = str(geo_id)
    return b


CALLS_JS = """async ([calls, headers]) => {
  const one = async (c) => {
    try {
      const h = Object.assign({'accept': 'application/json'}, c.body ? headers : {},
                              c.body ? {'content-type': 'application/json', 'edo-graphql-operation-name': c.body.operationName} : {});
      const r = await fetch(c.url, {method: c.body ? 'POST' : 'GET', headers: h, credentials: 'include',
                                    body: c.body ? JSON.stringify(c.body) : undefined});
      const t = await r.text();
      if (r.status !== 200) return {error: 'HTTP ' + r.status + ': ' + t.slice(0, 150)};
      const doc = JSON.parse(t);
      const s = ((doc.data || {}).searchAccommodation);
      if (s && s.accommodations) {        // only what is read: the answer is ~1 MB
        s.accommodations = s.accommodations.map(a => ({dedupId: a.dedupId, priceBreakdown: a.priceBreakdown,
          boardType: a.boardType, roomsLeft: a.roomsLeft, roomOffer: a.roomOffer,
          accommodationDeal: {name: (a.accommodationDeal || {}).name, coordinates: (a.accommodationDeal || {}).coordinates,
                              cancellationFree: (a.accommodationDeal || {}).cancellationFree}}));
      }
      return {doc};
    } catch (e) { return {error: String(e).slice(0, 200)}; }
  };
  return await Promise.all(calls.map(one));
}"""


class EdreamsPage:
    """eDreams' own calls, made from inside its page (its cookies and visit headers)."""
    def __init__(self, pw, cfg: dict, log=log):
        self.ec, self.log = cfg["edreams"], log
        from .seller_probe import accept_cookies, open_browser
        self.br, ctx = open_browser(pw, None, self.ec["browser"], {"accept_language": self.ec["accept_language"],
                                                                   "user_agent": self.ec.get("user_agent", "")})
        self.page = ctx.new_page()
        self.search_t = json.loads(Path(self.ec["search_template"]).read_text())
        self.location_t = json.loads(Path(self.ec["location_template"]).read_text())
        seen: dict = {}

        def note(r):                    # the page's own visit headers, sent with ours too
            if "/service/graphql" in r.url:
                for k in ("x-visit", "x-of1jsessionid"):
                    if r.headers.get(k):
                        seen[k] = r.headers[k]
        self.page.on("request", note)
        self.page.goto(self.ec["home"], wait_until="domcontentloaded", timeout=60000)
        self.page.wait_for_timeout(self.ec["warm_up_s"] * 1000)
        accept_cookies(self.page, cfg["seller_probe"]["cookie_buttons"])
        self.page.wait_for_timeout(2000)
        self.page.remove_listener("request", note)
        self.headers = seen
        if not seen:
            log("eDreams: no visit headers seen on its page (calls go without them)")

    def calls(self, items: list[dict]) -> list[dict]:
        return self.page.evaluate(CALLS_JS, [items, self.headers])

    def _one(self, item: dict) -> dict:
        got = self.calls([item])[0]
        if got.get("error"):
            raise RuntimeError(f"eDreams {got['error']}")
        return got["doc"]

    def suggest(self, q: str) -> list[dict]:
        return suggestions(self._one({"url": self.ec["suggest_url"].format(q=quote(q))}))

    def position(self, geo_id: str) -> tuple:
        return position(self._one({"url": self.ec["graphql"], "body": location_body(self.location_t, geo_id)}))

    def search(self, wanted: list[tuple[str, date, int]]) -> list[dict]:
        """[(eDreams hotel id, night, adults)] -> per search {offers, error}, several searches at once."""
        got = self.calls([{"url": self.ec["graphql"], "body": search_body(self.search_t, g, n, a)} for g, n, a in wanted])
        return [{"offers": offers(g["doc"]) if g.get("doc") else {}, "error": g.get("error", "")} for g in got]

    def close(self):
        self.br.close()


def find_id(page, w: dict, ec: dict, stats: dict, log=log) -> dict | None:
    best, tried = None, set()
    for tpl in ec["queries"]:
        q = tpl.format(name=w["name"], city=w.get("city", "")).strip()
        try:
            for c in page.suggest(q)[: ec["candidates_checked"]]:
                if c["id"] in tried:
                    continue
                tried.add(c["id"])
                lat, lon = page.position(c["id"])
                m = judge(w, {**c, "lat": lat, "lon": lon}, ec)
                if m and m["ok"]:
                    return m
                if m and (best is None or m["km"] < best["km"]):
                    best = m
        except Exception as e:  # noqa: BLE001
            stats["errors"] += 1
            log(f"{q[:40]}: {str(e)[:120]}")
    return best


def run(cfg: dict, part: str, max_minutes: float, page, today: date | None = None, log=log, clock=time.monotonic,
        now_dt: datetime | None = None) -> dict:
    ec, p = cfg["edreams"], cfg["paths"]
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
    adults = cfg["scan"]["adults"]

    if part in ("ids", "all"):
        todo = ids_due(watch, known, today, ec)[: ec["ids_per_run"]]
        stats["ids_due"] = len(todo)
        budget = max_minutes * 60 * (ec["ids_share"] if part == "all" else 1)
        for w in todo:
            if clock() - started > budget or stats["errors"] >= ec["stop_after_errors"]:
                break
            b = find_id(page, w, ec, stats, log) or {}
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
        new: dict[tuple, dict] = {}
        mapped = sorted((r for r in known.values() if r["status"] == "ok" and r["seller_id"]), key=lambda r: r["pid"])
        by_id = {r["seller_id"]: r for r in mapped}
        hotels = list(mapped)
        if hotels:                                # each pass starts further on, so every hotel comes round
            s = (int(now_dt.timestamp() // (4 * 3600)) * ec["hotels_per_pass"]) % len(hotels)
            hotels = (hotels[s:] + hotels[:s])[: ec["hotels_per_pass"]]
        start = today + timedelta(days=ec["min_lead_days"])
        days = ec["horizon_days"] - ec["min_lead_days"]
        tasks = [(r, n) for r in hotels
                 for n in rotating_nights(r["pid"], start, days, ec["nights_per_pass"], ec["night_stride"], now_dt)]
        k = ec["searches_at_once"]
        i = 0
        while i < len(tasks):
            if clock() - started > max_minutes * 60 or stats["errors"] >= ec["stop_after_errors"]:
                break
            batch = []
            while i < len(tasks) and len(batch) < k:
                r, n = tasks[i]
                i += 1
                if (r["pid"], n.isoformat()) not in new:      # not priced already by a search around a neighbour
                    batch.append((r, n))
            if not batch:
                continue
            try:
                answers = page.search([(r["seller_id"], n, adults) for r, n in batch])
            except Exception as e:  # noqa: BLE001
                stats["errors"] += 1
                log(f"search: {str(e)[:150]}")
                continue
            for (r, n), a in zip(batch, answers):
                if a["error"]:
                    stats["errors"] += 1
                    log(f"{r['seller_name'][:30]} {n}: {a['error'][:120]}")
                    continue
                stats["calls"] += 1
                for hid, x in a["offers"].items():
                    m = by_id.get(hid)
                    if m is None:
                        continue
                    key = (m["pid"], n.isoformat())
                    if key not in new:
                        stats["priced"] += 1
                        stats["available"] += 1
                    new[key] = {"pid": m["pid"], "seller": SELLER, "seller_id": hid, "night": n.isoformat(),
                                "price": x["price"], "price_excl": "", "price_total": "", "currency": "EUR",
                                "available": "yes", "cancellation": x["cancellation"],
                                "rooms_left": x["rooms_left"],
                                "url": ec["hotel_url"].format(id=hid, adults=adults, checkin=n.isoformat(),
                                                              checkout=(n + timedelta(days=1)).isoformat()),
                                "seen_at": now}
            if stats["calls"] and stats["calls"] % 20 < k:
                log(f"{stats['calls']} searches, {stats['available']} hotel-nights priced")
        old_all = read_csv(p["seller_prices"])
        ok = {r["pid"] for r in known.values() if r["status"] == "ok"}     # a hotel no longer matched: its prices go
        merged = merge_prices([x for x in old_all if x.get("seller") == SELLER and x["pid"] in ok and priced(x)],
                              list(new.values()), today)
        write_csv(sorted([x for x in old_all if x.get("seller") != SELLER] + merged,
                         key=lambda x: (x["seller"], x["pid"], x["night"])), p["seller_prices"], PRICE_FIELDS)
        stats["stored_nights"] = len(merged)
    stats["ids_coverage"] = f"{sum(1 for w in watch if known.get(w['pid'], {}).get('status') == 'ok')}/{len(watch)}"
    return stats


def summary_md(s: dict) -> str:
    return ("## eDreams\n\n" + f"Hotels found on eDreams: {s['ids_coverage']} (this pass: {s['ids_found']} found, "
            f"{s['ids_missed']} not found of {s['ids_due']} looked up). Prices: {s['calls']} searches, "
            f"{s['available']} hotel-nights priced; {s.get('stored_nights', 0)} nights stored; {s['errors']} errors.\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(config.DEFAULT_PATH))
    ap.add_argument("--part", choices=["ids", "prices", "all"], default="all")
    ap.add_argument("--max-minutes", type=float)
    args = ap.parse_args(argv)
    cfg = config.load(args.config)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        page = EdreamsPage(pw, cfg)
        try:
            stats = run(cfg, args.part, args.max_minutes or cfg["edreams"]["max_minutes"], page)
        finally:
            page.close()
    log(f"edreams: {stats}")
    out = Path(cfg["paths"]["out_dir"])
    out.mkdir(parents=True, exist_ok=True)
    (out / "edreams_summary.md").write_text(summary_md(stats), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
