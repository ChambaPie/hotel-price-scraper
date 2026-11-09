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


def problems(last_success: dict, now: datetime, limits: dict) -> list[str]:
    """last_success: workflow file -> ISO time of its last successful run (or None)."""
    out = []
    for wf, max_h in limits.items():
        t = last_success.get(wf)
        if not t:
            out.append(f"`{wf}` has no successful run yet")
            continue
        age = (now - datetime.fromisoformat(t.replace("Z", "+00:00"))).total_seconds() / 3600
        if age > max_h:
            out.append(f"`{wf}` last succeeded {age:.0f} h ago (limit {max_h} h)")
    return out


def last_successes(repo: str, token: str, workflows: list[str]) -> dict:
    res = {}
    for wf in workflows:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/actions/workflows/{wf}/runs?status=success&per_page=1",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            runs = json.load(r).get("workflow_runs", [])
        res[wf] = runs[0]["updated_at"] if runs else None
    return res


def main(argv=None) -> int:
    cfg = config.load()
    limits = cfg["ops"]["max_hours_since_success"]
    now = datetime.now(timezone.utc)
    last = last_successes(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"], list(limits))
    found = problems(last, now, limits)
    out = Path(cfg["paths"]["out_dir"])
    out.mkdir(parents=True, exist_ok=True)
    lines = ["## Health check", ""] + [f"- `{wf}`: last success {last.get(wf) or 'never'}" for wf in limits]
    (out / "health.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / "health_issue.md").unlink(missing_ok=True)
    if found:
        (out / "health_issue.md").write_text("Problems found by the daily health check:\n\n" +
                                            "\n".join(f"- {p}" for p in found) + "\n", encoding="utf-8")
    print("\n".join(lines + (["", "PROBLEMS:"] + found if found else ["", "All good."])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
