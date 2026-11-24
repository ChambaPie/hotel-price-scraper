"""Agoda: each watched hotel's own price on Agoda (settings: agoda). Owner, 2026-11-23: every seller that sells the room
itself counts, not only Booking.com.

    ids     once per hotel: Agoda's own search-box suggestions (what the box asks while you type) give candidates, each
            checked by name and place through a city search with it pinned; kept in data/seller_ids.csv
    prices  Agoda's city search (the call its results page makes) asked again from inside the page for one city and one
            night, with every watched hotel of that city pinned: one call prices all of them. Nights rotate through the
            next 24 months, a few per city per pass. Rows go to seller_prices.csv (snapshots branch).

Prices are per room per night for the party (2 adults), in euros: price = with VAT, before city tax (what Booking.com
shows), price_excl = before taxes, price_total = everything Agoda charges.

    python -m hotel_scraper.agoda [--part ids|prices|all] [--max-minutes 40]
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
from urllib.parse import quote

from . import config, geo
from .scan import read_csv, write_csv

SELLER = "agoda"
LEADS = [(0, 30), (31, 90), (91, 180), (181, 365), (366, 545), (546, 10_000)]   # availability by lead time (summary)
ID_FIELDS = ["pid", "seller", "seller_id", "city_id", "seller_name", "lat", "lon", "km", "similarity", "status", "checked_at"]
PRICE_FIELDS = ["pid", "seller", "seller_id", "night", "price", "price_excl", "price_total", "currency", "available",
                "cancellation", "rooms_left", "url", "seen_at"]


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%H:%M:%S} {msg}", flush=True)


# ---------- reading Agoda's answers ----------

def _num(v):
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def properties(doc: dict) -> list[dict]:
    """Every property in a citySearch answer: [{id, name, city_id, lat, lon, available, price, price_excl,
    price_total, currency, cancellation, rooms_left}] (prices per room per night; None when not available)."""
    out = []
    for p in ((doc.get("data") or {}).get("citySearch") or {}).get("properties") or []:
        info = ((p.get("content") or {}).get("informationSummary") or {})
        g = info.get("geoInfo") or {}
        pr = p.get("pricing") or {}
        row = {"id": p.get("propertyId"), "name": info.get("displayName") or info.get("defaultName") or "",
               "city_id": ((info.get("address") or {}).get("city") or {}).get("id"),
               "lat": g.get("latitude"), "lon": g.get("longitude"), "available": bool(pr.get("isAvailable")),
               "price": None, "price_excl": None, "price_total": None, "currency": "", "cancellation": "", "rooms_left": None}
        offers = pr.get("offers") or []
        room = (((offers[0].get("roomOffers") or [{}])[0]).get("room") or {}) if offers else {}
        rp = (room.get("pricing") or [{}])[0] if room.get("pricing") else {}
        night = ((rp.get("price") or {}).get("perRoomPerNight") or {})
        if row["available"] and night:
            excl = night.get("exclusive") or {}
            base = ((excl.get("loyaltyOfferSummary") or {}).get("basePrice") or {})
            row.update(price_excl=_num(excl.get("display")), price_total=_num((night.get("inclusive") or {}).get("display")),
                       price=_num(base.get("allInclusive")) or _num((night.get("inclusive") or {}).get("display")),
                       currency=rp.get("currency") or "",
                       cancellation=(((room.get("payment") or {}).get("cancellation") or {}).get("cancellationType") or ""),
                       rooms_left=room.get("availableRooms"))
        out.append(row)
    return out


def city_search_body(template: dict, city_id: int, night: date, hotel_ids: list[int], currency: str, adults: int,
                     now: datetime | None = None) -> dict:
    """Agoda's own citySearch request (captured from its results page) for another city and night, with the given
    hotels pinned (extraHotelIds) so they are priced whatever their rank."""
    b = copy.deepcopy(template)
    v = b["variables"]
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    ci, co = night.isoformat(), (night + timedelta(days=1)).isoformat()
    csr = v["CitySearchRequest"]
    csr["cityId"] = city_id
    sc = csr["searchRequest"]["searchCriteria"]
    sc.update(checkInDate=f"{ci}T00:00:00.000Z", localCheckInDate=ci, los=1, adults=adults, rooms=1, bookingDate=stamp,
              currency=currency)
    csr["searchRequest"]["extraHotels"] = {"extraHotelIds": list(hotel_ids), "enableFiltersForExtraHotels": False}
    csr["searchRequest"]["page"] = {**csr["searchRequest"].get("page", {}), "pageSize": max(5, len(hotel_ids)),
                                    "pageNumber": 1, "pageToken": ""}
    csr["searchRequest"]["searchHistory"] = []
    ctx = v["ContentSummaryRequest"]["context"]
    ctx["searchCriteria"] = {"cityId": city_id}
    ctx["occupancy"] = {**ctx.get("occupancy", {}), "numberOfAdults": adults, "checkIn": f"{ci}T00:00:00.000Z"}
    pricing = v["PricingSummaryRequest"]["pricing"]
    pricing.update(bookingDate=stamp, checkIn=f"{ci}T00:00:00.000Z", checkout=f"{co}T00:00:00.000Z",
                   localCheckInDate=ci, localCheckoutDate=co, currency=currency, mseHotelIds=list(hotel_ids),
                   mseClicked="")
    pricing["occupancy"] = {**pricing.get("occupancy", {}), "adults": adults, "rooms": 1}
    return b


def similarity(a: str, b: str) -> float:
    from .locations import similarity as s
    return s(a, b)


# ---------- the browser ----------

class AgodaPage:
    """One browser page on agoda.com: the search box for ids, and the page's own citySearch call for prices."""
    def __init__(self, pw, cfg: dict, log=log):
        from .booking_session import launch_browser
        self.ac, self.log = cfg["agoda"], log
        self.br = launch_browser(pw, True)
        self.ctx = self.br.new_context(user_agent=self.ac["user_agent"], locale="en-GB",
                                       viewport={"width": 1366, "height": 900},
                                       extra_http_headers={"Accept-Language": self.ac["accept_language"]})
        self.page = self.ctx.new_page()
        self.seen: list = []
        self.page.on("response", lambda r: self.seen.append(r))
        self.template, self.endpoint, self.headers, self.failed_saved = None, "", {}, False

    def close(self):
        self.br.close()

    def search(self, query: str) -> tuple[str, list[dict]]:
        """Type the hotel into Agoda's search box; returns (results address, properties on the results page)."""
        from .seller_probe import accept_cookies, search_box
        self.seen.clear()
        self.page.goto(self.ac["home"], wait_until="domcontentloaded", timeout=60000)
        self.page.wait_for_timeout(self.ac["wait_ms"])
        accept_cookies(self.page, self.ac["cookie_buttons"])
        search_box(self.page, self.ac["search_box"], query, self.ac["wait_ms"])
        props = []
        for r in self.seen:
            try:
                if "/graphql/search" in r.url and "citySearch" in (r.request.post_data or ""):
                    if self.template is None:
                        self.template = json.loads(r.request.post_data)
                        self.endpoint = r.url
                        self.headers = {k: v for k, v in r.request.headers.items()
                                        if k.lower() not in ("content-length", "cookie", "host")}
                    props += properties(r.json())
            except Exception:  # noqa: BLE001
                continue
        return self.page.url, props

    def hotel_night(self, hotel_id, night: date, adults: int) -> list[dict]:
        """The hotel's price for one night the plain way: open its Agoda page for that night and read the city search
        the page itself makes (slower than repeating the request, but nothing is repeated)."""
        self.seen.clear()
        url = self.ac["hotel_url"].format(id=hotel_id, checkin=night.isoformat(),
                                          checkout=(night + timedelta(days=1)).isoformat(), adults=adults)
        self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        props = []
        for _ in range(int(self.ac["page_wait_s"] * 2)):
            self.page.wait_for_timeout(500)
            for r in list(self.seen):
                try:
                    if "/graphql/search" in r.url and "citySearch" in (r.request.post_data or ""):
                        props = properties(r.json())
                        if self.template is None:
                            self.template = json.loads(r.request.post_data)
                            self.endpoint = r.url
                            self.headers = {k: v for k, v in r.request.headers.items()
                                            if k.lower() not in ("content-length", "cookie", "host")}
                except Exception:  # noqa: BLE001
                    continue
            if any(str(x["id"]) == str(hotel_id) for x in props):
                return props
        if not props:
            self._sample("no_city_search")
            raise RuntimeError(f"no city search on {self.page.url[:120]}")
        return props

    def _sample(self, what: str) -> None:
        """Keep what the page showed (text and picture) the first two times something went wrong (run 45333656075:
        Agoda's pages stopped making their city search for a whole pass)."""
        self.samples = getattr(self, "samples", 0) + 1
        if self.samples > 2:
            return
        try:
            out = Path(self.ac.get("debug_dir", "out"))
            out.mkdir(parents=True, exist_ok=True)
            (out / f"agoda_{what}_{self.samples}.txt").write_text(f"#URL {self.page.url}\n" + self.page.inner_text("body")[:20000],
                                                                  encoding="utf-8")
            self.page.screenshot(path=str(out / f"agoda_{what}_{self.samples}.png"))
        except Exception:  # noqa: BLE001
            pass

    def open_hotel(self, hotel_id, night: date, adults: int) -> bool:
        """Open Agoda's page for one hotel and night (it redirects to a search with the hotel pinned, whose citySearch
        request is the one to repeat). True when that request was seen."""
        self.seen.clear()
        url = self.ac["hotel_url"].format(id=hotel_id, checkin=night.isoformat(),
                                          checkout=(night + timedelta(days=1)).isoformat(), adults=adults)
        self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        self.page.wait_for_timeout(self.ac["wait_ms"] + 2000)
        for r in self.seen:
            try:
                if "/graphql/search" in r.url and "citySearch" in (r.request.post_data or ""):
                    self.template = json.loads(r.request.post_data)
                    self.endpoint = r.url
                    self.headers = {k: v for k, v in r.request.headers.items()
                                    if k.lower() not in ("content-length", "cookie", "host")}
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def suggest(self, query: str) -> list[dict]:
        """Agoda's own search-box suggestions for the text (what the box asks while you type)."""
        url = self.ac["suggest_url"].format(q=quote(query))
        status, text = self.page.evaluate("""async (url) => {
            const r = await fetch(url, {credentials: 'include'}); return [r.status, await r.text()]; }""", url)
        if status != 200:
            raise RuntimeError(f"suggest HTTP {status}")
        return suggestions(json.loads(text))

    def city_prices(self, city_id: int, night: date, hotel_ids: list[int], adults: int) -> list[dict]:
        if self.template is None:
            raise RuntimeError("no citySearch request seen yet")
        body = city_search_body(self.template, city_id, night, hotel_ids, self.ac["currency"], adults)
        status, text = self.page.evaluate("""async ([url, body, headers]) => {
            const r = await fetch(url, {method: 'POST', body, headers, credentials: 'include'});
            return [r.status, await r.text()]; }""", [self.endpoint, json.dumps(body), self.headers])
        if status != 200:
            if not self.failed_saved:          # keep one failing request and answer to see what Agoda refused
                out = Path(self.ac.get("debug_dir", "out"))
                out.mkdir(parents=True, exist_ok=True)
                (out / "agoda_failed_request.json").write_text(json.dumps({"status": status, "answer": text[:5000],
                                                                          "body": body}, indent=1), encoding="utf-8")
                self.failed_saved = True
            raise RuntimeError(f"citySearch HTTP {status}: {text[:600]}")
        return properties(json.loads(text))


