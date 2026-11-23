"""Google rating, number of reviews, hotel class and amenities for watched properties, from Google Hotels search
results (settings: google_ratings). Google Hotels shows Google's own reviews (the same as Google Maps).

One search per property ("<name> <city>"); the results list has a card per hotel:

    Haus Hepi / €113 / 5.0 / (191) / Amenities for Haus Hepi: Free breakfast, Free Wi-Fi, ... / View prices

The best-matching card (name similarity) is kept; other watched properties of the same town found in the same list
are filled too. Rolling: properties never checked first, then the oldest, each refreshed every refresh_days.
Personal, low-volume use; Google's terms don't allow automated access.

    python -m hotel_scraper.google_ratings [--max-minutes 20]
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote

from . import config
from .discover_osm import _norm_name
from .scan import read_csv, write_csv

FIELDS = ["pid", "google_name", "google_rating", "google_reviews", "google_class", "amenities", "similarity", "query",
          "status", "checked_at"]
RATING = re.compile(r"^(\d[.,]\d)(?:/5)?$")   # always one decimal ("5.0"): a lone "2" is the guests box
COUNT = re.compile(r"^\((\d[\d.,]*)\s*([kKmM]?)\)$")
PRICE = re.compile(r"^(?:€|US\$|\$|£|CHF)\s?\d")
CLASS = re.compile(r"(\d)-star hotel")
ENDS = ("View prices", "View details", "View hotel")
NOT_NAMES = {"Sponsored", "·", "Excellent location", "Great location", "Good location", "Deal", "Great deal",
             "About these results"}


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%H:%M:%S} {msg}", flush=True)


def parse_count(num: str, suffix: str = "") -> int | None:
    """"191" -> 191, "2.8" + "k" -> 2800, "1,234" -> 1234."""
    s = num.strip()
    if suffix:
        try:
            return int(round(float(s.replace(",", ".")) * (1000 if suffix.lower() == "k" else 1_000_000)))
        except ValueError:
            return None
    s = re.sub(r"[.,]", "", s)
    return int(s) if s.isdigit() else None


def cards_from_text(text: str) -> list[dict]:
    """Hotel cards from the text of a Google Hotels results page: [{name, rating, reviews, stars, amenities}].
    A card ends with "View prices" / "View details"; its name is the last line before the rating that isn't a price
    or a label; amenities come from the "Amenities for <name>[, a 4-star hotel.]: a, b, c," line."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    cards, block = [], []
    for line in lines:
        if line in ENDS:
            card = _card(block)
            if card:
                cards.append(card)
            block = []
        else:
            block.append(line)
    return cards


def _card(block: list[str]) -> dict | None:
    ri = next((i for i, l in enumerate(block) if RATING.match(l)), None)
    if ri is None:
        return None
    name = next((l for l in reversed(block[:ri]) if not PRICE.match(l) and l not in NOT_NAMES and len(l) > 2), "")
    amen_line = next((l for l in block if l.startswith("Amenities for ")), "")
    if amen_line:   # the name as Google writes it in the amenities line is the most reliable
        m = re.match(r"Amenities for (.+?)(?:, an? \d-star hotel\.)?: ", amen_line)
        if m:
            name = m.group(1)
    if not name:
        return None
    reviews = None
    if ri + 1 < len(block):
        m = COUNT.match(block[ri + 1])
        if m:
            reviews = parse_count(m.group(1), m.group(2))
    stars = None
    for l in block:
        m = CLASS.search(l)
        if m:
            stars = int(m.group(1))
            break
    amenities = []
    if ":" in amen_line:
        amenities = [a.strip() for a in amen_line.split(":", 1)[1].split(",") if a.strip()]
    return {"name": name, "rating": float(RATING.match(block[ri]).group(1).replace(",", ".")), "reviews": reviews,
            "stars": stars, "amenities": amenities}


def similarity(a: str, b: str) -> float:
    na, nb = _norm_name(a), _norm_name(b)
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:   # "Haus Hepi" vs "Haus Hepi Obertraun"
        return max(0.9, SequenceMatcher(None, na, nb).ratio())
    return round(SequenceMatcher(None, na, nb).ratio(), 3)


def best_card(name: str, cards: list[dict], min_sim: float) -> tuple[dict | None, float]:
    best, score = None, 0.0
    for c in cards:
        s = similarity(name, c["name"])
        if s > score:
            best, score = c, s
    return (best, round(score, 3)) if score >= min_sim else (None, round(score, 3))


def row_for(pid: str, card: dict | None, sim: float, query: str, now: str, status: str) -> dict:
    c = card or {}
    return {"pid": pid, "google_name": c.get("name", ""), "google_rating": c.get("rating", ""),
            "google_reviews": "" if c.get("reviews") is None else c["reviews"],
            "google_class": c.get("stars") or "", "amenities": "|".join(c.get("amenities") or []),
            "similarity": sim, "query": query, "status": status, "checked_at": now}


