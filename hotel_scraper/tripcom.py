"""Trip.com: each watched hotel's own price on Trip.com (settings: tripcom). Owner, 2026-11-23: every vendor that
sells the room itself.

    ids     Trip.com's own search-box suggestions (getHotelKeywords: what the box asks while you type) give hotels with
            their position and city; each candidate is checked by name and place (same rules as Agoda)
    prices  Trip.com's own results list (fetchDynamicRefreshList, the call its list page makes) asked again from inside
            the page for the hotel and one night; a few nights per hotel per pass, rotating through the horizon

Prices per room per night in euros: price = with taxes and fees but before the city tax paid at the hotel (what
Booking.com and Agoda show), price_excl = before taxes and fees, price_total = everything incl. the city tax.

    python -m hotel_scraper.tripcom [--part ids|prices|all] [--max-minutes 30]
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import config
from .agoda import ID_FIELDS, PRICE_FIELDS, ids_due, judge, merge_prices
from .engines import rotating_nights
from .scan import read_csv, write_csv

SELLER = "trip_com"


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%H:%M:%S} {msg}", flush=True)


def money(text) -> float | None:
    """Trip.com's English amounts: "€188.77" -> 188.77, "€1,204" -> 1204.0."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(text))
    return round(float(m.group(0).replace(",", "")), 2) if m else None


def keywords(doc: dict) -> list[dict]:
    """Hotels among Trip.com's suggestions (landmarks, cities... left out): [{id, name, lat, lon, city_id, country_id}]."""
    out = []
    for k in (((doc.get("data") or {}).get("mainKeywordList") or {}).get("keywords") or []):
        ki = (k.get("keyword") or {}).get("keywordContentInfo") or {}
        if ki.get("tripType") != "H" or not ki.get("keywordId"):
            continue
        coords = {c.get("coordinateType"): c for c in ki.get("coordinateItemList") or []
                  if c.get("latitude") not in (None, "", "-1", -1)}
        g = coords.get("GOOGLE") or coords.get("NORMAL")
        city = ((k.get("controlInfo") or {}).get("regionInfo") or {}).get("basicCityModel") or {}
        out.append({"id": int(ki["keywordId"]), "name": ki.get("keyword") or "",
                    "lat": float(g["latitude"]) if g else None, "lon": float(g["longitude"]) if g else None,
                    "city_id": city.get("cityId"), "country_id": city.get("countryId")})
    return out


def hotels(doc: dict) -> list[dict]:
    """Hotels in a results-list answer: [{id, name, available, price, price_excl, price_total, currency}] (the cheapest
    room Trip.com shows first). No price, or a hidden one: not available that night."""
    out = []
    for h in ((doc.get("data") or {}).get("hotelList") or []):
        hi = h.get("hotelInfo") or {}
        r0 = (h.get("roomInfo") or [{}])[0] or {}
        pi, layer = r0.get("priceInfo") or {}, r0.get("priceInfoLayer") or {}
        pay = layer.get("payInfo") or {}
        available = bool(pi.get("price")) and not pi.get("isHiddenPrice")
        out.append({"id": int((hi.get("summary") or {}).get("hotelId") or 0),
                    "name": (hi.get("nameInfo") or {}).get("name", ""),
                    "available": available,
                    "price": money((pay.get("total") or {}).get("content")) if available else None,
                    "price_excl": (money((pay.get("roomQuantityDays") or {}).get("content")) or money(pi.get("price")))
                    if available else None,
                    "price_total": money((layer.get("total") or {}).get("content")) if available else None,
                    "currency": pi.get("currency") or ""})
    return out


def asked_for(doc: dict) -> set[int]:
    """The hotels a results-list answer says it was asked about (its own record of the hotel filters)."""
    info = ((doc.get("data") or {}).get("hotelListAddtionInfo") or {})
    return {int(f["value"]) for f in info.get("combineFilters") or [] if f.get("type") == "31" and str(f.get("value", "")).isdigit()}


def keywords_body(template: dict, query: str) -> dict:
    b = copy.deepcopy(template)
    b["queryInfo"] = {**b.get("queryInfo", {}), "keyword": query}
    return b


