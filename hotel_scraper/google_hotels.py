"""Google Hotels prices for one hotel and stay, from an open Playwright page (cross-check with other sellers,
including the hotel's official site). Dates go in the URL (`ts`, see google_probe.encode_ts), no date picker.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from urllib.parse import quote

from .google_probe import encode_ts, seller_prices

PRICE = re.compile(r"(?:€|US\$|\$)\s?(\d[\d.,]*)")


def amount(text: str) -> float | None:
    m = PRICE.search(text or "")
    if not m:
        return None
    raw = m.group(1)
    if re.fullmatch(r"\d{1,3}([.,]\d{3})+", raw):
        raw = re.sub(r"[.,]", "", raw)
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return None


def best_by_seller(pairs: list[tuple[str, str]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for seller, price in pairs:
        a = amount(price)
        if a is not None and (seller not in out or a < out[seller]):
            out[seller] = a
    return out


def hotel_prices(page, query: str, checkin: date, nights: int = 1, currency: str = "EUR", wait_ms: int = 5000) -> dict:
    """{"url", "found": bool, "sellers": {seller: price per stay as shown}} for the hotel best matching `query`."""
    ts = encode_ts(checkin, checkin + timedelta(days=nights), currency)
    page.goto(f"https://www.google.com/travel/search?q={quote(query)}&hl=en-GB&gl=it&curr={currency}&ts={ts}",
              wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)
    if "/entity/" not in page.url:
        hrefs = page.evaluate("""() => Array.from(document.querySelectorAll('a[href*="/travel/hotels/entity/"]'))
            .map(a => ({href: a.href, text: (a.innerText || a.getAttribute('aria-label') || '').slice(0, 120)}))""")
        words = [w.lower() for w in query.split()[:2]]
        pick = next((h for h in hrefs if all(w in h["text"].lower() for w in words)), None)
        if not pick:
            return {"url": page.url[:200], "found": False, "sellers": {}}
        url = pick["href"] + ("" if "ts=" in pick["href"] else ("&" if "?" in pick["href"] else "?") +
                              f"ts={ts}&hl=en-GB&gl=it&curr={currency}")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(wait_ms)
    tab = page.get_by_role("tab", name=re.compile("Prices", re.I))
    if tab.count():
        tab.first.click()
        page.wait_for_timeout(3000)
    return {"url": page.url[:300], "found": True, "sellers": best_by_seller(seller_prices(page.inner_text("body")))}
