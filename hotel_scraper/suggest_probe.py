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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(config.DEFAULT_PATH))
    ap.add_argument("--seller", required=True)
    ap.add_argument("--query", action="append", required=True)
    ap.add_argument("--out", default="out/suggest")
    args = ap.parse_args(argv)
    cfg = config.load(args.config)
    sp = cfg["seller_probe"]
    from .seller_probe import probe_specs
    spec = probe_specs(cfg, ["probe", "account", "official", "live"])[args.seller]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    from playwright.sync_api import sync_playwright
    from .booking_session import launch_browser
    with sync_playwright() as pw:
        br = launch_browser(pw, True)
        page = br.new_context(user_agent=sp["user_agent"], locale="en-GB", viewport={"width": 1366, "height": 900}).new_page()
        for q in args.query:
            page.goto(spec["home"], wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)
            accept_cookies(page, sp["cookie_buttons"])
            reqs = []
            page.on("request", lambda r: reqs.append(r))
            page.screenshot(path=str(out / f"{args.seller}_{slug(q)}_before.png"))
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
            calls = []
            for r in reqs:
                if r.resource_type not in ("xhr", "fetch"):
                    continue
                item = {"method": r.method, "url": r.url[:400], "post": (r.post_data or "")[:3000]}
                try:
                    resp = r.response()
                    if resp:
                        item["status"] = resp.status
                        item["type"] = resp.headers.get("content-type", "")
                        body = resp.text()
                        item["body"] = body[:20000]
                except Exception as e:  # noqa: BLE001
                    item["error"] = str(e)[:100]
                calls.append(item)
            page.remove_listener("request", lambda r: reqs.append(r))
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
