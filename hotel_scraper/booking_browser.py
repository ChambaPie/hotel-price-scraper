"""Booking.com through a real browser (headless Chromium via Playwright), which runs the JavaScript bot challenge
that blocks plain requests. Probe only: a few pages, slowly. Booking.com's terms don't allow automated access;
this stays personal, low-volume, and is meant for verifying candidate deals, not bulk collection.

    pip install -r requirements-browser.txt && python -m playwright install chromium
    python -m hotel_scraper.booking_browser [--label github] [--page it/grand-duchi-d-aosta ...]

Per page: did the challenge pass, the room table (room, price, board words per row), and the availability calendar
(nightly prices for the next ~2 months) fetched from inside the page. Writes <out_dir>/booking/: report.md,
results.json, <page>.html (truncated), <page>.png.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

import yaml

from . import config
from .spike import booking_page_markers, calendar_payload, summarize_calendar

ROWS_JS = """() => { let room = ''; const out = [];
  for (const tr of document.querySelectorAll('tr[data-block-id]')) {
    const name = tr.querySelector('.hprt-roomtype-icon-link, .hprt-roomtype-link');
    if (name) room = name.innerText.trim();
    const cond = tr.querySelector('.hprt-table-cell-conditions');
    if (!cond) continue;
    const price = tr.querySelector('.bui-price-display__value, .prco-valign-middle-helper');
    out.push({block: tr.getAttribute('data-block-id'), room: room.slice(0, 80),
              price: price ? price.innerText.trim() : '', conditions: cond.innerText.replace(/\\s+/g, ' ').trim().slice(0, 300)});
  }
  return out.slice(0, 40); }"""

CAL_JS = """async ({payload, token}) => {
  const r = await fetch('/dml/graphql?lang=en-gb', {method: 'POST', credentials: 'include',
    headers: {'content-type': 'application/json', 'x-booking-csrf-token': token}, body: JSON.stringify(payload)});
  return {status: r.status, text: (await r.text()).slice(0, 200000)};
}"""

BOARD = {"breakfast_included": r"breakfast included|breakfast is included|includes breakfast",
         "half_board": r"half board|breakfast (?:&|and) dinner included",
         "full_board": r"full board", "all_inclusive": r"all[- ]inclusive"}
PRICE = re.compile(r"(€|US\$|£|CHF|\$)\s?([\d.,]+)")


def board_of(text: str) -> list[str]:
    low = text.lower()
    return [k for k, pat in BOARD.items() if re.search(pat, low)]


def _amount(s: str) -> float | None:
    s = s.strip().rstrip(".,")
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", s):
        return float(s.replace(",", ""))
    if re.fullmatch(r"\d{1,3}(\.\d{3})+(,\d+)?", s):
        return float(s.replace(".", "").replace(",", "."))
    if re.fullmatch(r"\d+([.,]\d{1,2})?", s):
        return float(s.replace(",", "."))
    return None


def price_of(text: str) -> float | None:
    """Lowest amount in a price cell (cells can show the original and the discounted price)."""
    vals = [v for v in (_amount(m[1]) for m in PRICE.findall(text)) if v is not None]
    return min(vals) if vals else None


def currency_of(text: str) -> str:
    m = PRICE.search(text or "")
    return {"€": "EUR", "US$": "USD", "$": "USD", "£": "GBP", "CHF": "CHF"}.get(m.group(1), "") if m else ""


def cancellation_of(text: str) -> str:
    low = text.lower()
    if "free cancellation" in low:
        return "free cancellation"
    if "non-refundable" in low or "non refundable" in low:
        return "non-refundable"
    return "partially refundable" if "partially refundable" in low else ""


OTHER_HOTELS = re.compile(r"/hotel/([a-z]{2})/([a-z0-9-]+)\.(?:[a-z-]+\.)?html")


def _text(fragment: str) -> str:
    import html as h
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", fragment, flags=re.S)
    return re.sub(r"\s+", " ", h.unescape(re.sub(r"<[^>]+>", " ", t))).strip()


def offers_from_html(html: str) -> list[dict]:
    """Same result as ROWS_JS, from saved HTML (no browser): used for regression tests on real pages and as a fallback."""
    out, room = [], ""
    for row in re.findall(r'<tr[^>]*data-block-id="[^"]+"[^>]*>.*?</tr>', html, re.S):
        name = re.search(r'class="[^"]*hprt-roomtype-(?:icon-)?link[^"]*"[^>]*>(.*?)</', row, re.S)
        if name:
            room = _text(name.group(1))
        cond = re.search(r'class="[^"]*hprt-table-cell-conditions[^"]*"[^>]*>(.*?)</td>', row, re.S)
        if not cond:
            continue
        price = re.search(r'class="[^"]*(?:bui-price-display__value|prco-valign-middle-helper)[^"]*"[^>]*>(.*?)</', row, re.S)
        c, p = _text(cond.group(1)), _text(price.group(1)) if price else ""
        out.append({"room": room[:80], "price": price_of(p), "currency": currency_of(p), "board": board_of(c),
                    "cancellation": cancellation_of(c)})
    return out


def probe_page(page, base: str, country: str, pagename: str, checkin: date, adults: int, rooms: int, currency: str,
               wait_s: int, out: Path, other_calendars: int = 0) -> dict:
    params = urlencode({"checkin": checkin.isoformat(), "checkout": (checkin + timedelta(days=1)).isoformat(),
                        "group_adults": adults, "no_rooms": rooms, "group_children": 0,
                        "selected_currency": currency, "changed_currency": 1, "lang": "en-gb"})
    url = f"{base}/hotel/{country}/{pagename}.html?{params}"
    t0 = time.monotonic()
    first = page.goto(url, wait_until="domcontentloaded", timeout=60000)
    first_status = first.status if first else 0
    html, passed = "", False
    while time.monotonic() - t0 < wait_s:  # the challenge script solves itself, then reloads the real page
        page.wait_for_timeout(2000)
        try:
            html = page.content()
        except Exception:  # noqa: BLE001  (page navigating)
            continue
        if "b_csrf_token" in html:
            passed = True
            break
    res = {"page": f"{country}/{pagename}", "first_status": first_status, "challenge_passed": passed,
           "seconds": round(time.monotonic() - t0, 1), "title": page.title() if passed else "",
           "cookies": sorted(c["name"] for c in page.context.cookies())[:30]}
    slug = f"{country}_{pagename}"
    (out / f"{slug}.html").write_text(html[:1_000_000], encoding="utf-8")
    page.screenshot(path=str(out / f"{slug}.png"))
    if not passed:
        return res
    res["markers"] = booking_page_markers(html)
    rows = page.evaluate(ROWS_JS)
    res["rooms"] = [{"block": r["block"], "room": r["room"], "price": price_of(r["price"]), "currency": currency_of(r["price"]),
                     "board": board_of(r["conditions"]), "cancellation": cancellation_of(r["conditions"]),
                     "conditions": r["conditions"][:160]} for r in rows]
    token = re.search(r"b_csrf_token:\s*'(.+?)'", html).group(1)
    res["_token"] = token
    m = res["markers"]

    def calendar(cc, name, start):
        t1 = time.monotonic()
        cal = page.evaluate(CAL_JS, {"payload": calendar_payload(cc, name, start, 61, adults, rooms), "token": token})
        try:
            out = {"status": cal["status"], **summarize_calendar(json.loads(cal["text"]))}
        except ValueError:
            out = {"status": cal["status"], "error": cal["text"][:200]}
        out["seconds"] = round(time.monotonic() - t1, 2)
        return out
    me = (m["hotel_country"] or country, m["hotel_name"] or pagename)
    res["calendar"] = calendar(*me, date.today())
    res["calendar_windows"] = [calendar(*me, date.today() + timedelta(days=61 * k)) for k in range(1, 6)]
    hrefs = page.evaluate("() => Array.from(document.querySelectorAll('a[href*=\"/hotel/\"]')).map(a => a.href)")
    found = [o for h in hrefs for o in OTHER_HOTELS.findall(h)] + OTHER_HOTELS.findall(page.content())
    others = list(dict.fromkeys(o for o in found if o[1] != me[1]))[:other_calendars]
    res["other_calendars"] = [{"page": f"{cc}/{nm}", **calendar(cc, nm, date.today())} for cc, nm in others]
    return res


CARDS_JS = """() => Array.from(document.querySelectorAll('[data-testid="property-card"]')).map(c => {
  const q = s => c.querySelector(s);
  const a = q('a[data-testid="title-link"]') || q('a[href*="/hotel/"]');
  const txt = s => (q(s) ? q(s).innerText.replace(/\\s+/g, ' ').trim() : '');
  const stars = q('[data-testid="rating-stars"], [data-testid="rating-squares"]');
  return {name: txt('[data-testid="title"]'), href: a ? a.href : '', price: txt('[data-testid="price-and-discounted-price"]'),
          badge: txt('[data-testid="property-card-deal"]'), score: txt('[data-testid="review-score"]'),
          units: txt('[data-testid="recommended-units"]'), stars: stars ? stars.children.length : 0,
          text: (c.innerText || '').replace(/\\s+/g, ' ').slice(0, 1500)};
})"""


def parse_card(c: dict) -> dict:
    """Search card -> deal facts: Booking.com's own badge ("Late Escape Deal", "Limited-time Deal", ...), its
    original vs current price (Booking.com's advertised discount), breakfast, score, stars."""
    text = c.get("text", "")
    m = re.search(r"Original price\s*([^.]*?\d[\d.,]*)\.\s*Current price\s*([^.]*?\d[\d.,]*)", text)
    orig = price_of(m.group(1)) if m else None
    cur = price_of(m.group(2)) if m else price_of(c.get("price", ""))
    sc = re.search(r"(\d+(?:\.\d)?)", c.get("score", ""))
    return {"badge": c.get("badge", ""), "price": cur, "original_price": orig,
            "booking_discount": round(1 - cur / orig, 3) if cur and orig and orig > cur else None,
            "currency": currency_of(c.get("price", "")), "score": float(sc.group(1)) if sc else None,
            "stars": c.get("stars") or None, "breakfast": "breakfast included" in (c.get("units", "") + " " + text).lower(),
            "members_only": "members-only" in text.lower(), "room": c.get("units", "")[:80]}


def cards_from_html(html: str) -> list[dict]:
    """Same fields as CARDS_JS from saved HTML (regression tests; no browser)."""
    out = []
    for c in re.split(r'(?=<div[^>]*data-testid="property-card")', html):
        if 'data-testid="property-card"' not in c[:300]:
            continue
        def tid(name, html_=c):
            mm = re.search(r'data-testid="' + name + r'"[^>]*>(.*?)</(?:div|span|h3|a)>', html_, re.S)
            return _text(mm.group(1)) if mm else ""
        href = re.search(r'href="([^"]*?/hotel/[^"]+)"', c)
        out.append({"name": tid("title"), "href": href.group(1) if href else "", "price": tid("price-and-discounted-price"),
                    "badge": tid("property-card-deal"), "score": tid("review-score"), "units": tid("recommended-units"),
                    "stars": 0, "text": _text(c)[:1500]})
    return out


def search_probe(page, base: str, query: str, checkin: date, adults: int, rooms: int, currency: str,
                 n_calendars: int, shots: Path, session_token: str = "") -> dict:
    """Destination search (one page of property cards) -> Booking.com ids, then each property's 61-night calendar
    from the same session: measures whether ids can be harvested and how many calendar calls pass."""
    params = urlencode({"ss": query, "checkin": checkin.isoformat(), "checkout": (checkin + timedelta(days=1)).isoformat(),
                        "group_adults": adults, "no_rooms": rooms, "group_children": 0, "selected_currency": currency})
    t0 = time.monotonic()
    page.goto(f"{base}/searchresults.en-gb.html?{params}", wait_until="domcontentloaded", timeout=60000)
    cards = []
    while time.monotonic() - t0 < 45 and not cards:
        page.wait_for_timeout(2000)
        try:
            cards = page.evaluate(CARDS_JS)
        except Exception:  # noqa: BLE001
            cards = []
    html = page.content()
    slug = "search_" + re.sub(r"[^A-Za-z0-9]+", "_", query)
    (shots / f"{slug}.html").write_text(html[:1_000_000], encoding="utf-8")
    page.screenshot(path=str(shots / f"{slug}.png"))
    m_tok = re.search(r"b_csrf_token:\s*'(.+?)'", html)
    token = m_tok.group(1) if m_tok else session_token  # the search page may not carry it; the session's works
    out = {"query": query, "token_source": "search page" if m_tok else ("hotel page" if token else "none"), "cards": len(cards), "seconds": round(time.monotonic() - t0, 1), "sample": cards[:5],
           "url": page.url[:200], "title": page.title()[:120], "html_bytes": len(html),
           "testids": sorted(set(re.findall(r'data-testid="([a-z-]+)"', html)))[:40]}
    ids = []
    for c in cards:
        m = OTHER_HOTELS.search(c["href"])
        if m:
            ids.append((m.group(1), m.group(2), c["name"]))
    out["ids"] = len(ids)
    if not token or not ids:
        return out
    calls, t1 = [], time.monotonic()
    for cc, nm, name in ids[:n_calendars]:
        cal = page.evaluate(CAL_JS, {"payload": calendar_payload(cc, nm, date.today(), 61, adults, rooms),
                                     "token": token})
        try:
            sm = summarize_calendar(json.loads(cal["text"]))
        except ValueError:
            sm = {"error": cal["text"][:120]}
        calls.append({"page": f"{cc}/{nm}", "name": name, "status": cal["status"], **{k: sm.get(k) for k in ("priced", "days", "error")}})
    out["calendar_calls"] = len(calls)
    out["calendar_ok"] = sum(1 for c in calls if c["status"] == 200 and not c.get("error"))
    out["calendar_seconds"] = round(time.monotonic() - t1, 1)
    out["calendar_failures"] = [c for c in calls if c["status"] != 200 or c.get("error")][:5]
    return out


def report_md(label: str, results: list[dict]) -> str:
    lines = [f"## Booking.com via headless browser: {label} ({date.today().isoformat()})", "",
             "| Page | First HTTP | Challenge passed | Seconds | Room rows | Rows with board info | Calendar |",
             "|---|---|---|---|---|---|---|"]
    def cal_line(c):
        return (f"HTTP {c.get('status')}, {c.get('priced', 0)}/{c.get('days', 0)} priced, {c.get('first')}..{c.get('last')}, "
                f"{c.get('seconds')} s" if "error" not in c else f"HTTP {c.get('status')}: {c['error'][:80]}")
    for r in [r for r in results if "page" in r]:
        rooms = r.get("rooms") or []
        cal = r.get("calendar") or {}
        cal_s = (f"HTTP {cal.get('status')}: {cal.get('priced', 0)} of {cal.get('days', 0)} days priced"
                 if cal and "error" not in cal else (cal.get("error", "-")[:80] if cal else "-"))
        lines.append(f"| {r['page']} | {r['first_status']} | {'yes' if r['challenge_passed'] else 'no'} | {r['seconds']} | "
                     f"{len(rooms)} | {sum(1 for x in rooms if x['board'])} | {cal_s} |")
    for r in [r for r in results if r.get("stay_offers") is not None]:
        lines += ["", f"### {r['page']}: offers (total for the stay)"]
        lines += [f"- {o['room']}: {o['currency']} {o['total']} {', '.join(o['board'])}" for o in r["stay_offers"]]
    for r in [r for r in results if "page" in r and r.get("stay_offers") is None]:
        lines += ["", f"### {r['page']}: offers"]
        for x in (r.get("rooms") or [])[:12]:
            lines.append(f"- {x['room']}: {x['currency']} {x['price']}, {', '.join(x['board']) or 'no board'}, "
                         f"{x['cancellation'] or '?'}")
        if r.get("calendar_windows"):
            lines += ["", "Calendar windows (61 nights each): " + "; ".join(cal_line(c) for c in r["calendar_windows"])]
        if r.get("other_calendars"):
            ok = sum(1 for c in r["other_calendars"] if c.get("status") == 200 and "error" not in c)
            lines += ["", f"Other hotels' calendars from the same session: {ok} of {len(r['other_calendars'])} OK"]
            lines += [f"- {c['page']}: {cal_line(c)}" for c in r["other_calendars"]]
    for r in results:
        if "query" in r:
            lines += ["", f"### Search \"{r['query']}\": {r['cards']} cards, {r.get('ids', 0)} Booking.com ids ({r['seconds']} s)",
                      f"Landed on {r.get('url', '')} — \"{r.get('title', '')}\", {r.get('html_bytes', 0)} bytes; "
                      f"test ids seen: {', '.join(r.get('testids', [])[:25])}"]
            if r.get("calendar_calls"):
                lines.append(f"Calendars from the same session ({r.get('token_source')} token): {r['calendar_ok']} of "
                             f"{r['calendar_calls']} OK in {r['calendar_seconds']} s")
                lines += [f"- failed: {c}" for c in r.get("calendar_failures", [])]
            lines += [f"- {c['name']}: {c['price']} ({c['href'][:80]})" for c in r.get("sample", [])]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(config.DEFAULT_PATH))
    ap.add_argument("--label", default="")
    ap.add_argument("--page", action="append", help="country/pagename, e.g. it/grand-duchi-d-aosta (repeatable)")
    ap.add_argument("--wait", type=int, default=45, help="seconds to wait for the challenge per page")
    ap.add_argument("--stay", action="append", help="also check offers for page:checkin:nights, e.g. at/haus-hepi:2027-01-14:4")
    ap.add_argument("--headed", action="store_true", help="show the browser window (local runs)")
    ap.add_argument("--other-calendars", type=int, default=10, help="also fetch calendars of N hotels linked from the page")
    ap.add_argument("--search", action="append", help="destination to search after the hotel pages, e.g. Trieste (repeatable)")
    ap.add_argument("--search-calendars", type=int, default=40, help="calendar calls per search, one per property found")
    args = ap.parse_args(argv)
    from playwright.sync_api import sync_playwright

    cfg = config.load(args.config)
    scfg = yaml.safe_load(Path(cfg["paths"]["spike"]).read_text(encoding="utf-8"))["booking"]
    s = cfg["scan"]
    pages = [tuple(p.split("/", 1)) for p in (args.page or [])] or [(p["country"], p["pagename"]) for p in scfg["pages"]]
    label = args.label or ("github-actions" if os.environ.get("GITHUB_ACTIONS") else "local")
    out = Path(cfg["paths"]["out_dir"]) / "booking"
    out.mkdir(parents=True, exist_ok=True)
    results = []
    with sync_playwright() as pw:
        from .booking_session import launch_browser
        browser = launch_browser(pw, not args.headed)
        ctx = browser.new_context(user_agent=scfg["user_agent"], locale="en-GB", viewport={"width": 1366, "height": 900},
                                  extra_http_headers={"Accept-Language": scfg["accept_language"]})
        page = ctx.new_page()
        for country, pagename in pages:
            try:
                results.append(probe_page(page, scfg["base_url"], country, pagename,
                                          date.today() + timedelta(days=scfg["checkin_offset_days"]),
                                          s["adults"], s["rooms"], s["currency"], args.wait, out, args.other_calendars))
            except Exception as e:  # noqa: BLE001
                results.append({"page": f"{country}/{pagename}", "first_status": 0, "challenge_passed": False,
                                "seconds": 0, "error": str(e)[:300]})
            page.wait_for_timeout(5000)
        for st in args.stay or []:
            pg, ci, nn = st.rsplit(":", 2)
            cc, slug = pg.split("/", 1)
            from .booking_session import BookingSession  # noqa: F401  (reuse the parsers via the open page)
            params = urlencode({"checkin": ci, "checkout": (date.fromisoformat(ci) + timedelta(days=int(nn))).isoformat(),
                                "group_adults": s["adults"], "no_rooms": s["rooms"], "group_children": 0, "lang": "en-gb"})
            page.goto(f"{scfg['base_url']}/hotel/{cc}/{slug}.html?{params}", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(8000)
            rows = page.evaluate(ROWS_JS)
            offers = [{"room": r["room"], "total": price_of(r["price"]), "currency": currency_of(r["price"]),
                       "board": board_of(r["conditions"])} for r in rows]
            results.append({"page": f"{pg} stay {ci} x{nn}", "first_status": 0, "challenge_passed": bool(rows), "seconds": 0,
                            "stay_offers": offers[:8]})
        session_token = next((r.get("_token") for r in results if r.get("_token")), "")
        for q in args.search or []:
            try:
                results.append(search_probe(page, scfg["base_url"], q, date.today() + timedelta(days=scfg["checkin_offset_days"]),
                                            s["adults"], s["rooms"], s["currency"], args.search_calendars, out, session_token))
            except Exception as e:  # noqa: BLE001
                results.append({"query": q, "cards": 0, "seconds": 0, "error": str(e)[:300]})
        browser.close()
    md = report_md(label, results)
    (out / "report.md").write_text(md, encoding="utf-8")
    for r in results:
        r.pop("_token", None)
    (out / "results.json").write_text(json.dumps({"label": label, "results": results}, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    print(md, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
