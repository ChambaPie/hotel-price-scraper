"""One headless-Chromium session on Booking.com: pass the JavaScript challenge once, then search destinations,
read price calendars (61 nights per call) and hotel offers (board, cancellation) from the same session.
Personal, low-volume use; Booking.com's terms don't allow automated access.
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, timedelta
from urllib.parse import quote, urlencode

from .booking_browser import (CAL_JS, CARDS_JS, OTHER_HOTELS, ROWS_JS, board_of, cancellation_of, currency_of,
                              price_of)
from .spike import calendar_payload

TOKEN = re.compile(r"b_csrf_token:\s*'(.+?)'")


def parse_price(raw: str) -> float | None:
    """Calendar price strings: "364", "€ 1,234", "US$1.1K" (Booking.com abbreviates thousands as K, millions as M)."""
    s = (raw or "").strip().upper().replace("\u00a0", " ")
    mult = 1000.0 if s.endswith("K") else 1_000_000.0 if s.endswith("M") else 1.0
    num = re.sub(r"[^\d.,]", "", s)
    if not num:
        return None
    if mult > 1:  # "1.1K" / "1,1K": the separator is a decimal point
        num = num.replace(",", ".")
    elif re.fullmatch(r"\d{1,3}([.,]\d{3})+", num):  # thousands separators
        num = re.sub(r"[.,]", "", num)
    else:
        num = num.replace(",", ".")
    try:
        return round(float(num) * mult, 2)
    except ValueError:
        return None


def parse_calendar(text: str) -> dict[str, dict]:
    """GraphQL calendar answer -> {night: {"price": float|None, "raw": str, "available": bool, "min_stay": int}}."""
    data = json.loads(text)
    cal = ((data or {}).get("data") or {}).get("availabilityCalendar") or {}
    out = {}
    for d in cal.get("days") or []:
        raw = d.get("avgPriceFormatted") or ""
        out[d["checkin"]] = {"price": parse_price(raw), "raw": raw, "available": bool(d.get("available")),
                             "min_stay": d.get("minLengthOfStay") or 1}
    return out


class BookingSession:
    def __init__(self, base_url: str, user_agent: str, accept_language: str, adults: int, rooms: int, currency: str,
                 headless: bool = True, log=print, cookies: list[dict] | None = None):
        self.base, self.ua, self.lang = base_url.rstrip("/"), user_agent, accept_language
        self.adults, self.rooms, self.currency, self.headless, self.log = adults, rooms, currency, headless, log
        self.token, self.currency_seen, self.calls = "", "", 0
        self.diag_dir, self.diag_left = None, 3   # save a few empty search pages for diagnosis
        self.cookies = cookies or []              # optional signed-in session (Playwright cookie dicts)

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=self.headless, args=["--disable-blink-features=AutomationControlled"])
        ctx = self.browser.new_context(user_agent=self.ua, locale="en-GB", viewport={"width": 1366, "height": 900},
                                       extra_http_headers={"Accept-Language": self.lang})
        if self.cookies:
            ctx.add_cookies(self.cookies)
        self.page = ctx.new_page()
        return self

    def __exit__(self, *exc):
        try:
            self.browser.close()
        finally:
            self._pw.stop()

    def _wait_for(self, js: str | None, needle: str, seconds: int):
        t0, html, got = time.monotonic(), "", None
        while time.monotonic() - t0 < seconds:
            self.page.wait_for_timeout(1500)
            try:
                html = self.page.content()
                got = self.page.evaluate(js) if js else None
            except Exception:  # noqa: BLE001  (page still navigating)
                continue
            if needle in html and (js is None or got):
                break
        m = TOKEN.search(html)
        if m:
            self.token = m.group(1)
        return html, got

    def warm_up(self, cc: str, slug: str, checkin: date) -> bool:
        """Open one known hotel page so the challenge is passed and the session token is set."""
        self.offers(cc, slug, checkin)
        return bool(self.token)

    def search(self, query: str, checkin: date, wait: int = 20, scrolls: int = 0, raw: bool = False) -> list[dict]:
        """Property cards for a destination or hotel name: [{name, cc, slug, price_text}].
        A hotel-name search can land straight on that hotel's page: then that hotel is returned as the only card."""
        params = urlencode({"ss": query, "checkin": checkin.isoformat(), "checkout": (checkin + timedelta(days=1)).isoformat(),
                            "group_adults": self.adults, "no_rooms": self.rooms, "group_children": 0,
                            "selected_currency": self.currency}, quote_via=quote)  # spaces as %20: Booking.com keeps '+' literally
        self.page.goto(f"{self.base}/searchresults.en-gb.html?{params}", wait_until="domcontentloaded", timeout=60000)
        html, cards = self._wait_for(CARDS_JS, "property-card", wait)
        for _ in range(scrolls if cards else 0):  # more cards load as the list scrolls
            self.page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            self.page.wait_for_timeout(2500)
            try:
                cards = self.page.evaluate(CARDS_JS) or cards
            except Exception:  # noqa: BLE001
                break
        landed = OTHER_HOTELS.search(self.page.url)
        if not cards and landed:
            title = (self.page.title() or "").split(",")[0].split("|")[0].strip()
            return [{"name": title, "cc": landed.group(1), "slug": landed.group(2), "price_text": ""}]
        if not cards and self.diag_dir is not None and self.diag_left > 0:
            self.diag_left -= 1
            tag = re.sub(r"[^A-Za-z0-9]+", "_", query)[:40]
            (self.diag_dir / f"search_empty_{tag}.html").write_text(html[:1_000_000], encoding="utf-8")
            self.page.screenshot(path=str(self.diag_dir / f"search_empty_{tag}.png"))
            self.log(f"empty search '{query}': landed on {self.page.url[:160]} title '{self.page.title()[:80]}'")
        out = []
        for c in cards or []:
            m = OTHER_HOTELS.search(c.get("href") or "")
            if m:
                out.append({**(c if raw else {}), "name": c.get("name", ""), "cc": m.group(1), "slug": m.group(2),
                            "price_text": c.get("price", "")})
        return out

    def calendar(self, cc: str, slug: str, start: date, days: int = 61) -> dict[str, dict]:
        if not self.token:
            raise RuntimeError("no session token yet (search or open a hotel page first)")
        self.calls += 1
        r = self.page.evaluate(CAL_JS, {"payload": calendar_payload(cc, slug, start, days, self.adults, self.rooms),
                                        "token": self.token})
        if r["status"] != 200:
            raise RuntimeError(f"calendar HTTP {r['status']}")
        return parse_calendar(r["text"])

    def year(self, cc: str, slug: str, today: date, windows: int = 6) -> dict[str, dict]:
        """Up to 12 months; a window that errors (e.g. no availability published that far ahead) is skipped.
        Raises only if every window fails."""
        nights, errors = {}, []
        for k in range(windows):
            try:
                nights.update(self.calendar(cc, slug, today + timedelta(days=61 * k)))
            except RuntimeError as e:
                errors.append(str(e))
        if not nights and errors:
            raise RuntimeError(errors[0])
        self.last_window_errors = len(errors)
        return nights

    def offers(self, cc: str, slug: str, night: date, nights: int = 1) -> list[dict]:
        """Room offers for a stay starting `night` (`nights` long, e.g. a minimum stay): price for the whole stay,
        per_night, currency, board, cancellation."""
        params = urlencode({"checkin": night.isoformat(), "checkout": (night + timedelta(days=nights)).isoformat(),
                            "group_adults": self.adults, "no_rooms": self.rooms, "group_children": 0,
                            "selected_currency": self.currency, "changed_currency": 1, "lang": "en-gb"})
        self.page.goto(f"{self.base}/hotel/{cc}/{slug}.html?{params}", wait_until="domcontentloaded", timeout=60000)
        _, rows = self._wait_for(ROWS_JS, "b_csrf_token", 40)
        out = []
        for r in rows or []:
            total = price_of(r["price"])
            out.append({"room": r["room"], "price": total, "per_night": round(total / nights, 2) if total else None,
                        "nights": nights, "currency": currency_of(r["price"]), "board": board_of(r["conditions"]),
                        "cancellation": cancellation_of(r["conditions"])})
        if out and out[0]["currency"]:
            self.currency_seen = out[0]["currency"]
        return out
