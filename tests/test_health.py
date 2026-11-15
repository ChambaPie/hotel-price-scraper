from datetime import datetime, timezone

from hotel_scraper import health

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def test_problems():
    last = {"scan.yml": "2026-11-09T05:30:00Z", "catalog.yml": "2026-11-07T03:50:00Z", "osm.yml": None}
    p = health.problems(last, NOW, {"scan.yml": 14, "catalog.yml": 30, "osm.yml": 200})
    assert p == ["`catalog.yml` last succeeded 56 h ago (limit 30 h)", "`osm.yml` has no successful run yet"]


def test_workflow_not_run_yet_is_not_a_problem():
    # Regression (2026-11-15): the weekly job had not reached its first Monday and the health issue got 29 comments
    last = {"scan.yml": "2026-11-09T05:30:00Z", "osm.yml": None, "booking.yml": None}
    ran = {"scan.yml": True, "osm.yml": False, "booking.yml": True}
    p = health.problems(last, NOW, {"scan.yml": 14, "osm.yml": 200, "booking.yml": 14}, ran)
    assert p == ["`booking.yml` has no successful run yet"]        # ran and never succeeded: still flagged