# ---------- ids ----------

def match(w: dict, url: str, props: list[dict], ac: dict) -> dict | None:
    """The hotel Agoda pinned for our search (selectedproperty in the address), if its name and place agree."""
    m = re.search(r"selectedproperty=(\d+)", url)
    if not m:
        return None
    p = next((x for x in props if str(x["id"]) == m.group(1)), None)
    return judge(w, p, ac) if p else None


def suggestions(doc: dict) -> list[dict]:
    """Hotels in Agoda's search-box suggestions (GetUnifiedSuggestResult): [{id, name, city_id, where}]."""
    out, seen = [], set()
    for v in doc.get("ViewModelList") or []:
        dn = v.get("DisplayNames") or {}
        is_hotel = v.get("PageTypeId") == 103 or dn.get("CategoryName") == "Property" or v.get("IsHotel")
        if not is_hotel or not v.get("ObjectId") or not v.get("CityId") or v["ObjectId"] in seen:
            continue
        seen.add(v["ObjectId"])
        out.append({"id": v["ObjectId"], "name": dn.get("Name") or v.get("Name") or "", "city_id": v["CityId"],
                    "where": dn.get("GeoHierarchyName") or v.get("CityName") or ""})
    return out


def judge(w: dict, p: dict, ac: dict) -> dict | None:
    """Agoda's hotel p is our watched hotel w when its name and place agree (settings: max_km, near_km...)."""
    if not p or p.get("lat") is None:
        return None
    km = geo.haversine_km(float(w["lat"]), float(w["lon"]), float(p["lat"]), float(p["lon"]))
    sim = similarity(w["name"], p["name"])
    ok = (km <= ac["max_km"] and sim >= ac["min_similarity"] or km <= ac["near_km"] and sim >= ac["min_similarity_near"]
          or km <= ac["same_place_km"] and contains(w["name"], p["name"]))
    return {**p, "km": round(km, 2), "similarity": sim, "ok": ok}


