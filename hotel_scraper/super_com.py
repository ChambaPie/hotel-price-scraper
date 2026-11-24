"""Super.com: each watched hotel's own price where Super.com sells the room itself (settings: super_com). Owner,
2026-11-23: every vendor that sells the room itself.

Super.com's hotel pages show other sites' rates for some hotels (from Vio, "partner": "vio"; probe of 2026-11-24).
Only its own sales count here: rates where it is the merchant of record ("is_mor"), what its own map search shows.

    ids     its own map search around the hotel (one search lists the hotels of the whole area) for a night a while
            ahead; candidates are checked by name and place (same rules as Agoda); the area (Super.com's region) is kept
    prices  the same search per area and night: one search prices every mapped hotel of that area

The search is Super.com's own (snapcommerce tRPC: getUnauthMapView starts a job, checkJobStatus until it is finished,
getUnauthMapViewByJobId gives the hotels), called from inside its page in Google Chrome with a window (Cloudflare).
Prices per room per night: price = with taxes, price_excl = before taxes, price_total = with Super.com's surcharge.

    python -m hotel_scraper.super_com [--part ids|prices|all] [--max-minutes 25]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import config
from .agoda import ID_FIELDS, PRICE_FIELDS, ids_due, judge, merge_prices
from .engines import rotating_nights
from .scan import read_csv, write_csv
from .seller_merge import priced

SELLER = "super_com"


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%H:%M:%S} {msg}", flush=True)


def results(doc: dict) -> list[dict]:
    """Hotels of a map-search answer: [{id, name, lat, lon, slug, own, available, price, price_excl, price_total,
    currency}]. own: Super.com is the merchant of record (its own sale); a rate it does not sell itself is not a
    price here."""
    out = []
    for x in ((doc.get("result") or {}).get("data") or {}).get("query_results") or []:
        h, r = x.get("hotel") or {}, x.get("rate") or {}
        if not h.get("id"):
            continue
        own = bool(r.get("is_mor"))
        total, excl, extra = r.get("nightly_total_price"), r.get("nightly_price_before_tax"), r.get("surcharge") or 0
        ok = own and bool(total) and total > 0
        out.append({"id": str(h["id"]), "name": h.get("name") or "", "lat": h.get("latitude"), "lon": h.get("longitude"),
                    "slug": h.get("url_slug") or "", "own": own, "available": ok,
                    "price": round(total, 2) if ok else None, "price_excl": round(excl, 2) if ok and excl else None,
                    "price_total": round(total + extra, 2) if ok else None, "currency": r.get("currency") or ""})
    return out


def query(lat, lon, name: str, night: date, adults: int, sc: dict, props: str) -> dict:
    """The queryParams of Super.com's own map search (as its hotel page sends them, probe 2026-11-24)."""
    return {"checkin_at": night.isoformat(), "checkout_at": (night + timedelta(days=1)).isoformat(),
            "num_adults": adults, "children": "[]", "latitude": float(lat), "longitude": float(lon), "name": name,
            "currency": sc["currency"], "country_code": sc["country"], "locale": sc["locale"], "source": "web",
            "user_properties": props}


SEARCH_JS = """async ([base, qs, props, device, session, maxPolls, pollMs]) => {
  const call = async (proc, inp) => {
    const u = base + '/' + proc + '?input=' + encodeURIComponent(JSON.stringify(inp));
    const r = await fetch(u, {headers: {'accept': 'application/json', 'x-session-id': session}, credentials: 'include'});
    const t = await r.text();
    if (r.status !== 200) throw new Error(proc + ' HTTP ' + r.status + ': ' + t.slice(0, 150));
    return JSON.parse(t);
  };
  const one = async (qp) => {
    try {
      const job = (await call('recommender.getUnauthMapView', {queryParams: qp, deviceId: device})).result.data;
      let st = '';
      for (let i = 0; i < maxPolls; i++) {
        st = (await call('recommender.checkJobStatus',
                         {pathParams: {jobId: job.job_id}, userProperties: props, deviceId: device})).result.data.status;
        if (st !== 'started' && st !== 'pending' && st !== 'queued') break;
        await new Promise(r => setTimeout(r, pollMs));
      }
      if (st !== 'finished') return {error: 'search ' + st, job};
      const doc = await call('recommender.getUnauthMapViewByJobId',
                             {pathParams: {jobId: job.job_id}, userProperties: props, deviceId: device});
      const rows = ((doc.result || {}).data || {}).query_results || [];
      const slim = rows.map(x => ({hotel: {id: x.hotel.id, name: x.hotel.name, latitude: x.hotel.latitude,
                                           longitude: x.hotel.longitude, url_slug: x.hotel.url_slug}, rate: x.rate}));
      return {job, doc: {result: {data: {query_results: slim}}}};
    } catch (e) { return {error: String(e).slice(0, 200)}; }
  };
  return await Promise.all(qs.map(one));
}"""


