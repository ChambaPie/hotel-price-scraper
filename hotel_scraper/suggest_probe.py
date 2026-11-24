"""Probe: what a seller's search box asks its server while you type, and what its suggestion list looks like
(settings: suggest_probe). To read a seller's own hotel suggestions directly instead of driving the search box.

    python -m hotel_scraper.suggest_probe --seller agoda --query "Hotel Gesser Sillian" --out out/suggest
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config
from .seller_probe import accept_cookies, log, slug

DOM_JS = """(word) => Array.from(document.querySelectorAll('li, [role=option], a, button, div'))
  .filter(e => e.children.length < 12 && (e.innerText || '').toLowerCase().includes(word) && e.offsetParent !== null)
  .slice(0, 12).map(e => ({tag: e.tagName, attrs: Array.from(e.attributes).map(a => a.name + '=' + a.value.slice(0, 80)),
                           text: (e.innerText || '').slice(0, 120)}))"""


def finished_calls(reqs: list, done: set, body_max: int) -> list[dict]:
    """The page's own xhr/fetch calls with their answers; a call still open is listed without waiting for it."""
    calls = []
    for r in reqs:
        if r.resource_type not in ("xhr", "fetch"):
            continue
        item = {"method": r.method, "url": r.url[:600], "post": (r.post_data or "")[:20000],
                "headers": {k: v for k, v in r.headers.items() if k.lower() != "cookie"}}
        if id(r) not in done:
            item["status"] = "open"
            calls.append(item)
            continue
        try:
            resp = r.response()
            if resp:
                item["status"] = resp.status
                item["type"] = resp.headers.get("content-type", "")
                item["body"] = resp.text()[:body_max]
        except Exception as e:  # noqa: BLE001
            item["error"] = str(e)[:100]
        calls.append(item)
    return calls