def contains(a: str, b: str) -> bool:
    """One name inside the other ("Hotel Gesser" / "Hotel Gesser Sillian Hochpustertal"), not a generic stub."""
    from .discover_osm import _norm_name
    na, nb = _norm_name(a), _norm_name(b)
    short, long_ = sorted((na, nb), key=len)
    return len(short) >= 4 and short in long_   # "andy" in "andy4sternesuperior" (Hotel Andy, run 45260559478)


def warm_up_template(page, mapped: list[dict], today: date, adults: int, ac: dict, by_pid: dict, log=log,
                     skip: int = 0) -> bool:
    """Get Agoda's own citySearch request: from a mapped hotel's page (tries a few), else the search box."""
    for r in mapped[skip: skip + 3]:
        try:
            if page.open_hotel(r["seller_id"], today + timedelta(days=ac["verify_offset_days"]), adults):
                return True
        except Exception as e:  # noqa: BLE001
            log(f"hotel page {r['seller_id']}: {str(e)[:100]}")
    w = next((by_pid[r["pid"]] for r in mapped if r["pid"] in by_pid), None)
    page.search(f"{w['name']} {w.get('city', '')}".strip() if w else ac["warm_up_query"])
    return page.template is not None


def town_matches(ours: str, theirs: str) -> bool:
    from .discover_osm import _norm_name
    a, b = _norm_name(ours), _norm_name(theirs)
    return bool(a and b) and (a in b or b in a)