class SuperPage:
    """Super.com's own map search, asked from inside its page (its cookies, its session)."""
    def __init__(self, pw, cfg: dict, log=log):
        self.sc, self.log = cfg["super_com"], log
        from .seller_probe import accept_cookies, open_browser
        self.br, ctx = open_browser(pw, None, self.sc["browser"], {"accept_language": self.sc["accept_language"],
                                                                   "user_agent": self.sc.get("user_agent", "")})
        self.page = ctx.new_page()
        self.session, self.device = str(uuid.uuid4()), str(uuid.uuid4())
        self.props = self.sc["user_properties"]
        seen: dict = {}

        def note(r):                     # the page's own session id and visitor properties, sent with ours too
            if "/snapcommerce/api/trpc/" in r.url:
                seen.setdefault("session", r.headers.get("x-session-id"))
                if "input=" in r.url:
                    from urllib.parse import unquote
                    try:
                        inp = json.loads(unquote(r.url.split("input=", 1)[1].split("&")[0]))
                        seen.setdefault("device", inp.get("deviceId"))
                        seen.setdefault("props", inp.get("userProperties"))
                    except Exception:  # noqa: BLE001
                        pass
        self.page.on("request", note)
        self.page.goto(self.sc["home"], wait_until="domcontentloaded", timeout=60000)
        self.page.wait_for_timeout(self.sc["warm_up_s"] * 1000)
        accept_cookies(self.page, cfg["seller_probe"]["cookie_buttons"])
        self.page.wait_for_timeout(2000)
        self.page.remove_listener("request", note)
        self.session = seen.get("session") or self.session
        self.device = seen.get("device") or self.device
        self.props = seen.get("props") or self.props
        self.kept = 0

    def search(self, wanted: list[tuple]) -> list[dict]:
        """[(lat, lon, place name, night, adults)] -> per search {hotels, region, error}, several searches at once."""
        qs = [query(lat, lon, name, night, adults, self.sc, self.props) for lat, lon, name, night, adults in wanted]
        got = self.page.evaluate(SEARCH_JS, [self.sc["trpc"], qs, self.props, self.device, self.session,
                                             self.sc["max_polls"], int(self.sc["poll_seconds"] * 1000)])
        out = []
        for g in got:
            if g.get("doc") and self.kept < 2 and self.sc.get("sample_dir"):     # real answers, to build tests from
                self.kept += 1
                d = Path(self.sc["sample_dir"])
                d.mkdir(parents=True, exist_ok=True)
                (d / f"super_sample_{self.kept}.json").write_text(json.dumps(g)[:3_000_000])
            out.append({"hotels": results(g["doc"]) if g.get("doc") else [],
                        "region": str(((g.get("job") or {}).get("region_id")) or ""), "error": g.get("error", "")})
        return out

    def close(self):
        self.br.close()


def best_match(w: dict, hotels: list[dict], sc: dict) -> dict | None:
    """The search's hotel that is our hotel (name and place), or the nearest candidate (not ok) to explain a miss."""
    best = None
    for h in hotels:
        m = judge(w, h, sc)
        if m and m["ok"]:
            return {**m, "slug": h["slug"]}
        if m and (best is None or m["km"] < best["km"]):
            best = {**m, "slug": h["slug"]}
    return best


