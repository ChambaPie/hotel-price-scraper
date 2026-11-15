"""Phase 8: health check. Flags workflows whose last successful run is too old (settings: ops).

    GITHUB_TOKEN=... GITHUB_REPOSITORY=owner/repo python -m hotel_scraper.health
Writes <out_dir>/health.md; writes <out_dir>/health_issue.md only when something is wrong.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import config


def problems(last_success: dict, now: datetime, limits: dict, ever_ran: dict | None = None) -> list[str]:
    """last_success: workflow file -> ISO time of its last successful run (or None). ever_ran: workflow file -> whether
    it has any run at all; a workflow whose first scheduled run hasn't come yet (e.g. the weekly one) is not a problem
    (2026-11-15: 29 repeated comments about osm.yml before its first Monday)."""
    out = []
    for wf, max_h in limits.items():
        t = last_success.get(wf)
        if not t:
            if ever_ran is None or ever_ran.get(wf, True):
                out.append(f"`{wf}` has no successful run yet")
            continue
        age = (now - datetime.fromisoformat(t.replace("Z", "+00:00"))).total_seconds() / 3600
        if age > max_h:
            out.append(f"`{wf}` last succeeded {age:.0f} h ago (limit {max_h} h)")
    return out


def _runs(repo: str, token: str, wf: str, query: str) -> list[dict]:
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/actions/workflows/{wf}/runs?{query}",
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r).get("workflow_runs", [])


def last_successes(repo: str, token: str, workflows: list[str]) -> tuple[dict, dict]:
    """({workflow: time of last successful run or None}, {workflow: has any run at all})."""
    res, ran = {}, {}
    for wf in workflows:
        ok = _runs(repo, token, wf, "status=success&per_page=1")
        res[wf] = ok[0]["updated_at"] if ok else None
        ran[wf] = bool(ok) or bool(_runs(repo, token, wf, "per_page=1"))
    return res, ran


def main(argv=None) -> int:
    cfg = config.load()
    limits = cfg["ops"]["max_hours_since_success"]
    now = datetime.now(timezone.utc)
    last, ran = last_successes(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"], list(limits))
    found = problems(last, now, limits, ran)
    out = Path(cfg["paths"]["out_dir"])
    out.mkdir(parents=True, exist_ok=True)
    lines = ["## Health check", ""] + [f"- `{wf}`: last success {last.get(wf) or ('never' if ran.get(wf) else 'not run yet')}"
                                       for wf in limits]
    (out / "health.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / "health_issue.md").unlink(missing_ok=True)
    if found:
        (out / "health_issue.md").write_text("Problems found by the daily health check:\n\n" +
                                            "\n".join(f"- {p}" for p in found) + "\n", encoding="utf-8")
    print("\n".join(lines + (["", "PROBLEMS:"] + found if found else ["", "All good."])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