def open_page_calls(page, url: str, out: Path, seller: str, cookie_buttons, wait_s: float = 25) -> None:
    """Open one of the seller's pages and keep its text, a screenshot and every call it makes (to find its price
    call)."""
    reqs, done = [], set()
    on_req, on_done = (lambda r: reqs.append(r)), (lambda r: done.add(id(r)))
    page.on("request", on_req)
    page.on("requestfinished", on_done)
    page.on("requestfailed", on_done)
    name = f"{seller}_page_{slug(url.split('://', 1)[-1])[:60]}"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        accept_cookies(page, cookie_buttons)
        for _ in range(int(wait_s / 2.5)):
            page.wait_for_timeout(2500)
            try:
                page.mouse.wheel(0, 400)
            except Exception:  # noqa: BLE001
                pass
        (out / f"{name}_after.txt").write_text(f"#URL {page.url}\n" + page.inner_text("body")[:30000], encoding="utf-8")
        page.screenshot(path=str(out / f"{name}.png"))
    except Exception as e:  # noqa: BLE001
        log(f"{url[:100]}: {str(e)[:150]}")
    finally:
        page.remove_listener("request", on_req)
        page.remove_listener("requestfinished", on_done)
        page.remove_listener("requestfailed", on_done)
    calls = finished_calls(reqs, done, 300000)
    (out / f"{name}.json").write_text(json.dumps({"url": url, "calls": calls}, indent=1), encoding="utf-8")
    log(f"{url[:100]}: {len(calls)} calls")
    for c in calls:
        log(f"  {c.get('status')} {c['method']} {c['url'][:150]} {c.get('type', '')[:30]}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(config.DEFAULT_PATH))
    ap.add_argument("--seller", required=True)
    ap.add_argument("--query", action="append", required=True)
    ap.add_argument("--out", default="out/suggest")
    ap.add_argument("--follow", action="store_true",
                    help="then take the first suggestion and search, keeping every call the site makes (to find its "
                         "price call) and where it ends up")
    ap.add_argument("--person", action="store_true", help="Google Chrome with a window (settings seller_probe.person)")
    args = ap.parse_args(argv)
    cfg = config.load(args.config)
    sp = cfg["seller_probe"]
    from .seller_probe import probe_specs
    # a booking engine (not a catalogued seller) can be probed page by page too: no home page, no search box
    spec = probe_specs(cfg, ["probe", "account", "official", "live", "blocked"]).get(args.seller, {})
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    from playwright.sync_api import sync_playwright
    from .booking_session import launch_browser
    with sync_playwright() as pw:
        if args.person:
            from .seller_probe import open_browser
            br, ctx = open_browser(pw, None, sp["person"], sp)
            page = ctx.new_page()
        else:
            br = launch_browser(pw, True)
            page = br.new_context(user_agent=sp["user_agent"], locale="en-GB", viewport={"width": 1366, "height": 900}).new_page()
        warmed = False
        for q in args.query:
            if q.startswith("http"):        # a page of the seller's (a hotel page for a night): every call it makes
                home = spec.get("home") or (spec.get("browser") or {}).get("warm_up")
                if not warmed and home:                # its home page first, as a visitor arrives (HolidayCheck's
                    warmed = True                      # offers page opened cold: "Diese Funktionalität steht gerade
                    try:                               # nicht zur Verfügung", 2026-11-24)
                        page.goto(home, wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(6000)
                        accept_cookies(page, sp["cookie_buttons"])
                        page.wait_for_timeout(3000)
                    except Exception as e:  # noqa: BLE001
                        log(f"home: {str(e)[:120]}")
                open_page_calls(page, q, out, args.seller, sp["cookie_buttons"])
                continue
            page.goto(spec["home"], wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)
            accept_cookies(page, sp["cookie_buttons"])
            reqs, done = [], set()
            on_req, on_done = (lambda r: reqs.append(r)), (lambda r: done.add(id(r)))
            page.on("request", on_req)
            page.on("requestfinished", on_done)
            page.on("requestfailed", on_done)
            page.screenshot(path=str(out / f"{args.seller}_{slug(q)}_before.png"))
            inputs = page.evaluate("""() => Array.from(document.querySelectorAll('input, [contenteditable=true], [role=combobox], [role=searchbox]'))
                .filter(e => e.offsetParent !== null).slice(0, 15)
                .map(e => ({tag: e.tagName, attrs: Array.from(e.attributes).map(a => a.name + '=' + a.value.slice(0, 60))}))""")
            (out / f"{args.seller}_{slug(q)}_inputs.json").write_text(json.dumps(inputs, indent=1), encoding="utf-8")
            for t in spec.get("pre_click") or []:      # e.g. HolidayCheck's "Nur Hotel" tab before its search box
                try:
                    page.get_by_text(t, exact=True).first.click(timeout=4000)
                    page.wait_for_timeout(1000)
                except Exception:  # noqa: BLE001
                    log(f"no {t!r} to click")
            box = page.locator(spec["input"] + " >> visible=true").first
            if not box.count():
                log(f"{q}: no search box")
                continue
            try:
                box.click(timeout=8000)
            except Exception:  # noqa: BLE001
                box.focus()
            page.keyboard.type(q, delay=120)
            page.wait_for_timeout(4000)
            word = q.split()[-2 if len(q.split()) > 1 else 0].lower()
            dom = page.evaluate(DOM_JS, word)
            if args.follow:                 # take the first suggestion, search, and wait for the results
                sug = page.locator(spec.get("suggestion", "[role='option']") + " >> visible=true")
                try:
                    if sug.count():
                        sug.first.click(timeout=5000)
                    else:
                        page.keyboard.press("ArrowDown")
                        page.keyboard.press("Enter")
                except Exception:  # noqa: BLE001
                    page.keyboard.press("Enter")
                page.wait_for_timeout(2500)
                btn = page.locator((spec.get("search") or "button[type='submit']") + " >> visible=true")
                try:
                    if btn.count():
                        with page.context.expect_page(timeout=4000) as new:
                            btn.first.click(timeout=5000)
                        np = new.value
                        np.wait_for_load_state("domcontentloaded")
                        page.goto(np.url, wait_until="domcontentloaded")
                        np.close()
                except Exception:  # noqa: BLE001
                    pass
                for _ in range(6):
                    page.wait_for_timeout(2500)
                    try:
                        page.mouse.wheel(0, 500)
                    except Exception:  # noqa: BLE001
                        pass
                (out / f"{args.seller}_{slug(q)}_after.txt").write_text(f"#URL {page.url}\n" + page.inner_text("body")[:30000],
                                                                        encoding="utf-8")
            calls = finished_calls(reqs, done, 300000 if args.follow else 20000)
            page.remove_listener("request", on_req)
            page.remove_listener("requestfinished", on_done)
            page.remove_listener("requestfailed", on_done)
            (out / f"{args.seller}_{slug(q)}.json").write_text(json.dumps({"query": q, "calls": calls, "dom": dom}, indent=1),
                                                             encoding="utf-8")
            page.screenshot(path=str(out / f"{args.seller}_{slug(q)}.png"))
            log(f"{q}: {len(calls)} calls while typing; {len(dom)} suggestion-like elements")
            for c in calls:
                log(f"  {c.get('status')} {c['method']} {c['url'][:150]} {c.get('type', '')[:30]}")
        br.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
