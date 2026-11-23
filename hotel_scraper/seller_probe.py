"""Probe S0: which sellers' own websites give prices to an automated browser on GitHub's servers, and which booking
engines the watched hotels' official websites use (settings: seller_probe). Owner, 2026-11-23: prices only from the
sellers themselves (Booking.com, Expedia, Hotels.com, official sites...), never from comparison sites.

    python -m hotel_scraper.seller_probe [--part sellers|engines|all] [--hotels 6] [--out out/sellers]

sellers  For a few watched hotels, each seller's search page for one night: blocked or not, prices visible or not, the
         hotel found or not; saves the page text, a screenshot and the JSON the page loaded (to build readers from).
engines  For every watched hotel with an official website (OpenStreetMap's website tag): fetch the home page (and its
         booking link when needed) and name the booking engine from its links and scripts. Unknown booking domains
         are listed too, to find engines missing from the settings.
Probe only: nothing is collected on a schedule.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

from . import config
from .http import Session
from .scan import read_csv

PRICE = re.compile(r"(?:€\s?\d[\d.,]*|\d[\d.,]*\s?€|EUR\s?\d[\d.,]*)")
HREF = re.compile(r"""<a\b[^>]*?href\s*=\s*["']([^"'#][^"']*)["'][^>]*>(.*?)</a>""", re.I | re.S)
ANY_URL = re.compile(r"""https?://[^\s"'<>\\)]+""", re.I)
TAG = re.compile(r"<[^>]+>")


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def slug(s: str, n: int = 40) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:n]


def host(url: str) -> str:
    h = urlsplit(url).hostname or ""
    return h[4:] if h.startswith("www.") else h


def normal_site(url: str) -> str:
    url = (url or "").strip().split(";")[0].strip()
    if not url:
        return ""
    return url if re.match(r"^https?://", url, re.I) else "https://" + url


# ---------- sellers ----------

def blocked_by(text: str, title: str, phrases: list[str]) -> str:
    """The first block / challenge phrase on the page, or ""."""
    hay = f"{title}\n{text[:20000]}".lower()
    return next((p for p in phrases if p.lower() in hay), "")


def page_facts(text: str, title: str, name: str, phrases: list[str]) -> dict:
    words = [w for w in re.findall(r"[A-Za-zÀ-ÿ]{4,}", name) if w.lower() not in {"hotel", "resort", "garni", "haus"}]
    low = text.lower()
    return {"blocked": blocked_by(text, title, phrases), "prices": len(PRICE.findall(text)),
            "name_found": bool(words) and all(w.lower() in low for w in words[:3]), "chars": len(text)}


def sample_hotels(watch: list[dict], ids: dict[str, dict], n: int) -> list[dict]:
    """Up to n watched hotels with a Booking.com page (so prices can be compared later), one country at a time."""
    ok = [w for w in watch if not w.get("gone_since") and ids.get(w["pid"], {}).get("status") == "ok"]
    ok.sort(key=lambda w: (-float(w.get("review_count") or 0), w["pid"]))
    out, seen = [], Counter()
    for rnd in range(n):
        for w in ok:
            if len(out) >= n:
                return out
            if w not in out and seen[w.get("country")] == rnd:
                out.append(w)
                seen[w.get("country")] += 1
    return out