def list_body(template: dict, hotel_ids: list[int], city_id: int, country_id: int | None, night: date,
              adults: int, name: str = "") -> dict:
    """Trip.com's own results-list request (captured, config tripcom.list_template) for these hotels and one night."""
    b = copy.deepcopy(template)
    b["date"] = {**b.get("date", {}), "dateType": 1,
                 "dateInfo": {"checkInDate": night.strftime("%Y%m%d"),
                              "checkOutDate": (night + timedelta(days=1)).strftime("%Y%m%d")}}
    geo = {"cityId": city_id, **({"countryId": country_id} if country_id else {})}
    b["destination"] = {**{k: v for k, v in b.get("destination", {}).items() if k != "keyword"}, "type": 1, "geo": geo}
    if name:                                 # the page sends the hotel's name with its id (run 45277915620: without
        b["destination"]["keyword"] = {"word": name.replace(" ", "+")}     # it, 404 answers without our hotel)
    b["filters"] = [f for f in b.get("filters", []) if f.get("type") not in ("31", "29")] + \
        [{"type": "31", "title": "", "value": str(i), "filterId": f"31|{i}"} for i in hotel_ids] + \
        [{"filterId": "29|1", "type": "29", "value": f"1|{adults}"}]
    b["paging"] = {**b.get("paging", {}), "pageIndex": 1, "pageSize": max(10, len(hotel_ids))}
    # "dynamic refresh" re-prices the list a page already has (its token and session): without them Trip.com is asked
    # for a new list (run 45333656075: with them, every answer was the captured hotel, 912 of 912)
    b.pop("dynamicRefresh", None)
    import uuid
    b["extraFilter"] = {**b.get("extraFilter", {}), "sessionId": uuid.uuid4().hex}
    return b


def list_url(tpl: str, hotel_id: int, city_id: int, country_id, night: date, adults: int) -> str:
    """Trip.com's own results page for one hotel and night (what its search box opens)."""
    return tpl.format(id=hotel_id, city=city_id, country=country_id or 0, checkin=night.isoformat(),
                      checkout=(night + timedelta(days=1)).isoformat(), adults=adults)


class TripPage:
    """A browser page on trip.com: its own suggestion and results-list calls, asked again from inside the page (so
    they carry the page's own cookies and headers)."""
    def __init__(self, pw, cfg: dict, log=log):
        self.tc, self.log = cfg["tripcom"], log
        bc = self.tc.get("browser") or {}
        if bc.get("headed") or bc.get("channel"):
            from .seller_probe import open_browser
            self.br, ctx = open_browser(pw, None, bc, {"accept_language": self.tc["accept_language"],
                                                       "user_agent": self.tc["user_agent"]})
        else:
            from .booking_session import launch_browser
            self.br = launch_browser(pw, True)
            ctx = self.br.new_context(user_agent=self.tc["user_agent"], locale="en-GB",
                                      viewport={"width": 1366, "height": 900},
                                      extra_http_headers={"Accept-Language": self.tc["accept_language"]})
        self.page = ctx.new_page()
        self.page.goto(self.tc["home"], wait_until="domcontentloaded", timeout=60000)
        self.page.wait_for_timeout(self.tc["wait_ms"])
        self.kw_template = json.loads(Path(self.tc["keywords_template"]).read_text())
        self.list_template = json.loads(Path(self.tc["list_template"]).read_text())

    def close(self):
        self.br.close()

    def _post(self, url: str, body: dict) -> dict:
        status, text = self.page.evaluate("""async ([url, body]) => {
            const r = await fetch(url, {method: 'POST', body, credentials: 'include',
                                        headers: {'content-type': 'application/json'}});
            return [r.status, await r.text()]; }""", [url, json.dumps(body)])
        if status != 200:
            raise RuntimeError(f"Trip.com HTTP {status}: {text[:150]}")
        return json.loads(text)

    def suggest(self, query: str) -> list[dict]:
        return keywords(self._post(self.tc["keywords_url"], keywords_body(self.kw_template, query)))

    def nights(self, hotel_ids: list[int], city_id: int, country_id: int | None, night: date, adults: int,
               name: str = "") -> list[dict]:
        """Our hotels' prices for the night: Trip.com's list call asked again; when its answer doesn't have our hotel,
        Trip.com's own results page for the hotel and night (and every call after that in this pass)."""
        if not getattr(self, "pages_mode", self.tc.get("price_mode") == "pages"):
            doc = self._post(self.tc["list_url"], list_body(self.list_template, hotel_ids, city_id, country_id, night,
                                                            adults, name))
            self.last = doc
            got = hotels(doc)
            if any(h["id"] in hotel_ids for h in got):
                return got
            self.misses = getattr(self, "misses", 0) + 1
            if self.misses < self.tc["replay_misses_before_pages"]:
                return got
            self.log("Trip.com's list call doesn't name our hotels: opening its results page instead")
            self.pages_mode = True
        return self.page_night(hotel_ids[0], city_id, country_id, night, adults)

    def page_night(self, hotel_id: int, city_id: int, country_id, night: date, adults: int) -> list[dict]:
        """Open Trip.com's results page for the hotel and night and read the list answers it loads itself."""
        seen: list = []

        def keep(r):
            if "/34951/fetch" in r.url:      # fetchHotelList / fetchDynamicRefreshList
                try:
                    seen.append(r.json())
                except Exception:  # noqa: BLE001
                    pass
        self.page.on("response", keep)
        try:
            self.page.goto(list_url(self.tc["list_page"], hotel_id, city_id, country_id, night, adults),
                           wait_until="domcontentloaded", timeout=60000)
            for i in range(int(self.tc["page_wait_s"] * 2)):
                if any(h["id"] == hotel_id for d in seen for h in hotels(d)):
                    break
                if i in (4, 12):                  # a little scrolling, as a person would: the list loads its prices
                    try:
                        self.page.mouse.wheel(0, 600)
                    except Exception:  # noqa: BLE001
                        pass
                self.page.wait_for_timeout(500)
        finally:
            self.page.remove_listener("response", keep)
        got = page_answer(seen, hotel_id)
        if got and not got[0]["available"] and getattr(self, "shown", 0) < 2:   # what the visitor saw, kept to check
            self.shown = getattr(self, "shown", 0) + 1
            try:
                out = Path(self.tc.get("debug_dir", "out"))
                out.mkdir(parents=True, exist_ok=True)
                (out / f"tripcom_no_room_{self.shown}.txt").write_text(f"#URL {self.page.url}\n" +
                                                                         self.page.inner_text("body")[:20000])
                self.page.screenshot(path=str(out / f"tripcom_no_room_{self.shown}.png"))
            except Exception:  # noqa: BLE001
                pass
        return got or self._nothing(seen)

    def _nothing(self, seen):
        self.last = seen[-1] if seen else {"page": self.page.url, "text": self.page.inner_text("body")[:3000]}
        return []