def find_id(page, w: dict, ac: dict, today: date, adults: int, stats: dict, log=log) -> dict | None:
    """Agoda's id for a watched hotel: its search-box suggestions (name and town, then the name alone), each candidate
    checked by name and place through a city search with it pinned; the search box itself until the page has the
    city-search request to repeat. Returns the best judged candidate (ok or not) or None."""
    best = None
    for tpl in ac["queries"]:
        q = tpl.format(name=w["name"], city=w.get("city", "")).strip()
        try:
            if page.template is None:           # the first search also gives the request to repeat
                url, props = page.search(q)
                m = match(w, url, props, ac)
                if m and m["ok"]:
                    return m
                best = best or m
                continue
            for c in page.suggest(q)[: ac["suggestions_checked"]]:
                try:
                    props = page.city_prices(int(c["city_id"]), today + timedelta(days=ac["verify_offset_days"]),
                                             [int(c["id"])], adults)
                except RuntimeError:       # Agoda refused the check (HTTP 502 now and then): name and town instead
                    stats["errors"] += 1
                    if similarity(w["name"], c["name"]) >= ac["min_similarity"] and town_matches(w.get("city", ""), c["where"]):
                        return {**c, "lat": "", "lon": "", "km": "", "similarity": similarity(w["name"], c["name"]), "ok": True}
                    continue
                m = judge(w, next((x for x in props if str(x["id"]) == str(c["id"])), None), ac)
                if m and m["ok"]:
                    return m
                if m and (best is None or m["km"] < best["km"]):
                    best = m
        except Exception as e:  # noqa: BLE001
            stats["errors"] += 1
            log(f"{q[:40]}: {str(e)[:100]}")
    return best


