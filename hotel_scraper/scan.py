"""Phase 4: the 12-month scanner.

Each run checks the watched properties that were checked longest ago (never-checked first), as many as the run
budget allows (settings: scan). One Xotelo heatmap call per property returns every night from today to about
10.5 months ahead, each classed cheap / average / high. The latest classes are kept per property in
paths.heatmap; every change of a night's class (e.g. high -> cheap) is appended to paths.heatmap_changes, which is
what detection (Phase 5) works from.

    python -m hotel_scraper.scan
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import catalog as cat
from . import config
from .xotelo import Xotelo

CODES = {"cheap_price_days": "c", "average_price_days": "a", "high_price_days": "h"}
HEAT_FIELDS = ["pid", "checked_at", "first_date", "classes", "status", "error", "failures"]
CHANGE_FIELDS = ["pid", "night", "was", "now", "seen_at"]


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%H:%M:%S} {msg}", flush=True)


def encode(heatmap: dict, start: date, end: date) -> str:
    """One character per night from start to end: c / a / h, or '.' when TripAdvisor has no class for it."""
    by_day = {d: CODES[k] for k, days in (heatmap or {}).items() if k in CODES for d in (days or [])}
    n = (end - start).days + 1
    return "".join(by_day.get((start + timedelta(days=i)).isoformat(), ".") for i in range(max(n, 0)))


def nights(first: str, classes: str) -> dict[str, str]:
    d0 = date.fromisoformat(first)
    return {(d0 + timedelta(days=i)).isoformat(): ch for i, ch in enumerate(classes) if ch != "."}


def diff(old: dict | None, first: str, classes: str, pid: str, seen_at: str) -> list[dict]:
    """Nights whose class changed since the previous check (nights missing on either side are ignored)."""
    if not old:
        return []
    before, after = nights(old["first_date"], old["classes"]), nights(first, classes)
    return [{"pid": pid, "night": n, "was": before[n], "now": c, "seen_at": seen_at}
            for n, c in sorted(after.items()) if n in before and before[n] != c]


def read_csv(path) -> list[dict]:
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


def write_csv(rows: list[dict], path, fields: list[str]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fields, lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


@dataclass
class ScanResult:
    checked: int = 0
    failed: int = 0
    no_data: int = 0
    changes: list[dict] = field(default_factory=list)
    to_cheap: int = 0
    due: int = 0
    watched: int = 0
    errors: dict = field(default_factory=dict)
    failed_kinds: dict = field(default_factory=dict)
    seconds: float = 0.0


def run(cfg: dict, x: Xotelo, today: date, log=log, clock=time.monotonic) -> ScanResult:
    s, p = cfg["scan"], cfg["paths"]
    started = clock()
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    watched = [r for r in cat.read(p["watchlist"]) if r["pid"].startswith("ta:")]
    heat = {r["pid"]: r for r in read_csv(p["heatmap"])}
    order = sorted(watched, key=lambda r: heat.get(r["pid"], {}).get("checked_at", ""))  # never checked first
    end = today + timedelta(days=s["horizon_days"] - 1)
    chk_out = (today + timedelta(days=s["horizon_days"])).isoformat()
    res = ScanResult(watched=len(watched), due=min(len(order), s["properties_per_run"]))

    for r in order[: s["properties_per_run"]]:
        if clock() - started > s["run_budget_minutes"] * 60 or x.requests >= s["max_requests_per_run"]:
            log("run budget reached; the rest are checked next run")
            break
        key = r["pid"][3:]
        out = x.heatmap(key, chk_out)
        if not out.ok:
            res.failed += 1
            res.errors[out.error[:120]] = res.errors.get(out.error[:120], 0) + 1
            res.failed_kinds[r.get("kind", "?")] = res.failed_kinds.get(r.get("kind", "?"), 0) + 1
            log(f"heatmap failed {r['pid']} ({r.get('kind')}, {r.get('name', '')[:40]}): {out.error[:160]}")
            prev = heat.get(r["pid"], {})
            heat[r["pid"]] = {**prev, "pid": r["pid"], "checked_at": now_iso, "status": "error", "error": out.error[:160],
                              "failures": int(prev.get("failures") or 0) + 1}  # rotation moves on; retried next cycle
            continue
        classes = encode(out.data.get("heatmap") or {}, today, end)
        if set(classes) <= {"."}:
            res.no_data += 1
        changes = diff(heat.get(r["pid"]) if heat.get(r["pid"], {}).get("classes") else None,
                       today.isoformat(), classes, r["pid"], now_iso)
        res.changes.extend(changes)
        heat[r["pid"]] = {"pid": r["pid"], "checked_at": now_iso, "first_date": today.isoformat(), "classes": classes,
                          "status": "ok", "error": "", "failures": 0}
        res.checked += 1
    res.to_cheap = sum(1 for c in res.changes if c["now"] == "c")
    res.seconds = round(clock() - started)

    keep = {r["pid"] for r in watched}
    write_csv(sorted((v for k, v in heat.items() if k in keep), key=lambda v: v["pid"]), p["heatmap"], HEAT_FIELDS)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=s["change_log_days"])).isoformat(timespec="seconds")
    old = [c for c in read_csv(p["heatmap_changes"]) if c["seen_at"] >= cutoff]
    write_csv(old + res.changes, p["heatmap_changes"], CHANGE_FIELDS)
    return res


def summary_md(res: ScanResult, cfg: dict) -> str:
    lines = [f"## Scan: {res.checked} of {res.watched} watched properties checked ({res.seconds} s)", "",
             f"Failed: {res.failed}; no data: {res.no_data}; nights whose class changed: {len(res.changes)}, "
             f"of which turned cheap: {res.to_cheap}.", ""]
    if res.errors:
        lines += ["Errors: " + "; ".join(f"{k} ×{v}" for k, v in res.errors.items()),
                  "Failures by kind: " + ", ".join(f"{k}: {v}" for k, v in res.failed_kinds.items()), ""]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(config.DEFAULT_PATH))
    args = ap.parse_args(argv)
    cfg = config.load(args.config)
    t, s = cfg["tripadvisor"], cfg["scan"]
    x = Xotelo(t["base_url"], user_agent=t["user_agent"], pause_seconds=tuple(s["pause_seconds"]))
    res = run(cfg, x, date.today())
    out = Path(cfg["paths"]["out_dir"])
    out.mkdir(parents=True, exist_ok=True)
    md = summary_md(res, cfg)
    (out / "scan_summary.md").write_text(md, encoding="utf-8")
    print(md, flush=True)
    return 0 if res.checked or not res.due else 1


if __name__ == "__main__":
    sys.exit(main())