def probe_sellers(cfg: dict, hotels: list[dict], out: Path, checkin: date, log=log, only: list[str] | None = None) -> list[dict]:
    from playwright.sync_api import sync_playwright
    from .booking_session import launch_browser
    sp = cfg["seller_probe"]
    checkout = checkin + timedelta(days=1)
    rows = []
    with sync_playwright() as pw:
        br = launch_browser(pw, True)
        for seller, spec in sp["sellers"].items():
            if only and seller not in only:
                continue
            tpl = spec["home"] if isinstance(spec, dict) else spec
            ctx = br.new_context(user_agent=sp["user_agent"], locale="en-GB", viewport={"width": 1366, "height": 900},
                                 extra_http_headers={"Accept-Language": sp["accept_language"]})
            page = ctx.new_page()
            seen: list = []
            page.on("response", lambda r: seen.append(r))
            for w in hotels:
                q = f"{w['name']} {w.get('city', '')}".strip()
                url = tpl.format(q=quote(q), checkin=checkin.isoformat(), checkout=checkout.isoformat(),
                                 checkin_slash=checkin.strftime("%Y/%m/%d"), checkout_slash=checkout.strftime("%Y/%m/%d"),
                                 adults=cfg["scan"]["adults"])
                seen.clear()
                status, err, t0 = 0, "", time.monotonic()
                try:
                    r = page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    status = r.status if r else 0
                    page.wait_for_timeout(sp["wait_ms"])
                    if isinstance(spec, dict):
                        err = search_box(page, spec, q, sp["wait_ms"])
                except Exception as e:  # noqa: BLE001
                    err = str(e)[:160]
                text, title = "", ""
                try:
                    title, text = page.title(), page.inner_text("body")
                except Exception as e:  # noqa: BLE001
                    err = err or str(e)[:160]
                base = out / "pages" / f"{seller}_{slug(q)}"
                base.parent.mkdir(parents=True, exist_ok=True)
                base.with_suffix(".txt").write_text(f"#URL {url}\n#FINAL {page.url}\n#TITLE {title}\n{text[:300_000]}",
                                                   encoding="utf-8")
                try:
                    page.screenshot(path=str(base.with_suffix(".png")))
                except Exception:  # noqa: BLE001
                    pass
                api = save_json(seen, base, sp["json_url_pattern"], sp["json_per_page"], name_words(w["name"]))
                facts = page_facts(text, title, w["name"], sp["block_phrases"])
                if not facts["blocked"] and (status in (403, 429) or "ERR_HTTP2_PROTOCOL_ERROR" in err):
                    facts["blocked"] = f"HTTP {status}" if status else "connection refused (HTTP/2 error)"
                row = {"seller": seller, "pid": w["pid"], "hotel": w["name"], "country": w.get("country", ""),
                       "status": status, "final_host": host(page.url), "title": title[:80], **facts, "json_saved": api,
                       "seconds": round(time.monotonic() - t0, 1), "error": err}
                rows.append(row)
                log(f"{seller:10} {w['name'][:30]:30} {status} blocked={facts['blocked'] or '-'} prices={facts['prices']} "
                    f"name={facts['name_found']} json={api}")
                time.sleep(sp["pause_seconds"])
            ctx.close()
        br.close()
    return rows


def name_words(name: str) -> list[str]:
    return [x.lower() for x in re.findall(r"[A-Za-zÀ-ÿ]{5,}", name) if x.lower() not in {"hotel", "resort", "garni"}][:2]


def search_box(page, spec: dict, query: str, wait_ms: int) -> str:
    """Use the site's own search box: type the hotel, take the first suggestion, search. Returns "" or what failed."""
    box = page.locator(spec["input"] + " >> visible=true").first
    if not box.count():
        return "search box not found"
    box.click()
    box.fill("")
    box.type(query, delay=60)
    page.wait_for_timeout(3000)
    sug = page.locator(spec["suggestion"] + " >> visible=true")
    if sug.count():
        sug.first.click()
    else:
        page.keyboard.press("ArrowDown")
        page.keyboard.press("Enter")
    page.wait_for_timeout(1500)
    btn = page.locator(spec["search"] + " >> visible=true")
    if btn.count():
        try:
            with page.context.expect_page(timeout=4000) as new:   # some sites open results in a new tab
                btn.first.click()
            np = new.value
            np.wait_for_load_state("domcontentloaded")
            page.goto(np.url, wait_until="domcontentloaded")
            np.close()
        except Exception:  # noqa: BLE001  (same tab)
            pass
    page.wait_for_timeout(wait_ms)
    return "" if sug.count() else "no suggestion list (pressed Enter)"