def ids_due(watch: list[dict], known: dict, today: date, ac: dict) -> list[dict]:
    out = []
    for w in watch:
        k = known.get(w["pid"])
        if not k:
            out.append((0, w))
        elif k["status"] != "ok" and (today - date.fromisoformat(k["checked_at"][:10])).days >= ac["retry_days"]:
            out.append((1, w))
    return [w for _, w in sorted(out, key=lambda x: x[0])]


# ---------- nights ----------

def nights_for(city_key: str, state: dict, today: date, horizon: int, per_pass: int, stride: int,
               min_lead: int = 0) -> list[date]:
    """The next nights for a city: a walk through the horizon with a fixed stride (so a pass covers far-apart nights
    and every night comes round), continuing where the last pass stopped; never closer than min_lead days."""
    start = int(state.get(city_key, 0))
    out = []
    span = max(1, horizon - min_lead)
    for i in range(per_pass):
        k = min_lead + (start + i * stride) % span
        out.append(today + timedelta(days=k))
    state[city_key] = (start + 1) % span
    return sorted(set(out))


def merge_prices(old: list[dict], new: list[dict], today: date) -> list[dict]:
    """Latest row per (hotel, night); nights already past dropped."""
    keep = {(r["pid"], r["night"]): r for r in old if r["night"] >= today.isoformat()}
    for r in new:
        keep[(r["pid"], r["night"])] = r
    return sorted(keep.values(), key=lambda r: (r["pid"], r["night"]))


# ---------- run ----------

def shard_of(pid: str, shards: int) -> int:
    import zlib
    return zlib.crc32(pid.encode()) % max(1, shards)


def merge_ids(paths: list[str], out: str) -> int:
    """Combine the id files of parallel id jobs: per (seller, hotel), the newest check wins, a found id over a miss."""
    best: dict[tuple, dict] = {}
    for path in paths:
        for r in read_csv(path):
            k = (r["seller"], r["pid"])
            old = best.get(k)
            if old is None or (r["status"] == "ok", r["checked_at"]) > (old["status"] == "ok", old["checked_at"]):
                best[k] = r
    write_csv(sorted(best.values(), key=lambda r: (r["seller"], r["pid"])), out, ID_FIELDS)
    return len(best)


