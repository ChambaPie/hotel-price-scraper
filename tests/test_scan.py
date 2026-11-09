import csv
import json
from datetime import date, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from hotel_scraper import config, scan
from hotel_scraper.http import Response
from hotel_scraper.xotelo import Xotelo

TODAY = date(2026, 11, 8)


def d(i):
    return (TODAY + timedelta(days=i)).isoformat()


class FakeX:
    def __init__(self, maps, fail=()):
        self.maps, self.fail, self.calls = maps, set(fail), []

    def get(self, url, headers=None):
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        self.calls.append(q["hotel_key"])
        if q["hotel_key"] in self.fail:
            return Response(503, {}, b"", 0.1, url, "HTTP 503")
        body = {"error": None, "result": {"heatmap": self.maps.get(q["hotel_key"], {})}}
        return Response(200, {}, json.dumps(body).encode(), 0.1, url)


@pytest.fixture
def cfg(tmp_path):
    c = config.load()
    c["paths"].update(watchlist=str(tmp_path / "watchlist.csv"), heatmap=str(tmp_path / "heatmap.csv"),
                      heatmap_changes=str(tmp_path / "changes.csv"))
    c["scan"].update(horizon_days=5, properties_per_run=2, pause_seconds=[0, 0])
    with open(c["paths"]["watchlist"], "w", newline="") as f:
        w = csv.DictWriter(f, ["pid", "name"]); w.writeheader()
        w.writerows([{"pid": f"ta:g1-d{i}", "name": f"H{i}"} for i in range(3)])
    return c


def go(cfg, fake, today=TODAY):
    return scan.run(cfg, Xotelo("https://x", fake, pause_seconds=(0, 0), sleep=lambda s: None), today, log=lambda m: None)


def test_encode_and_nights():
    hm = {"cheap_price_days": [d(1)], "average_price_days": [d(0)], "high_price_days": [d(3)]}
    assert scan.encode(hm, TODAY, TODAY + timedelta(days=4)) == "ac.h."
    assert scan.nights(TODAY.isoformat(), "ac.h.") == {d(0): "a", d(1): "c", d(3): "h"}


def test_rotation_and_changes(cfg):
    m1 = {"high_price_days": [d(1), d(2)], "average_price_days": [d(3)]}
    fake = FakeX({"g1-d0": m1, "g1-d1": m1, "g1-d2": m1})
    r1 = go(cfg, fake)
    assert r1.checked == 2 and fake.calls == ["g1-d0", "g1-d1"] and not r1.changes      # first check: no changes
    fake2 = FakeX({"g1-d0": m1, "g1-d1": m1, "g1-d2": m1})
    go(cfg, fake2)
    assert fake2.calls[0] == "g1-d2"                                                   # never-checked goes first
    m2 = {"cheap_price_days": [d(2)], "high_price_days": [d(1)], "average_price_days": [d(3)]}
    fake3 = FakeX({"g1-d0": m2, "g1-d1": m2, "g1-d2": m2})
    r3 = go(cfg, fake3, today=TODAY + timedelta(days=1))                               # a day later: d(0) dropped off
    assert all(c["night"] == d(2) and c["was"] == "h" and c["now"] == "c" for c in r3.changes) and r3.to_cheap == 2
    rows = list(csv.DictReader(open(cfg["paths"]["heatmap_changes"])))
    assert len(rows) == 2 and {r["pid"] for r in rows} <= {"ta:g1-d0", "ta:g1-d1", "ta:g1-d2"}


def test_failures_and_budget(cfg):
    cfg["scan"]["max_requests_per_run"] = 1
    fake = FakeX({}, fail={"g1-d0"})
    r = go(cfg, fake)
    assert r.failed == 1 and r.checked == 0 and len(fake.calls) == 1
    rows = list(csv.DictReader(open(cfg["paths"]["heatmap"])))
    assert rows[0]["status"] == "error" and rows[0]["failures"] == "1" and rows[0]["classes"] == ""
    cfg["scan"]["max_requests_per_run"] = 10
    fake2 = FakeX({})
    go(cfg, fake2)
    assert fake2.calls[0] != "g1-d0"                                   # failed one waits for the next cycle