def save_json(responses, base: Path, pattern: str, limit: int, words: list[str] = ()) -> int:
    """JSON the page loaded from its own APIs (search, graphql...), to build a reader from a real response. Responses
    that mention the hotel's name come first."""
    rx, n = re.compile(pattern, re.I), 0
    found = []
    for r in responses:
        try:
            if "json" not in (r.headers.get("content-type") or ""):
                continue
            body = r.body()
        except Exception:  # noqa: BLE001
            continue
        if len(body) < 500:
            continue
        low = body[:2_000_000].decode("utf-8", "replace").lower()
        named = bool(words) and any(w in low for w in words)
        if named or rx.search(r.url):
            found.append((not named, r, body))
    found.sort(key=lambda x: x[0])
    for _, r, body in found:
        if n >= limit:
            break
        p = base.parent / f"{base.name}.api{n}.json"
        p.write_bytes(body[:400_000])
        p.with_suffix(".url").write_text(r.request.method + " " + r.url[:500] + "\n" + (r.request.post_data or "")[:4000],
                                         encoding="utf-8")
        n += 1
    return n


# ---------- official sites: booking engines ----------

def links(html: str, base: str) -> list[tuple[str, str]]:
    """(absolute url, visible text) of every link."""
    out = []
    for href, inner in HREF.findall(html):
        if href.lower().startswith(("mailto:", "tel:", "javascript:")):
            continue
        out.append((urljoin(base, href.strip()), re.sub(r"\s+", " ", TAG.sub(" ", inner)).strip()[:80]))
    return out


def engine_of(html: str, final_url: str, engines: dict[str, list[str]]) -> tuple[str, str]:
    """(engine, evidence) from the page's own address, links and scripts; ("", "") when none matches."""
    hay = (final_url + "\n" + html).lower()
    for name, patterns in engines.items():
        for p in patterns:
            m = re.search(p, hay)
            if m:
                return name, m.group(0)[:80]
    return "", ""


def booking_links(html: str, base: str, words: list[str], skip: list[str] = ()) -> list[str]:
    """Links that look like "Book now" / "Prenota" / "Buchen" / "Anfrage", best first: other sites first (an engine)."""
    rx = re.compile("|".join(words), re.I)
    own = host(base)
    found = []
    for url, text in links(html, base):
        if not url.startswith("http") or any(n in host(url) for n in skip):
            continue
        if rx.search(text) or rx.search(urlsplit(url).path + "?" + urlsplit(url).query):
            if url not in found:
                found.append(url)
    return sorted(found, key=lambda u: host(u) == own)


def survey_site(row: dict, cfg: dict, fetch=None) -> dict:
    sp = cfg["seller_probe"]
    site = normal_site(row["website"])
    out = {"pid": row["pid"], "name": row["name"], "country": row.get("country", ""), "website": site, "status": "",
           "engine": "", "evidence": "", "booking_link": "", "booking_host": "", "pages": 0}
    if fetch is None:
        sess = Session(sp["user_agent"], timeout=sp["site_timeout"], headers={"Accept-Language": sp["accept_language"]})
        fetch = lambda u: sess.request("GET", u)  # noqa: E731
    r = fetch(site)
    out["pages"] = 1
    out["status"] = str(r.status) if r.status else f"error: {r.error[:60]}"
    if r.status != 200:
        return out
    html = r.text()
    eng, ev = engine_of(html, r.url or site, sp["engines"])
    bl = booking_links(html, r.url or site, sp["booking_words"], sp["not_engines"])
    if bl:
        out["booking_link"], out["booking_host"] = bl[0][:300], host(bl[0])
    if eng:
        out["engine"], out["evidence"] = eng, ev
        return out
    for url in bl[: sp["follow_links"]]:   # the booking page itself (often another site: the engine)
        r2 = fetch(url)
        out["pages"] += 1
        if r2.status == 200 or r2.url:
            eng, ev = engine_of(r2.text() if r2.status == 200 else "", r2.url or url, sp["engines"])
            if eng:
                out["engine"], out["evidence"], out["booking_link"], out["booking_host"] = eng, ev, url[:300], host(r2.url or url)
                return out
    return out


