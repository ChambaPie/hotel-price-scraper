"""Clock (hotel_scraper/clock.py): GitHub's schedules reached this repository hours late or not at all (2026-11-24:
the hourly Booking.com cron ran twice in 22 hours), so a clock workflow starts what is due."""
from datetime import datetime, timezone

from hotel_scraper import clock, config

NOW = datetime(2026, 11, 24, 16, 0, tzinfo=timezone.utc)


def run(created, status="completed", started=None):
    return {"id": 1, "status": status, "created_at": created, "run_started_at": started or created}


class FakeApi:
    def __init__(self, runs):
        self.runs, self.posts = runs, []

    def __call__(self, method, path, body=None):
        if method == "POST":
            self.posts.append((path, body))
            return {}
        wf = path.split("/")[2]
        return {"workflow_runs": self.runs.get(wf, [])}


def test_due_after_its_interval_and_never_while_running():
    assert clock.due(None, 4, NOW, 10)
    assert clock.due(run("2026-11-24T12:05:00Z"), 4, NOW, 10)              # 3 h 55: within the slack
    assert not clock.due(run("2026-11-24T12:30:00Z"), 4, NOW, 10)
    assert not clock.due(run("2026-11-24T05:00:00Z", "in_progress"), 4, NOW, 10)
    assert not clock.due(run("2026-11-24T05:00:00Z", "queued"), 4, NOW, 10)


def test_tick_starts_only_what_is_due_with_its_inputs():
    api = FakeApi({"booking.yml": [run("2026-11-24T14:50:00Z")],            # 70 minutes ago, hourly: due
                   "sellers.yml": [run("2026-11-24T13:07:00Z")],            # not yet 4 hours
                   "vendors.yml": []})                                      # never ran
    wfs = config.load()["clock"]["workflows"]
    started = clock.tick(api, wfs, NOW, 10, log=lambda m: None)
    assert started == ["booking.yml", "vendors.yml"]
    # Booking.com's own due check decides (a dispatch forces a pass by default)
    assert api.posts[0] == ("actions/workflows/booking.yml/dispatches", {"ref": "main", "inputs": {"force": "false"}})
    assert api.posts[1] == ("actions/workflows/vendors.yml/dispatches", {"ref": "main"})


def test_successor_only_when_no_clock_is_waiting():
    waiting = FakeApi({"clock.yml": [run("2026-11-24T15:59:00Z", "pending") | {"id": 7},
                                     run("2026-11-24T10:00:00Z", "in_progress") | {"id": 5}]})
    assert not clock.successor_needed(waiting, "clock.yml", "5")
    alone = FakeApi({"clock.yml": [run("2026-11-24T10:00:00Z", "in_progress") | {"id": 5}]})
    assert clock.successor_needed(alone, "clock.yml", "5")


def test_every_workflow_the_clock_starts_can_be_started_by_hand():
    from pathlib import Path
    for wf in config.load()["clock"]["workflows"]:
        text = (Path(__file__).parents[1] / ".github" / "workflows" / wf).read_text()
        assert "workflow_dispatch:" in text and "schedule:" not in text      # one starter only: no double runs