def due(watch: list[dict], known: dict[str, dict], today: date, refresh_days: int, retry_days: int) -> list[dict]:
    """Watched properties to check now: never checked first, then the oldest; a miss is retried after retry_days."""
    out = []
    for w in watch:
        k = known.get(w["pid"])
        if not k or not k.get("checked_at"):
            out.append((0, "", w))
            continue
        age = (today - date.fromisoformat(k["checked_at"][:10])).days
        wait = refresh_days if k.get("status") == "ok" else retry_days
        if age >= wait:
            out.append((1, k["checked_at"], w))
    return [w for _, _, w in sorted(out, key=lambda x: (x[0], x[1]))]


def search(page, query: str, wait_ms: int) -> str:
    page.goto(f"https://www.google.com/travel/search?q={quote(query)}&hl=en-GB&gl=it&curr=EUR",
              wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2500)
    for label in ("Accept all", "Reject all", "I agree"):
        btn = page.get_by_role("button", name=label)
        if btn.count():
            btn.first.click()
            page.wait_for_timeout(2500)
            break
    page.wait_for_timeout(wait_ms)
    return page.inner_text("body")


def run(cfg: dict, max_minutes: float, search_fn, today: date | None = None, log=log,
        clock=time.monotonic, samples: Path | None = None, sleep=time.sleep) -> dict:
    gc, p = cfg["google_ratings"], cfg["paths"]
    today = today or date.today()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    watch = [w for w in read_csv(p["watchlist"]) if not w.get("gone_since")]
    known = {r["pid"]: r for r in read_csv(p["google_ratings"])}
    todo = due(watch, known, today, gc["refresh_days"], gc["retry_days"])[: gc["max_per_run"]]
    by_city: dict[str, list[dict]] = {}
    for w in watch:
        by_city.setdefault((w.get("city") or "").lower(), []).append(w)
    started, stats = clock(), {"due": len(todo), "checked": 0, "found": 0, "also_filled": 0, "errors": 0}
    done: set[str] = set()
    saved = 0
    for w in todo:
        if clock() - started > max_minutes * 60:
            break
        if w["pid"] in done:
            continue
        query = f"{w['name']} {w.get('city', '')}".strip()
        try:
            text = search_fn(query)
        except Exception as e:  # noqa: BLE001  (one search failing must not stop the run)
            stats["errors"] += 1
            log(f"{query[:50]}: {str(e)[:100]}")
            known[w["pid"]] = row_for(w["pid"], None, 0.0, query, now, "error")
            continue
        cards = cards_from_text(text)
        if samples and saved < 2:   # a couple of real pages per run, for the parser's regression tests
            samples.mkdir(parents=True, exist_ok=True)
            (samples / f"sample_{saved}.txt").write_text(text[:200_000], encoding="utf-8")
            saved += 1
        card, sim = best_card(w["name"], cards, gc["min_similarity"])
        known[w["pid"]] = row_for(w["pid"], card, sim, query, now, "ok" if card else "not found")
        stats["checked"] += 1
        stats["found"] += 1 if card else 0
        done.add(w["pid"])
        for other in by_city.get((w.get("city") or "").lower(), []):   # neighbours in the same list
            if other["pid"] in done:
                continue
            c2, s2 = best_card(other["name"], cards, gc["min_similarity_others"])
            if c2:
                known[other["pid"]] = row_for(other["pid"], c2, s2, f"(with) {query}", now, "ok")
                done.add(other["pid"])
                stats["also_filled"] += 1
        log(f"{w['name'][:40]}: {card['rating'] if card else '-'} ({card['reviews'] if card else '-'}) "
            f"sim {sim}, {len(cards)} cards")
        sleep(gc["pause_seconds"])
    write_csv(sorted(known.values(), key=lambda r: r["pid"]), p["google_ratings"], FIELDS)
    have = sum(1 for w in watch if known.get(w["pid"], {}).get("status") == "ok")
    stats["coverage"] = f"{have}/{len(watch)}"
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(config.DEFAULT_PATH))
    ap.add_argument("--max-minutes", type=float)
    args = ap.parse_args(argv)
    cfg = config.load(args.config)
    gc = cfg["google_ratings"]
    from playwright.sync_api import sync_playwright
    from .booking_session import launch_browser
    with sync_playwright() as pw:
        br = launch_browser(pw, True)
        ctx = br.new_context(locale="en-GB", viewport={"width": 1366, "height": 900}, user_agent=gc["user_agent"])
        page = ctx.new_page()

        def search_fn(q):
            return search(page, q, gc["wait_ms"])
        stats = run(cfg, args.max_minutes or gc["max_minutes"], search_fn,
                    samples=Path(cfg["paths"]["out_dir"]) / "google_samples")
        br.close()
    log(f"google ratings: {stats}")
    out = Path(cfg["paths"]["out_dir"])
    out.mkdir(parents=True, exist_ok=True)
    (out / "google_ratings_summary.md").write_text(
        "## Google ratings\n\n" + "\n".join(f"- {k}: {v}" for k, v in stats.items()) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