def survey_engines(cfg: dict, watch: list[dict], osm: dict[str, dict], log=log, fetch=None) -> list[dict]:
    sites = []
    for w in watch:
        if w.get("gone_since"):
            continue
        site = osm.get(w.get("osm_pid") or "", {}).get("website", "")
        if site:
            sites.append({**w, "website": site})
    log(f"engines: {len(sites)} watched hotels with an official website")
    with ThreadPoolExecutor(cfg["seller_probe"]["site_workers"]) as ex:
        rows = list(ex.map(lambda r: survey_site(r, cfg, fetch), sites))
    return rows


# ---------- report ----------

def report(seller_rows: list[dict], engine_rows: list[dict], watched: int) -> str:
    md = ["# Sellers' own websites: probe S0", ""]
    if seller_rows:
        md += ["## Sellers (one night, search page)", "", "| seller | pages | blocked | with prices | hotel found | JSON saved |",
               "|---|---|---|---|---|---|"]
        by = {}
        for r in seller_rows:
            by.setdefault(r["seller"], []).append(r)
        for s, rs in by.items():
            md.append(f"| {s} | {len(rs)} | {sum(1 for r in rs if r['blocked'])} | {sum(1 for r in rs if r['prices'] >= 3)} | "
                      f"{sum(1 for r in rs if r['name_found'])} | {sum(r['json_saved'] for r in rs)} |")
        md += ["", "Block phrases seen: " + (", ".join(sorted({r['blocked'] for r in seller_rows if r['blocked']})) or "none"), ""]
    if engine_rows:
        ok = [r for r in engine_rows if r["status"] == "200"]
        c = Counter(r["engine"] or "(not recognised)" for r in ok)
        md += [f"## Official websites: booking engines", "",
               f"{len(engine_rows)} of {watched} watched hotels have an official website in OpenStreetMap; {len(ok)} answered.", "",
               "| engine | hotels |", "|---|---|"] + [f"| {k} | {v} |" for k, v in c.most_common()]
        unk = Counter(r["booking_host"] for r in ok if not r["engine"] and r["booking_host"] and r["booking_host"] != host(r["website"]))
        md += ["", "Booking links on other sites, engine not recognised (candidates to add to the settings):", ""]
        md += [f"- {h}: {n}" for h, n in unk.most_common(40)] or ["- none"]
        bad = Counter(r["status"] for r in engine_rows if r["status"] != "200")
        md += ["", "Sites not answering 200: " + (", ".join(f"{k} x{v}" for k, v in bad.most_common(8)) or "none")]
    return "\n".join(md) + "\n"


def write_rows(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(config.DEFAULT_PATH))
    ap.add_argument("--part", choices=["sellers", "engines", "all"], default="all")
    ap.add_argument("--hotels", type=int)
    ap.add_argument("--sellers", default="", help="comma-separated seller keys (default: all in the settings)")
    ap.add_argument("--checkin", help="YYYY-MM-DD (default: settings seller_probe.checkin_offset_days from today)")
    ap.add_argument("--out", default="out/sellers")
    args = ap.parse_args(argv)
    cfg = config.load(args.config)
    sp, p = cfg["seller_probe"], cfg["paths"]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    watch = read_csv(p["watchlist"])
    seller_rows, engine_rows = [], []
    if args.part in ("sellers", "all"):
        ids = {r["pid"]: r for r in read_csv(p["booking_ids"])}
        hotels = sample_hotels(watch, ids, args.hotels or sp["hotels"])
        checkin = date.fromisoformat(args.checkin) if args.checkin else date.today() + timedelta(days=sp["checkin_offset_days"])
        seller_rows = probe_sellers(cfg, hotels, out, checkin, only=[x for x in args.sellers.split(",") if x])
        write_rows(out / "sellers.csv", seller_rows)
    if args.part in ("engines", "all"):
        osm = {r["pid"]: r for r in read_csv(p["osm_catalog"])}
        engine_rows = survey_engines(cfg, watch, osm)
        write_rows(out / "engines.csv", engine_rows)
    md = report(seller_rows, engine_rows, sum(1 for w in watch if not w.get("gone_since")))
    (out / "report.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