def run(cfg: dict, part: str, max_minutes: float, page, today: date | None = None, log=log, clock=time.monotonic,
        now_dt: datetime | None = None) -> dict:
    sc, p = cfg["super_com"], cfg["paths"]
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
    k = sc["searches_at_once"]

    if part in ("ids", "all"):
        todo = ids_due(watch, known, today, sc)[: sc["ids_per_run"]]
        stats["ids_due"] = len(todo)
        budget = max_minutes * 60 * (sc["ids_share"] if part == "all" else 1)
        night = today + timedelta(days=sc["ids_night_days"])
        for i in range(0, len(todo), k):
            if clock() - started > budget or stats["errors"] >= sc["stop_after_errors"]:
                break
            batch = todo[i: i + k]
            try:
                answers = page.search([(w["lat"], w["lon"], w.get("city", ""), night, adults) for w in batch])
            except Exception as e:  # noqa: BLE001
                stats["errors"] += 1
                log(f"ids search: {str(e)[:150]}")
                continue
            stats["calls"] += len(batch)
            for w, a in zip(batch, answers):
                if a["error"]:
                    stats["errors"] += 1
                    log(f"id {w['name'][:35]}: {a['error'][:120]}")
                    continue
                b = best_match(w, a["hotels"], sc) or {}
                ok = bool(b.get("ok"))
                stats["ids_found" if ok else "ids_missed"] += 1
                known[w["pid"]] = {"pid": w["pid"], "seller": SELLER, "seller_id": b.get("id", "") if ok else "",
                                   "city_id": f"{a['region']}|{b.get('slug', '')}" if ok else a["region"],
                                   "seller_name": b.get("name", ""), "lat": b.get("lat", ""), "lon": b.get("lon", ""),
                                   "km": b.get("km", ""), "similarity": b.get("similarity", ""),
                                   "status": "ok" if ok else "not found", "checked_at": now}
                log(f"id {w['name'][:35]}: {known[w['pid']]['status']} {b.get('id', '')} {b.get('name', '')[:30]} "
                    f"{b.get('km', '')} km ({len(a['hotels'])} hotels in the area)")
        others = [r for r in all_ids if r["seller"] != SELLER]
        write_csv(sorted(others + list(known.values()), key=lambda r: (r["seller"], r["pid"])), p["seller_ids"],
                  ID_FIELDS)

    if part in ("prices", "all"):
        new = []
        mapped = [r for r in known.values() if r["status"] == "ok" and r["seller_id"]]
        areas: dict[str, list[dict]] = {}
        for r in mapped:
            areas.setdefault(r["city_id"].split("|")[0] or r["pid"], []).append(r)
        keys = sorted(areas)
        if keys:                                  # each pass starts further on, so every area comes round
            s = (int(now_dt.timestamp() // (4 * 3600)) * sc["areas_per_pass"]) % len(keys)
            keys = (keys[s:] + keys[:s])[: sc["areas_per_pass"]]
        start = today + timedelta(days=sc["min_lead_days"])
        days = sc["horizon_days"] - sc["min_lead_days"]
        tasks = [(a, n) for a in keys for n in rotating_nights(a, start, days, sc["nights_per_pass"], sc["night_stride"],
                                                                 now_dt)]
        for i in range(0, len(tasks), k):
            if clock() - started > max_minutes * 60 or stats["errors"] >= sc["stop_after_errors"]:
                break
            batch = tasks[i: i + k]
            wanted = []
            for area, night in batch:
                r0 = areas[area][0]
                wanted.append((r0["lat"], r0["lon"], "", night, adults))
            try:
                answers = page.search(wanted)
            except Exception as e:  # noqa: BLE001
                stats["errors"] += 1
                log(f"prices search: {str(e)[:150]}")
                continue
            for (area, night), a in zip(batch, answers):
                if a["error"]:
                    stats["errors"] += 1
                    log(f"area {area} {night}: {a['error'][:120]}")
                    continue
                stats["calls"] += 1
                by_id = {h["id"]: h for h in a["hotels"]}
                for r in areas[area]:
                    h = by_id.get(r["seller_id"])
                    if h is None or not h["own"] or h["currency"] != sc["currency"]:
                        continue                  # not listed that night, or not Super.com's own sale: says nothing
                    stats["priced"] += 1
                    stats["available"] += 1 if h["available"] else 0
                    slug = r["city_id"].split("|")[1] if "|" in r["city_id"] else h["slug"]
                    new.append({"pid": r["pid"], "seller": SELLER, "seller_id": r["seller_id"],
                                "night": night.isoformat(), "price": h["price"] or "", "price_excl": h["price_excl"] or "",
                                "price_total": h["price_total"] or "", "currency": h["currency"],
                                "available": "yes" if h["available"] else "no", "cancellation": "", "rooms_left": "",
                                "url": sc["hotel_url"].format(slug=slug, id=r["seller_id"], adults=adults,
                                                              checkin=night.isoformat(),
                                                              checkout=(night + timedelta(days=1)).isoformat()),
                                "seen_at": now})
            if stats["calls"] and stats["calls"] % 20 < k:
                log(f"{stats['calls']} searches, {stats['available']} hotel-nights priced")
        old_all = read_csv(p["seller_prices"])
        ok = {r["pid"] for r in known.values() if r["status"] == "ok"}     # a hotel no longer matched: its prices go
        merged = merge_prices([x for x in old_all if x.get("seller") == SELLER and x["pid"] in ok and priced(x)],
                              new, today)
        write_csv(sorted([x for x in old_all if x.get("seller") != SELLER] + merged,
                         key=lambda x: (x["seller"], x["pid"], x["night"])), p["seller_prices"], PRICE_FIELDS)
        stats["stored_nights"] = len(merged)
    stats["ids_coverage"] = f"{sum(1 for w in watch if known.get(w['pid'], {}).get('status') == 'ok')}/{len(watch)}"
    return stats


def summary_md(s: dict) -> str:
    return ("## Super.com\n\n" + f"Hotels found on Super.com: {s['ids_coverage']} (this pass: {s['ids_found']} found, "
            f"{s['ids_missed']} not found of {s['ids_due']} looked up). Prices: {s['calls']} area searches, "
            f"{s['available']} hotel-nights sold by Super.com itself; {s.get('stored_nights', 0)} nights stored; "
            f"{s['errors']} errors.\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(config.DEFAULT_PATH))
    ap.add_argument("--part", choices=["ids", "prices", "all"], default="all")
    ap.add_argument("--max-minutes", type=float)
    args = ap.parse_args(argv)
    cfg = config.load(args.config)
    cfg["super_com"]["sample_dir"] = str(Path(cfg["paths"]["out_dir"]) / "super_samples")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        page = SuperPage(pw, cfg)
        try:
            stats = run(cfg, args.part, args.max_minutes or cfg["super_com"]["max_minutes"], page)
        finally:
            page.close()
    log(f"super.com: {stats}")
    out = Path(cfg["paths"]["out_dir"])
    out.mkdir(parents=True, exist_ok=True)
    (out / "super_com_summary.md").write_text(summary_md(stats), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
