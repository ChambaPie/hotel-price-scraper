"""Clock: starts each collection workflow when it is due (settings: clock).

GitHub's own schedules reach this repository hours late or not at all (2026-11-24: the hourly Booking.com cron ran
twice in 22 hours, "every 4 hours" sellers once, the daily catalog 8-10 hours late every day since 11-17). The clock
workflow ticks every few minutes and starts whatever is due by workflow_dispatch, which GitHub runs at once.

    GITHUB_TOKEN=... GITHUB_REPOSITORY=owner/repo python -m hotel_scraper.clock            # one tick
    GITHUB_TOKEN=... GITHUB_REPOSITORY=owner/repo python -m hotel_scraper.clock --successor  # start the next clock
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

from . import config


def due(last: dict | None, every_hours: float, now: datetime, slack_minutes: float) -> bool:
    """Whether a workflow whose latest run is `last` (GitHub's run object, or None) should start now: not while a run
    is queued or running, and not before `every_hours` (less `slack_minutes`) since the latest run began."""
    if last is None:
        return True
    if last.get("status") in ("queued", "in_progress", "waiting", "pending", "requested"):
        return False
    began = datetime.fromisoformat((last.get("run_started_at") or last["created_at"]).replace("Z", "+00:00"))
    return (now - began).total_seconds() >= every_hours * 3600 - slack_minutes * 60


def tick(api, workflows: dict, now: datetime, slack_minutes: float, ref: str = "main", log=print) -> list[str]:
    """Start every due workflow; returns the ones started. api(method, path, body=None) -> parsed JSON."""
    started = []
    for wf, spec in workflows.items():
        hours, inputs = spec["every_hours"], spec.get("inputs") or {}
        runs = api("GET", f"actions/workflows/{wf}/runs?per_page=1").get("workflow_runs", [])
        last = runs[0] if runs else None
        if due(last, hours, now, slack_minutes):
            api("POST", f"actions/workflows/{wf}/dispatches",
                {"ref": ref, **({"inputs": {k: str(v) for k, v in inputs.items()}} if inputs else {})})
            started.append(wf)
            log(f"started {wf} (every {hours} h; last {last and last.get('created_at')})")
    return started


def successor_needed(api, wf: str, own_run_id: str | None) -> bool:
    """No other clock run is waiting to take over (a pending one, or one started by the backup schedule)."""
    runs = api("GET", f"actions/workflows/{wf}/runs?per_page=5").get("workflow_runs", [])
    return not any(r.get("status") in ("queued", "waiting", "pending", "requested") and str(r.get("id")) != str(own_run_id)
                   for r in runs)


def github_api(repo: str, token: str):
    def api(method: str, path: str, body=None):
        req = urllib.request.Request(f"https://api.github.com/repos/{repo}/{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    return api


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--successor", action="store_true", help="start the next clock run unless one is waiting")
    args = ap.parse_args(argv)
    cc = config.load()["clock"]
    api = github_api(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"])
    if args.successor:
        if successor_needed(api, cc["workflow"], os.environ.get("GITHUB_RUN_ID")):
            api("POST", f"actions/workflows/{cc['workflow']}/dispatches", {"ref": cc.get("ref", "main")})
            print("started the next clock")
        return 0
    started = tick(api, cc["workflows"], datetime.now(timezone.utc), cc["slack_minutes"], cc.get("ref", "main"))
    print(f"{datetime.now(timezone.utc):%H:%M} tick: {', '.join(started) or 'nothing due'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
