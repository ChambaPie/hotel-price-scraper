"""Probe: what Google Hotels shows a headless browser on GitHub's servers (issue #15 in the private repo).

    python -m hotel_scraper.google_probe --query "Haus Hepi Obertraun" --checkin 2026-11-26 --nights 2

Opens Google Travel's hotel search, accepts the consent page if shown, tries to set the dates, and saves the page,
a screenshot and a report: which sellers and prices are visible (official site, Booking.com, ...), and any deal tags
("less than usual", "Great deal"). Probe only; nothing is collected on a schedule yet.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

SELLER_HINTS = ["Official site", "Booking.com", "Expedia", "Hotels.com", "Agoda", "Trip.com", "Vio.com", "Priceline",
                "eDreams", "HRS", "Prestigia", "Destinia", "Zenhotels"]


def _varint(n: int) -> bytes:
    out = b""
    while True:
        b, n = n & 0x7F, n >> 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def _field(num: int, payload: bytes) -> bytes:
    return bytes([(num << 3) | 2]) + _varint(len(payload)) + payload


def _date(d: date) -> bytes:
    return b"\x08" + _varint(d.year) + b"\x10" + _varint(d.month) + b"\x18" + _varint(d.day)


def encode_ts(checkin: date, checkout: date, currency: str = "EUR") -> str:
    """Google Travel's `ts` URL parameter (a small protobuf) with the stay dates and currency, so the page opens on
    the right night without clicking through the date picker. Layout taken from a real Google Hotels URL
    (2026-11-10): ts=CAAaGhIYEhIKBwjqDxALGBESBwjqDxALGBIyAggAKgcKBToDVVNE = 2026-11-17 -> 2026-11-18, USD."""
    dates = _field(1, _date(checkin)) + _field(2, _date(checkout))
    msg = b"\x08\x00" + _field(3, _field(2, _field(2, dates) + _field(6, b"\x08\x00"))) + \
        _field(5, _field(1, _field(7, currency.encode())))
    return base64.urlsafe_b64encode(msg).decode().rstrip("=")


def seller_prices(text: str) -> list[tuple[str, str]]:
    """(seller, price) pairs from the page text: a seller line followed within two lines by a price."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    out = []
    for i, l in enumerate(lines):
        name = next((s for s in SELLER_HINTS if l.lower().startswith(s.lower())), None)
        if not name:
            continue
        for nxt in lines[i + 1:i + 3]:
            m = re.search(r"(?:€|US\$|\$)\s?\d[\d.,]*", nxt)
            if m:
                out.append((name, m.group(0)))
                break
    return out


def summarize(text: str) -> dict:
    prices = re.findall(r"(?:€|US\$|\$)\s?\d[\d.,]*", text)
    return {"prices_seen": len(prices), "price_sample": prices[:12],
            "sellers_seen": [s for s in SELLER_HINTS if s.lower() in text.lower()],
            "deal_tags": sorted(set(re.findall(r"\d+% less than usual|Great deal|Good deal|Deal", text)))[:10],
            "consent_wall": "Before you continue" in text or "consent.google" in text,
            "seller_prices": seller_prices(text)[:12]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", action="append", required=True)
    ap.add_argument("--checkin", default=(date.today() + timedelta(days=30)).isoformat())
    ap.add_argument("--nights", type=int, default=1)
    ap.add_argument("--out", default="out/google")
    args = ap.parse_args(argv)
    from playwright.sync_api import sync_playwright
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    ci = date.fromisoformat(args.checkin)
    co = ci + timedelta(days=args.nights)
    results = []
    with sync_playwright() as pw:
        br = pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = br.new_context(locale="en-GB", viewport={"width": 1366, "height": 900},
                             user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                                        "Chrome/130.0.0.0 Safari/537.36")
        page = ctx.new_page()
        for q in args.query:
            t0 = time.monotonic()
            ts = encode_ts(ci, co, "EUR")
            page.goto(f"https://www.google.com/travel/search?q={quote(q)}&hl=en-GB&gl=it&curr=EUR&ts={ts}",
                      wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
            consent = False
            for label in ("Accept all", "Reject all", "I agree"):
                btn = page.get_by_role("button", name=label)
                if btn.count():
                    btn.first.click()
                    consent = True
                    page.wait_for_timeout(3000)
                    break
            dates_set = False
            try:
                ci_box = page.get_by_role("textbox", name=re.compile("Check-in", re.I))
                co_box = page.get_by_role("textbox", name=re.compile("Check-out", re.I))
                if ci_box.count() and co_box.count():
                    ci_box.first.click(); ci_box.first.fill(ci.strftime("%a, %d %b")); ci_box.first.press("Enter")
                    co_box.first.click(); co_box.first.fill(co.strftime("%a, %d %b")); co_box.first.press("Enter")
                    page.wait_for_timeout(4000)
                    dates_set = True
            except Exception:  # noqa: BLE001
                pass
            page.wait_for_timeout(3000)
            if "/entity/" not in page.url:   # landed on a list: open the hotel whose name matches the query best
                links = page.locator('a[href*="/travel/hotels/entity/"]')
                if links.count():
                    words = [w.lower() for w in q.split()[:2]]
                    best = next((i for i in range(min(links.count(), 20))
                                 if all(w in (links.nth(i).inner_text() or "").lower() for w in words)), 0)
                    links.nth(best).click()
                    page.wait_for_timeout(5000)
            text = page.inner_text("body")
            slug = re.sub(r"[^A-Za-z0-9]+", "_", q)[:40]
            (out / f"{slug}.html").write_text(page.content()[:1_000_000], encoding="utf-8")
            page.screenshot(path=str(out / f"{slug}.png"), full_page=False)
            results.append({"query": q, "url": page.url[:200], "title": page.title()[:120], "consent_clicked": consent,
                            "dates_set": dates_set, "seconds": round(time.monotonic() - t0, 1), **summarize(text)})
        br.close()
    lines = [f"## Google Hotels probe ({date.today().isoformat()}), check-in {ci} for {args.nights} night(s)", ""]
    for r in results:
        lines += [f"### {r['query']}", f"- landed: {r['url']} — \"{r['title']}\"; consent clicked {r['consent_clicked']}; "
                  f"dates set {r['dates_set']}; {r['seconds']} s",
                  f"- prices seen {r['prices_seen']}: {', '.join(r['price_sample'])}",
                  f"- sellers seen: {', '.join(r['sellers_seen']) or '-'}; deal tags: {', '.join(r['deal_tags']) or '-'}",
                  f"- seller prices: {', '.join(f'{a} {b}' for a, b in r['seller_prices']) or '-'}", ""]
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    (out / "results.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
