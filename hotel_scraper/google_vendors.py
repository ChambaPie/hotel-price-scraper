"""Who sells each watched hotel, from Google Hotels' prices page (settings: google_vendors). Owner, 2026-11-23: Google
and trivago are a good baseline for the list of hotels and vendors; for actual deals, every vendor's own site.

Google's prices are never kept: only which vendors list the hotel (and whether its official site does), and the
official website's address. data/hotel_vendors.csv, rolling (refresh_days); the summary counts hotels per vendor,
the list the readers are built from.

    python -m hotel_scraper.google_vendors [--max-minutes 20]
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from . import config
from .google_probe import encode_ts
from .google_ratings import similarity
from .scan import read_csv, write_csv

FIELDS = ["pid", "google_name", "vendors", "official", "website", "entity", "status", "checked_at"]
PRICE = re.compile(r"^(?:€|US\$|\$|£|CHF)\s?\d[\d.,]*\s*\*?$")
STARTS = ("All options", "Sponsored·Featured options", "Featured options")
ENDS = ("Sponsored·Similar hotels", "Similar hotels", "Nearby places", "Reviews", "About this data", "Track this hotel",
        "View more options", "Fewer options", "Web results", "Frequently asked questions")   # "Fewer options": the
                                    # list once opened up (run 45271962537 counted it as a vendor)
SKIP = {"Visit site", "Visit site for more options", "Nightly price with taxes + fees", "Nightly price with fees", "*",
        "Free cancellation only", "Official site", "Book a room"}
DOMAIN = re.compile(r"\.[a-z]{2,6}$|^(official site)$", re.I)
NOISE = re.compile(r"^(Fewer options|View more options|Currency)", re.I)   # page controls, not vendors (run 45345520196)


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%H:%M:%S} {msg}", flush=True)


def vendors_from_text(text: str, known: list[str] = ()) -> tuple[list[str], bool]:
    """(vendors in Google's order, official site listed) from the text of a Google Hotels hotel page (Prices tab or
    Overview). Offers come in blocks ending "Visit site": the block's first line is the vendor, or the hotel's name
    followed by "Official site". In the featured part a vendor's extra rooms are blocks too: there a vendor must look
    like one (a web address such as "Expedia.com", or a name in `known`)."""
    lines = [l.strip() for l in text.splitlines()]
    lines = [l for l in lines if l]
    out, official, section, block = [], False, "", []
    knownl = {k.lower() for k in known}

    def close(blk):
        nonlocal official
        if not blk:
            return
        first = blk[0]
        if len(blk) > 1 and blk[1] == "Official site":
            official = True
            name = "Official site"
        elif first in SKIP or PRICE.match(first):
            return
        else:
            name = first
            if section != "all" and not (DOMAIN.search(name) or name.lower() in knownl):
                return                      # a room of the vendor above (featured part)
        if name not in out and not NOISE.match(name):
            out.append(name)

    for l in lines:
        if l in STARTS:
            close(block)
            section, block = ("all" if l == "All options" else "featured"), []
            continue
        if not section:
            continue
        if l in ENDS or l.startswith("View more options"):
            close(block)
            section, block = "", []
            continue
        if l == "Visit site" or l.startswith("Visit "):
            close(block)
            block = []
            continue
        block.append(l)
    close(block)
    return out, official


def canonical(name: str, aliases: dict) -> str:
    """One name per vendor: "Expedia.com" -> "Expedia" (settings google_vendors.aliases), else as Google writes it."""
    n = name.strip()
    return aliases.get(n.lower(), n)


def due(watch: list[dict], known: dict, today: date, refresh_days: int, retry_days: int) -> list[dict]:
    out = []
    for w in watch:
        k = known.get(w["pid"])
        if not k:
            out.append((0, "", w))
            continue
        age = (today - date.fromisoformat(k["checked_at"][:10])).days
        if age >= (refresh_days if k["status"] == "ok" else retry_days):
            out.append((1, k["checked_at"], w))
    return [w for _, _, w in sorted(out, key=lambda x: (x[0], x[1]))]


ENTITY_JS = """() => Array.from(document.querySelectorAll('a[href*="/travel/hotels/entity/"]'))
    .map(a => ({href: a.href, text: (a.getAttribute('aria-label') || a.innerText || '').slice(0, 160)}))"""
WEBSITE_JS = """() => Array.from(document.querySelectorAll('a')).filter(a => /^\\s*Website\\s*$/i.test(a.innerText || '')
    || /^Website/i.test(a.getAttribute('aria-label') || '')).map(a => a.href).slice(0, 3)"""


def open_hotel(page, w: dict, gv: dict, today: date, offset: int | None = None) -> tuple[str, str, str, str]:
    """(prices page text, Google's hotel name, entity address, official website) for a watched hotel; the text of
    Google's list when no card matches (kept as a sample)."""
    night = today + timedelta(days=offset or gv["offset_days"])
    ts = encode_ts(night, night + timedelta(days=1), "EUR")
    q = f"{w['name']} {w.get('city', '')}".strip()
    page.goto(f"https://www.google.com/travel/search?q={quote(q)}&hl=en-GB&gl=it&curr=EUR&ts={ts}",
              wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2500)
    for label in ("Accept all", "Reject all", "I agree"):
        b = page.get_by_role("button", name=label)
        if b.count():
            b.first.click()
            page.wait_for_timeout(2500)
            break
    page.wait_for_timeout(gv["wait_ms"])
    if "/entity/" not in page.url:              # a list: open the card that matches the hotel
        best, score = None, 0.0
        for h in page.evaluate(ENTITY_JS):
            s = similarity(w["name"], h["text"].split("\n")[0])
            if s > score:
                best, score = h, s
        if not best or score < gv["min_similarity"]:
            return page.inner_text("body"), "", "", ""
        page.goto(best["href"], wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(gv["wait_ms"])
    entity = page.url.split("?")[0]
    if "/entity/" not in entity:
        return "", "", "", ""
    entity = re.sub(r"/(prices|reviews|location|about|photos|overview)$", "", entity)
    try:
        sites = page.evaluate(WEBSITE_JS)
    except Exception:  # noqa: BLE001
        sites = []
    website = next((s for s in sites if s and "google." not in urlsplit(s).netloc), "")
    page.goto(f"{entity}/prices?hl=en-GB&gl=it&curr=EUR&ts={ts}", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(gv["wait_ms"])
    more = page.get_by_role("button", name=re.compile("View more options", re.I))
    if more.count():
        try:
            more.first.click()
            page.wait_for_timeout(2000)
        except Exception:  # noqa: BLE001
            pass
    text = page.inner_text("body")
    name = next((l.strip() for l in text.splitlines()[:12] if similarity(w["name"], l.strip()) >= gv["min_similarity"]), "")
    return text, name, entity, website


def run(cfg: dict, max_minutes: float, open_fn, today: date | None = None, log=log, clock=time.monotonic,
        sleep=time.sleep, samples: Path | None = None) -> dict:
    gv, p = cfg["google_vendors"], cfg["paths"]
    today = today or date.today()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    watch = [w for w in read_csv(p["watchlist"]) if not w.get("gone_since")]
    known = {r["pid"]: r for r in read_csv(p["hotel_vendors"])}
    todo = due(watch, known, today, gv["refresh_days"], gv["retry_days"])[: gv["max_per_run"]]
    stats = {"due": len(todo), "checked": 0, "found": 0, "errors": 0}
    started, saved = clock(), 0
    for w in todo:
        if clock() - started > max_minutes * 60:
            break
        try:
            text, gname, entity, website = open_fn(w)
        except Exception as e:  # noqa: BLE001
            stats["errors"] += 1
            log(f"{w['name'][:40]}: {str(e)[:100]}")
            continue
        vendors, official = vendors_from_text(text, gv["known"]) if text else ([], False)
        for off in gv.get("retry_offsets_days") or []:      # nothing sold that night (small places often need 3+
            if vendors or not entity:                       # nights, or close for the season): other nights
                break
            try:
                text2, gname2, entity2, website2 = open_fn(w, off)
            except Exception as e:  # noqa: BLE001
                stats["errors"] += 1
                log(f"{w['name'][:40]} (+{off} days): {str(e)[:100]}")
                break
            vendors, official = vendors_from_text(text2, gv["known"]) if text2 else ([], False)
            if vendors:
                text, gname, entity, website = text2, gname2 or gname, entity2 or entity, website2 or website
        names = [canonical(v, gv["aliases"]) for v in vendors]
        ok = bool(vendors)
        if samples and not ok and saved < gv["samples_per_run"] and text:
            samples.mkdir(parents=True, exist_ok=True)
            (samples / f"miss_{saved}.txt").write_text(f"#HOTEL {w['name']}\n#ENTITY {entity}\n{text[:200_000]}", encoding="utf-8")
            saved += 1
        known[w["pid"]] = {"pid": w["pid"], "google_name": gname, "vendors": "|".join(names),
                           "official": "yes" if official else "", "website": website[:300], "entity": entity[:200],
                           "status": "ok" if ok else "not found", "checked_at": now}
        stats["checked"] += 1
        stats["found"] += 1 if ok else 0
        log(f"{w['name'][:40]}: {len(names)} vendors{' + official site' if official else ''}: {', '.join(names)[:150]}")
        sleep(gv["pause_seconds"])
    write_csv(sorted(known.values(), key=lambda r: r["pid"]), p["hotel_vendors"], FIELDS)
    rows = [r for r in known.values() if r["status"] == "ok"]
    stats["coverage"] = f"{len(rows)}/{len(watch)}"
    stats["vendors"] = dict(Counter(v for r in rows for v in r["vendors"].split("|") if v and not NOISE.match(v)).most_common())
    stats["with_official_site"] = sum(1 for r in rows if r["official"] == "yes")
    stats["with_website"] = sum(1 for r in known.values() if r["website"])
    return stats


def summary_md(s: dict) -> str:
    md = ["## Vendors per hotel (Google Hotels' list of sellers)", "",
          f"Checked {s['checked']} of {s['due']} due ({s['found']} with a list of sellers); coverage {s['coverage']}; "
          f"official site listed for {s['with_official_site']}, website address known for {s['with_website']}.", "",
          "| vendor | hotels |", "|---|---|"]
    md += [f"| {k} | {v} |" for k, v in s["vendors"].items()]
    return "\n".join(md) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(config.DEFAULT_PATH))
    ap.add_argument("--max-minutes", type=float)
    args = ap.parse_args(argv)
    cfg = config.load(args.config)
    gv = cfg["google_vendors"]
    from playwright.sync_api import sync_playwright
    from .booking_session import launch_browser
    with sync_playwright() as pw:
        br = launch_browser(pw, True)
        page = br.new_context(locale="en-GB", viewport={"width": 1366, "height": 900},
                              user_agent=cfg["google_ratings"]["user_agent"]).new_page()
        stats = run(cfg, args.max_minutes or gv["max_minutes"],
                    lambda w, off=None: open_hotel(page, w, gv, date.today(), off),
                    samples=Path(cfg["paths"]["out_dir"]) / "vendor_samples")
        br.close()
    md = summary_md(stats)
    out = Path(cfg["paths"]["out_dir"])
    out.mkdir(parents=True, exist_ok=True)
    (out / "google_vendors_summary.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