def page_answer(seen: list[dict], hotel_id: int) -> list[dict]:
    """What Trip.com's own results page showed for our hotel: its prices, or, when the page's own answer about exactly
    our hotel was an empty list, nothing to sell that night (the visitor sees no room either; run of 2026-11-24 11:00:
    32 of 42 pages)."""
    for d in reversed(seen):
        got = hotels(d)
        if any(h["id"] == hotel_id for h in got):
            return got
    for d in reversed(seen):
        if not hotels(d) and hotel_id in asked_for(d):
            return [{"id": hotel_id, "name": "", "available": False, "price": None, "price_excl": None,
                     "price_total": None, "currency": ""}]
    return []


def find_id(page, w: dict, tc: dict, stats: dict, sleep=time.sleep, log=log) -> dict | None:
    """Trip.com's hotel for a watched hotel: the suggestions for its name and town (then the name alone), each judged
    by name and place. Returns the best judged candidate (ok or not) or None."""
    best = None
    for tpl in tc["queries"]:
        q = tpl.format(name=w["name"], city=w.get("city", "")).strip()
        try:
            for c in page.suggest(q):
                m = judge(w, c, tc)
                if m and m["ok"]:
                    return m
                if m and (best is None or m["km"] < best["km"]):
                    best = m
        except Exception as e:  # noqa: BLE001
            stats["errors"] += 1
            log(f"{q[:40]}: {str(e)[:120]}")
        sleep(tc["pause_seconds"])
    return best