def run(cfg: dict, part: str, max_minutes: float, page=None, today: date | None = None, log=log,
        clock=time.monotonic, sleep=time.sleep, shard: int = 0, shards: int = 1) -> dict:
    ac, p = cfg["agoda"], cfg["paths"]
    today = today or date.today()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    watch = [w for w in read_csv(p["watchlist"]) if not w.get("gone_since") and w.get("lat")]
    from .locations import positions
    pos = positions(cfg) if p.get("locations") else {}
    watch = [{**w, **dict(zip(("lat", "lon"), pos.get(w["pid"], (w["lat"], w["lon"]))))} for w in watch]
    all_ids = read_csv(p["seller_ids"])
    known = {r["pid"]: r for r in all_ids if r["seller"] == SELLER}
    started = clock()
    stats = {"ids_due": 0, "ids_found": 0, "ids_missed": 0, "calls": 0, "priced": 0, "available": 0, "errors": 0}

    def left():
        return max_minutes * 60 - (clock() - started)

    if part in ("ids", "all"):
        todo = [w for w in ids_due(watch, known, today, ac) if shard_of(w["pid"], shards) == shard][: ac["ids_per_run"]]
        stats["ids_due"] = len(todo)
        budget = max_minutes * 60 * (ac["ids_share"] if part == "all" else 1)
        for w in todo:
            if clock() - started > budget:
                break
            m = find_id(page, w, ac, today, cfg["scan"]["adults"], stats, log)
            if m and m["ok"]:
                stats["ids_found"] += 1
                known[w["pid"]] = {"pid": w["pid"], "seller": SELLER, "seller_id": m["id"], "city_id": m["city_id"],
                                   "seller_name": m["name"], "lat": m["lat"], "lon": m["lon"], "km": m["km"],
                                   "similarity": m["similarity"], "status": "ok", "checked_at": now}
            else:
                stats["ids_missed"] += 1
                known[w["pid"]] = {"pid": w["pid"], "seller": SELLER, "seller_id": m["id"] if m else "",
                                   "city_id": m["city_id"] if m else "", "seller_name": m["name"] if m else "",
                                   "lat": m["lat"] if m else "", "lon": m["lon"] if m else "", "km": m["km"] if m else "",
                                   "similarity": m["similarity"] if m else "", "status": "not found", "checked_at": now}
            log(f"id {w['name'][:35]}: {known[w['pid']]['status']} {known[w['pid']]['seller_id']} "
                f"{known[w['pid']]['seller_name'][:30]} {known[w['pid']]['km']} km")
            sleep(ac["pause_seconds"])
        others = [r for r in all_ids if r["seller"] != SELLER]
        write_csv(sorted(others + list(known.values()), key=lambda r: (r["seller"], r["pid"])), p["seller_ids"], ID_FIELDS)

    if part in ("prices", "all"):
        by_pid = {w["pid"]: w for w in watch}
        streak, refreshes, refused, backoffs, mode = 0, 0, 0, 0, ac.get("price_mode", "replay")
        if page.template is None and mode == "replay":   # the page's own citySearch request, from a hotel's own page
            # (a city search's request answered 502 when repeated with a hotel pinned, run 45253285417; the search box
            # sometimes made no request at all, run 45260559478)
            warm_up_template(page, [r for r in known.values() if r["status"] == "ok" and r["pid"] in by_pid],
                             today, cfg["scan"]["adults"], ac, by_pid, log)
        state_path = Path(p["agoda_state"])
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        cities: dict[str, list[dict]] = {}
        watched = {w["pid"] for w in watch}
        for r in known.values():
            if r["status"] == "ok" and r["pid"] in watched and r["city_id"]:
                cities.setdefault(str(r["city_id"]), []).append(r)
        order = sorted(cities, key=lambda c: state.get(f"last:{c}", ""))   # least recently priced first
        new = []
        horizon = ac.get("horizon_days") or cfg["scan"]["horizon_days"]
        for city in order:
            if left() <= 0:
                break
            rows = cities[city]
            for night in nights_for(city, state, today, horizon, ac["nights_per_city"], ac["night_stride"],
                                    ac.get("min_lead_days", 0)):
                for chunk in range(0, len(rows), ac["hotels_per_call"]):
                    part_rows = rows[chunk: chunk + ac["hotels_per_call"]]
                    try:
                        got = None
                        if mode == "replay":
                            try:
                                got = page.city_prices(int(city), night, [int(r["seller_id"]) for r in part_rows],
                                                       cfg["scan"]["adults"])
                            except Exception as e:  # noqa: BLE001
                                refused += 1
                                if refused < ac["replay_errors_before_pages"]:
                                    raise
                                mode = "pages"   # Agoda keeps refusing repeated requests: open the pages instead
                                log(f"switching to opening each hotel's page ({str(e)[:80]})")
                        if got is None:
                            got = page.hotel_night(int(part_rows[0]["seller_id"]), night, cfg["scan"]["adults"])
                        stats["calls"] += 1
                        streak = 0
                    except Exception as e:  # noqa: BLE001
                        stats["errors"] += 1
                        streak += 1
                        log(f"city {city} {night}: {str(e)[:200]}")
                        if mode == "replay" and streak >= 2 and refreshes < ac["template_refreshes"]:
                            # Agoda's page sometimes makes a variant of its request that can't be repeated (HTTP 400/502,
                            # runs 45253285417, 45258908611): take a fresh one from another hotel's search
                            refreshes += 1
                            page.template = None
                            warm_up_template(page, part_rows + [r for r in known.values() if r["status"] == "ok"],
                                             today, cfg["scan"]["adults"], ac, by_pid, log, skip=refreshes)
                            log(f"took a fresh request from Agoda's page ({refreshes})")
                        if streak and streak % ac["backoff_after"] == 0 and backoffs < ac["max_backoffs"]:
                            # Agoda refusing every search for a while (run 45370159785: "There was a problem
                            # completing your search", HTTP 502): wait, then start again with a fresh request
                            backoffs += 1
                            log(f"Agoda refusing searches: waiting {ac['backoff_s']} s ({backoffs})")
                            sleep(ac["backoff_s"])
                            mode, refused, page.template = "replay", 0, None
                            warm_up_template(page, part_rows + [r for r in known.values() if r["status"] == "ok"],
                                             today, cfg["scan"]["adults"], ac, by_pid, log, skip=refreshes + backoffs)
                        if stats["errors"] >= ac["stop_after_errors"]:
                            break
                        continue
                    by_id = {str(x["id"]): x for x in got}
                    for r in part_rows:
                        x = by_id.get(str(r["seller_id"]))
                        if not x:
                            continue
                        stats["priced"] += 1
                        stats["available"] += 1 if x["available"] else 0
                        lead = next(f"{lo}-{hi} days" for lo, hi in LEADS if (night - today).days <= hi)
                        got_lead = stats.setdefault("available_by_lead", {}).setdefault(lead, [0, 0])
                        got_lead[0] += 1 if x["available"] else 0
                        got_lead[1] += 1
                        new.append({"pid": r["pid"], "seller": SELLER, "seller_id": r["seller_id"], "night": night.isoformat(),
                                    "price": x["price"] if x["price"] is not None else "",
                                    "price_excl": x["price_excl"] if x["price_excl"] is not None else "",
                                    "price_total": x["price_total"] if x["price_total"] is not None else "",
                                    "currency": x["currency"] or ac["currency"], "available": "yes" if x["available"] else "no",
                                    "cancellation": x["cancellation"], "rooms_left": x["rooms_left"] if x["rooms_left"] is not None else "",
                                    "url": ac["hotel_url"].format(id=r["seller_id"], checkin=night.isoformat(),
                                                                   checkout=(night + timedelta(days=1)).isoformat(),
                                                                   adults=cfg["scan"]["adults"]),
                                    "seen_at": now})
                    sleep(ac["pause_seconds"])
                if stats["errors"] >= ac["stop_after_errors"]:
                    break
            state[f"last:{city}"] = now
            if stats["errors"] >= ac["stop_after_errors"]:
                log("too many errors in a row: stopping this pass")
                break
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, indent=0, sort_keys=True))
        old = [r for r in read_csv(p["seller_prices"]) if r.get("seller") == SELLER]
        others = [r for r in read_csv(p["seller_prices"]) if r.get("seller") != SELLER]
        merged = merge_prices(old, new, today)
        write_csv(sorted(others + merged, key=lambda r: (r["seller"], r["pid"], r["night"])), p["seller_prices"], PRICE_FIELDS)
        stats["cities"] = f"{len(order)} with mapped hotels"
        stats["stored_nights"] = len(merged)
    stats["ids_coverage"] = f"{sum(1 for w in watch if known.get(w['pid'], {}).get('status') == 'ok')}/{len(watch)}"
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(config.DEFAULT_PATH))
    ap.add_argument("--part", choices=["ids", "prices", "all"], default="all")
    ap.add_argument("--max-minutes", type=float)
    ap.add_argument("--shard", type=int, default=0, help="ids: this job's share of the hotels (0..shards-1)")
    ap.add_argument("--shards", type=int, default=1)
    ap.add_argument("--ids-out", help="ids: write to this file instead of paths.seller_ids (parallel jobs)")
    ap.add_argument("--merge-ids", nargs="+", help="combine id files into paths.seller_ids and stop")
    args = ap.parse_args(argv)
    cfg = config.load(args.config)
    if args.merge_ids:
        log(f"seller ids: {merge_ids(args.merge_ids + [cfg['paths']['seller_ids']], cfg['paths']['seller_ids'])} rows")
        return 0
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        page = AgodaPage(pw, cfg)
        try:
            if args.ids_out:          # read the shared file, write this job's own
                src = cfg["paths"]["seller_ids"]
                cfg["paths"]["seller_ids"] = args.ids_out
                if not Path(args.ids_out).exists() and Path(src).exists():
                    Path(args.ids_out).write_bytes(Path(src).read_bytes())
            stats = run(cfg, args.part, args.max_minutes or cfg["agoda"]["max_minutes"], page,
                        shard=args.shard, shards=args.shards)
        finally:
            page.close()
    log(f"agoda: {stats}")
    out = Path(cfg["paths"]["out_dir"])
    out.mkdir(parents=True, exist_ok=True)
    name = f"agoda_summary{'_' + args.part + str(args.shard) if args.shards > 1 else ''}.md"
    (out / name).write_text(f"## Agoda ({args.part}{f', share {args.shard}' if args.shards > 1 else ''})\n\n"
                            + "\n".join(f"- {k}: {v}" for k, v in stats.items()) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
