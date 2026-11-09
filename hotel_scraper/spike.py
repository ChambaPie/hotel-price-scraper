"""Phase 2 source spike. Measures which price sources answer from where, how fast, and with what data.

    python -m hotel_scraper.spike                    # on Actions or locally
    python -m hotel_scraper.spike --label home       # e.g. from your laptop, to compare with GitHub's servers

Writes <out_dir>/spike/report.md, results.json and raw/ (response samples for writing parsers later).
It never fails because a source is blocked: measuring that is the point.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlencode

import yaml

from . import catalog as cat
from . import config, geo
from .discover_osm import _norm_name
from .http import Response, Session

CALENDAR_QUERY = (
    "query AvailabilityCalendar($input: AvailabilityCalendarQueryInput!) {\n"
    "  availabilityCalendar(input: $input) {\n"
    "    ... on AvailabilityCalendarQueryResult {\n      hotelId\n      days {\n        available\n"
    "        avgPriceFormatted\n        checkin\n        minLengthOfStay\n        __typename\n      }\n"
    "      __typename\n    }\n"
    "    ... on AvailabilityCalendarQueryError {\n      message\n      __typename\n    }\n"
    "    __typename\n  }\n}\n"
)
WAF_MARKERS = ("gokuProps", "challenge.js", "awsWafCookieDomainList", "AwsWafIntegration")
MEAL_TERMS = {"breakfast_included": r"breakfast included", "half_board": r"half board",
              "full_board": r"full board", "all_inclusive": r"all[- ]inclusive"}


@dataclass
class Probe:
    source: str
    kind: str
    target: str
    status: int
    ok: bool
    blocked: str = ""
    elapsed: float = 0.0
    detail: dict = field(default_factory=dict)


# ---------- pure helpers (unit-tested) ----------

def block_reason(resp: Response) -> str:
    """Why a response is not usable data: '' if it looks fine."""
    if resp.status == 0:
        return f"no answer: {resp.error}"
    waf = resp.header("x-amzn-waf-action")
    if waf:
        return f"AWS WAF {waf}"
    head = resp.body[:200_000].decode("utf-8", "replace")
    if any(m in head for m in WAF_MARKERS):
        return f"bot challenge page (HTTP {resp.status})"
    if resp.status == 202 and len(resp.body) < 5000:
        return "HTTP 202 with empty body (challenge)"
    if resp.status in (401, 403, 405, 429) or resp.status >= 500:
        return f"HTTP {resp.status}"
    return ""


def booking_page_markers(html: str) -> dict:
    find = lambda pat: (re.search(pat, html) or [None, None])[1]  # noqa: E731
    low = html.lower()
    return {
        "csrf_token": bool(find(r"b_csrf_token:\s*'(.+?)'")),
        "hotel_name": find(r'hotelName:\s*"(.+?)"') or "",
        "hotel_country": find(r'hotelCountry:\s*"(.+?)"') or "",
        "room_blocks": len(re.findall(r"data-block-id", html)),
        "meal_mentions": {k: len(re.findall(p, low)) for k, p in MEAL_TERMS.items()},
        "bytes": len(html),
    }


def calendar_payload(country: str, pagename: str, start: date, days: int, adults: int, rooms: int) -> dict:
    return {"operationName": "AvailabilityCalendar",
            "variables": {"input": {"travelPurpose": 2,
                                    "pagenameDetails": {"countryCode": country, "pagename": pagename},
                                    "searchConfig": {"searchConfigDate": {"startDate": start.isoformat(), "amountOfDays": days},
                                                     "nbAdults": adults, "nbRooms": rooms}}},
            "extensions": {}, "query": CALENDAR_QUERY}


def summarize_calendar(data: dict) -> dict:
    cal = ((data or {}).get("data") or {}).get("availabilityCalendar") or {}
    if "message" in cal or not cal.get("days"):
        return {"error": cal.get("message") or str((data or {}).get("errors", "no days"))[:300]}
    days = cal["days"]
    priced = [d for d in days if d.get("avgPriceFormatted")]
    return {"days": len(days), "available": sum(1 for d in days if d.get("available")), "priced": len(priced),
            "first": days[0].get("checkin"), "last": days[-1].get("checkin"),
            "min_stay_over_1": sum(1 for d in days if (d.get("minLengthOfStay") or 1) > 1),
            "sample": priced[:3]}


def summarize_xotelo(kind: str, data: dict) -> dict:
    if data.get("error"):
        return {"error": str(data["error"])[:300]}
    res = data.get("result") or {}
    if kind == "list":
        items = res.get("list") or []
        return {"total_count": res.get("total_count"), "returned": len(items),
                "types": sorted({i.get("accommodation_type", "") for i in items}),
                "with_geo": sum(1 for i in items if (i.get("geo") or {}).get("latitude")),
                "sample": [{k: i.get(k) for k in ("name", "key", "accommodation_type", "review_summary")} for i in items[:3]]}
    if kind == "rates":
        rates = res.get("rates") or []
        vals = [r["rate"] for r in rates if isinstance(r.get("rate"), (int, float))]
        return {"sellers": len(rates), "names": sorted(r.get("name", "") for r in rates),
                "min": min(vals) if vals else None, "max": max(vals) if vals else None}
    if kind == "heatmap":
        hm = res.get("heatmap") or {}
        all_days = sorted(d for v in hm.values() for d in (v or []))
        return {"buckets": {k: len(v or []) for k, v in hm.items()}, "days": len(all_days),
                "first": all_days[0] if all_days else None, "last": all_days[-1] if all_days else None}
    if kind == "search":
        items = res.get("list") or []
        return {"returned": len(items), "sample": [{k: i.get(k) for k in ("name", "hotel_key", "location_key")} for i in items[:3]]}
    return {}


def match_to_catalog(items: list[dict], rows: list[dict], max_km: float, min_sim: float) -> dict:
    """How many Xotelo list hotels map to a catalog property by position + name."""
    pts = [(float(r["lat"]), float(r["lon"]), _norm_name(r["name"]), r["pid"]) for r in rows if r.get("lat")]
    matched, examples = 0, []
    for it in items:
        g = it.get("geo") or {}
        if not g.get("latitude"):
            continue
        name = _norm_name(it.get("name", ""))
        best = None
        for lat, lon, n, pid in pts:
            if geo.haversine_km(g["latitude"], g["longitude"], lat, lon) <= max_km:
                sim = SequenceMatcher(None, name, n).ratio()
                if sim >= min_sim and (best is None or sim > best[0]):
                    best = (sim, pid)
        if best:
            matched += 1
            if len(examples) < 3:
                examples.append({"xotelo": it.get("name"), "pid": best[1], "similarity": round(best[0], 2)})
    return {"xotelo_hotels": len(items), "matched": matched, "examples": examples}


# ---------- probes ----------

class Spike:
    def __init__(self, cfg: dict, scfg: dict, out: Path, today: date, sleep=time.sleep):
        self.cfg, self.scfg, self.out, self.today, self.sleep = cfg, scfg, out, today, sleep
        self.probes: list[Probe] = []
        self.extra: dict = {}
        (out / "raw").mkdir(parents=True, exist_ok=True)

    def _pause(self):
        self.sleep(random.uniform(*self.scfg["pause_seconds"]))

    def _raw(self, name: str, resp: Response):
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[:120]
        ext = "json" if resp.body[:1] in (b"{", b"[") else "html"
        (self.out / "raw" / f"{safe}.{ext}").write_bytes(resp.body[: self.scfg["raw_max_bytes"]])

    def _record(self, source, kind, target, resp, detail=None, usable=None) -> Probe:
        reason = block_reason(resp)
        p = Probe(source, kind, target, resp.status, ok=(usable if usable is not None else not reason), blocked=reason,
                  elapsed=round(resp.elapsed, 2), detail=detail or {})
        self.probes.append(p)
        self._raw(f"{source}_{kind}_{target}", resp)
        return p

    # Xotelo
    def xotelo(self, session: Session) -> list[dict]:
        x, s = self.scfg["xotelo"], self.cfg["scan"]
        listed: list[dict] = []

        def call(kind, params, target):
            resp = session.get(f"{x['base_url']}/{kind}?{urlencode(params)}")
            detail, usable = {}, False
            if not block_reason(resp):
                try:
                    data = resp.json()
                    detail = summarize_xotelo(kind, data)
                    usable = "error" not in detail
                    if kind == "list" and usable:
                        listed.extend((data.get("result") or {}).get("list") or [])
                except ValueError:
                    detail = {"error": "not JSON"}
            self._record("xotelo", kind, target, resp, detail, usable)
            self._pause()

        for lk in x["location_keys"]:
            call("list", {"location_key": lk, "limit": 30, "offset": 0, "sort": "best_value"}, lk)
        hotels = [h for h in listed if h.get("key")][: x["hotels_from_list"]]
        for h in hotels:
            for off in x["rates_offsets_days"]:
                ci = self.today + timedelta(days=off)
                call("rates", {"hotel_key": h["key"], "chk_in": ci.isoformat(), "chk_out": (ci + timedelta(days=1)).isoformat(),
                               "currency": s["currency"], "adults": s["adults"], "rooms": s["rooms"]}, f"{h['key']}+{off}d")
        for h in hotels[: x["heatmap_hotels"]]:
            for off in x["heatmap_checkout_offsets_days"]:
                call("heatmap", {"hotel_key": h["key"], "chk_out": (self.today + timedelta(days=off)).isoformat()},
                     f"{h['key']}+{off}d")
        for q in x.get("search") or []:
            call("search", q, q["query"])
        return listed

    # Booking.com
    def booking(self, session: Session):
        b, s = self.scfg["booking"], self.cfg["scan"]
        ci = self.today + timedelta(days=b["checkin_offset_days"])
        for page in b["pages"]:
            target = f"{page['country']}/{page['pagename']}"
            params = urlencode({"checkin": ci.isoformat(), "checkout": (ci + timedelta(days=1)).isoformat(),
                                "group_adults": s["adults"], "no_rooms": s["rooms"], "group_children": 0,
                                "selected_currency": s["currency"], "lang": "en-gb"})
            url = f"{b['base_url']}/hotel/{page['country']}/{page['pagename']}.html?{params}"
            resp = session.get(url, headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                                             "Accept-Language": b["accept_language"]})
            markers = booking_page_markers(resp.text()) if resp.body else {}
            page_ok = not block_reason(resp) and markers.get("csrf_token")
            self._record("booking", "page", target, resp, markers, usable=bool(page_ok))
            self._pause()
            if not page_ok:
                continue
            token = re.search(r"b_csrf_token:\s*'(.+?)'", resp.text()).group(1)
            payload = calendar_payload(markers["hotel_country"] or page["country"], markers["hotel_name"] or page["pagename"],
                                       self.today, b["calendar_days"], s["adults"], s["rooms"])
            cresp = session.post_json(f"{b['base_url']}/dml/graphql?lang=en-gb", payload,
                                      headers={"x-booking-csrf-token": token, "Referer": url, "Origin": b["base_url"],
                                               "Accept": "*/*", "Accept-Language": b["accept_language"]})
            detail, usable = {}, False
            if not block_reason(cresp):
                try:
                    detail = summarize_calendar(cresp.json())
                    usable = "error" not in detail
                except ValueError:
                    detail = {"error": "not JSON"}
            self._record("booking", "calendar", target, cresp, detail, usable)
            self._pause()

    def run(self, xotelo_session=None, booking_session=None) -> None:
        x, b = self.scfg["xotelo"], self.scfg["booking"]
        listed = self.xotelo(xotelo_session or Session(x["user_agent"]))
        self.booking(booking_session or Session(b["user_agent"]))
        rows = [r for r in cat.read(self.cfg["paths"]["catalog"]) if not r.get("gone_since")]
        m = self.scfg["catalog_match"]
        self.extra["catalog_match"] = (match_to_catalog(listed, rows, m["max_km"], m["min_name_similarity"])
                                       if rows else {"skipped": "no data/catalog.csv yet"})


# ---------- report ----------

def report_md(label: str, probes: list[Probe], extra: dict, today: date) -> str:
    lines = [f"## Source spike: {label} ({today.isoformat()})", "",
             "| Source | Probe | Requests | Usable | Blocked / failed | Median s |", "|---|---|---|---|---|---|"]
    groups: dict[tuple, list[Probe]] = {}
    for p in probes:
        groups.setdefault((p.source, p.kind), []).append(p)
    for (src, kind), ps in groups.items():
        reasons = sorted({p.blocked or (p.detail.get("error") if isinstance(p.detail, dict) else "") or "" for p in ps if not p.ok} - {""})
        lines.append(f"| {src} | {kind} | {len(ps)} | {sum(p.ok for p in ps)} | {'; '.join(reasons)[:120] or '-'} "
                     f"| {statistics.median(p.elapsed for p in ps):.2f} |")
    lines += ["", "### Details", ""]
    for p in probes:
        d = json.dumps(p.detail, ensure_ascii=False)
        lines.append(f"- `{p.source}/{p.kind}` {p.target}: HTTP {p.status}, {'usable' if p.ok else 'NOT usable'}"
                     f"{' (' + p.blocked + ')' if p.blocked else ''}: {d[:400]}")
    lines += ["", "### Catalog matching (Xotelo list → data/catalog.csv)", "", f"`{json.dumps(extra.get('catalog_match', {}), ensure_ascii=False)}`", ""]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(config.DEFAULT_PATH))
    ap.add_argument("--label", default="")
    args = ap.parse_args(argv)
    cfg = config.load(args.config)
    scfg = yaml.safe_load(Path(cfg["paths"]["spike"]).read_text(encoding="utf-8"))
    label = args.label or scfg.get("label") or ("github-actions" if os.environ.get("GITHUB_ACTIONS") else "local")
    out = Path(cfg["paths"]["out_dir"]) / "spike"
    today = date.today()
    sp = Spike(cfg, scfg, out, today)
    sp.run()
    md = report_md(label, sp.probes, sp.extra, today)
    (out / "report.md").write_text(md, encoding="utf-8")
    (out / "results.json").write_text(json.dumps({"label": label, "date": today.isoformat(),
                                                  "probes": [asdict(p) for p in sp.probes], **sp.extra},
                                                 ensure_ascii=False, indent=1), encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