def rotate(rows: list, now: datetime, per_pass: int) -> list:
    """The rows starting where the previous 4-hour pass left off (stateless): every hotel comes round in turn."""
    if not rows:
        return rows
    k = (int(now.timestamp() // (4 * 3600)) * max(1, per_pass)) % len(rows)
    return rows[k:] + rows[:k]


def run(cfg: dict, part: str, max_minutes: float, page, today: date | None = None, log=log, clock=time.monotonic,
        sleep=time.sleep, now_dt: datetime | None = None) -> dict:
    tc, p = cfg["tripcom"], cfg["paths"]
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
        todo = ids_due(watch, known, today, tc)[: tc["ids_per_run"]]
        stats["ids_due"] = len(todo)
        budget = max_minutes * 60 * (tc["ids_share"] if part == "all" else 1)
        for w in todo:
            if clock() - started > budget or stats["errors"] >= tc["stop_after_errors"]:
                break
            best = find_id(page, w, tc, stats, sleep, log)
            ok = bool(best and best["ok"])
            stats["ids_found" if ok else "ids_missed"] += 1
            b = best or {}
            known[w["pid"]] = {"pid": w["pid"], "seller": SELLER, "seller_id": b.get("id", ""),
                               "city_id": f"{b['city_id']}|{b.get('country_id') or ''}" if b.get("city_id") else "",
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
        mapped = sorted((r for r in known.values() if r["status"] == "ok" and r["city_id"] and r["seller_id"]),
                        key=lambda r: r["pid"])
        start = today + timedelta(days=tc["min_lead_days"])
        days = tc["horizon_days"] - tc["min_lead_days"]
        for r in rotate(mapped, now_dt, tc["hotels_per_pass"]):
            if clock() - started > max_minutes * 60 or stats["errors"] >= tc["stop_after_errors"]:
                break
            city, _, country = r["city_id"].partition("|")
            hid = int(r["seller_id"])
            for night in rotating_nights(r["pid"], start, days, tc["nights_per_pass"], tc["night_stride"], now_dt):
                try:
                    got = {x["id"]: x for x in page.nights([hid], int(city), int(country) if country else None,
                                                           night, adults, r["seller_name"])}
                    stats["calls"] += 1
                except Exception as e:  # noqa: BLE001
                    stats["errors"] += 1
                    log(f"{r['seller_name'][:30]} {night}: {str(e)[:120]}")
                    sleep(tc["pause_seconds"])
                    continue
                x = got.get(hid)
                sleep(tc["pause_seconds"])
                if x is None:               # Trip.com listed other hotels only: no answer for ours that night
                    stats["missing"] = stats.get("missing", 0) + 1
                    if stats["missing"] <= 3:     # what came back instead (kept to see why)
                        log(f"{r['seller_name'][:30]} {night}: not in the answer ({list(got)[:5]})")
                        raw = getattr(page, "last", None)
                        if raw is not None and cfg["paths"].get("out_dir"):
                            out = Path(cfg["paths"]["out_dir"])
                            out.mkdir(parents=True, exist_ok=True)
                            (out / f"tripcom_miss_{stats['missing']}.json").write_text(json.dumps(raw)[:2_000_000])
                    continue
                stats["priced"] += 1
                stats["available"] += 1 if x["available"] else 0
                new.append({"pid": r["pid"], "seller": SELLER, "seller_id": r["seller_id"], "night": night.isoformat(),
                            "price": _blank(x["price"]), "price_excl": _blank(x["price_excl"]),
                            "price_total": _blank(x["price_total"]), "currency": x["currency"] or tc["currency"],
                            "available": "yes" if x["available"] else "no", "cancellation": "", "rooms_left": "",
                            "url": tc["hotel_url"].format(id=hid, city=city, checkin=night.isoformat(),
                                                          checkout=(night + timedelta(days=1)).isoformat(),
                                                          adults=adults),
                            "seen_at": now})
        old_all = read_csv(p["seller_prices"])
        vf = tc.get("no_rows_valid_from", "")       # "not available" rows from the empty list answers (run 45374257349)
        mine = [x for x in old_all if x.get("seller") == SELLER
                and not (vf and x.get("available") == "no" and (x.get("seen_at") or "") < vf)]
        merged = merge_prices(mine, new, today)
        write_csv(sorted([x for x in old_all if x.get("seller") != SELLER] + merged,
                         key=lambda x: (x["seller"], x["pid"], x["night"])), p["seller_prices"], PRICE_FIELDS)
        stats["stored_nights"] = len(merged)
    stats["ids_coverage"] = f"{sum(1 for w in watch if known.get(w['pid'], {}).get('status') == 'ok')}/{len(watch)}"
    return stats


def _blank(v):
    return "" if v is None else v


def summary_md(s: dict) -> str:
    return ("## Trip.com\n\n" + f"Hotels found on Trip.com: {s['ids_coverage']} (this pass: {s['ids_found']} found, "
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
        page = TripPage(pw, cfg)
        try:
            stats = run(cfg, args.part, args.max_minutes or cfg["tripcom"]["max_minutes"], page)
        finally:
            page.close()
    log(f"trip.com: {stats}")
    out = Path(cfg["paths"]["out_dir"])
    out.mkdir(parents=True, exist_ok=True)
    (out / "tripcom_summary.md").write_text(summary_md(stats), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
