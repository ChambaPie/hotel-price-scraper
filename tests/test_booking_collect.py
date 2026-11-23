import csv
from datetime import date, timedelta

import pytest

from hotel_scraper import booking_collect as bc
from hotel_scraper import config

TODAY = date(2026, 11, 9)


def nights(prices):
    return {(TODAY + timedelta(days=i)).isoformat(): {"price": p, "available": True, "min_stay": 1} for i, p in prices.items()}


def test_best_match_and_clean_query():
    cards = [{"name": "DoubleTree By Hilton Trieste", "cc": "it", "slug": "doubletree-by-hilton-trieste"},
             {"name": "Hotel Continentale", "cc": "it", "slug": "hotelcontinentaletrieste"}]
    c, sim = bc.best_match("DoubleTree by Hilton Trieste", cards, 0.6)
    assert c["slug"] == "doubletree-by-hilton-trieste" and sim > 0.9
    assert bc.best_match("Some Other Place", cards, 0.6)[0] is None
    assert bc.clean_query("Hotel Seefischer - Finest Hideaway****s") == "Hotel Seefischer Finest Hideaway"


def test_encode_decode_round_trip():
    n = nights({0: 120.0, 2: 1100.0, 3: 98.5})
    n[(TODAY + timedelta(days=3)).isoformat()]["min_stay"] = 3
    enc = bc.encode_nights(n, TODAY, 5)
    assert enc == "120;;1100;98.5/3;"
    dec = bc.decode_nights(TODAY.isoformat(), enc)
    assert dec[(TODAY + timedelta(days=3)).isoformat()] == {"price": 98.5, "available": True, "min_stay": 3}
    assert (TODAY + timedelta(days=1)).isoformat() not in dec


class FakeSession:
    def __init__(self, cards, years):
        self.cards, self.years, self.token, self.calls, self.currency_seen = cards, years, "t", 0, "USD"

    def search(self, q, checkin, wait=20, scrolls=0, raw=False, nflt=""):
        self.searches = getattr(self, "searches", []) + [(q, nflt)]
        return self.cards.get(q.split(" ")[0], [])

    def year(self, cc, slug, today, windows):
        self.calls += windows
        return self.years[slug]


@pytest.fixture
def cfg(tmp_path):
    c = config.load()
    for k in ("watchlist", "booking_ids", "booking_calendars"):
        c["paths"][k] = str(tmp_path / f"{k}.csv")
    with open(c["paths"]["watchlist"], "w", newline="") as f:
        w = csv.DictWriter(f, ["pid", "name", "city"]); w.writeheader()
        w.writerow({"pid": "ta:g1-d1", "name": "Alpha Hotel", "city": "Bled"})
        w.writerow({"pid": "ta:g1-d2", "name": "Nowhere Inn", "city": "Bled"})
    return c


def test_pipeline_maps_and_collects(cfg):
    sess = FakeSession({"Alpha": [{"name": "Alpha Hotel", "cc": "si", "slug": "alpha"}]}, {"alpha": nights({0: 200.0, 1: 4.0})})
    res = bc.run(cfg, sess, TODAY, log=lambda m: None)
    assert res.mapped == 1 and res.not_found == 1 and res.collected == 1
    row = next(csv.DictReader(open(cfg["paths"]["booking_calendars"])))
    assert row["booking_id"] == "si/alpha" and row["nights"].startswith("200;4;") and row["currency"] == "USD"



def test_town_search_maps_several_properties_at_once(cfg):
    with open(cfg["paths"]["watchlist"], "w", newline="") as f:
        w = csv.DictWriter(f, ["pid", "name", "city"]); w.writeheader()
        for i, n in enumerate(["Alpha Hotel", "Beta Resort", "Gamma Inn"]):
            w.writerow({"pid": f"ta:g1-d{i}", "name": n, "city": "Bled"})
    town = [{"name": "Alpha Hotel", "cc": "si", "slug": "alpha"}, {"name": "Beta Resort", "cc": "si", "slug": "beta"}]
    sess = FakeSession({"Bled": town}, {"alpha": nights({0: 100.0}), "beta": nights({0: 120.0})})
    res = bc.run(cfg, sess, TODAY, log=lambda m: None)
    assert res.mapped == 2 and res.not_found == 1
    town_searches = [s for s in sess.searches if s[0] == "Bled"]
    assert len(town_searches) == 2 and town_searches[0][1].startswith("class")      # filtered town searches first


def test_shards_split_every_property_once():
    pids = [f"ta:g1-d{i}" for i in range(400)]
    parts = [[p for p in pids if bc.shard_of(p, 4) == k] for k in range(4)]
    assert sum(len(x) for x in parts) == 400 and all(60 < len(x) < 140 for x in parts)
    assert bc.shard_of("ta:g1-d7", 4) == bc.shard_of("ta:g1-d7", 4)          # stable between runs


def test_near_refresh_keeps_far_months_and_full_refresh_daily(cfg):
    cfg["booking_scan"].update(windows=4, near_windows=2, far_every_hours=24, shards=1)
    with open(cfg["paths"]["booking_ids"], "w", newline="") as f:
        w = csv.DictWriter(f, bc.ID_FIELDS); w.writeheader()
        w.writerow({"pid": "ta:g1-d1", "booking_id": "si/alpha", "booking_name": "Alpha Hotel", "similarity": 1,
                    "status": "ok", "checked_at": "2099-01-01T00:00:00+00:00"})

    class Windows(FakeSession):
        def year(self, cc, slug, today, windows):
            self.asked = getattr(self, "asked", []) + [windows]
            return {(today + timedelta(days=d)).isoformat(): {"price": 100.0 + windows, "available": True, "min_stay": 1}
                    for d in range(0, 61 * windows, 30)}
    s = Windows({}, {})
    bc.run(cfg, s, TODAY, log=lambda m: None, part="calendars")
    s2 = Windows({}, {})
    bc.run(cfg, s2, TODAY, log=lambda m: None, part="calendars")
    assert s.asked == [4] and s2.asked == [2]                       # all 24 months first, then only the near ones
    row = next(csv.DictReader(open(cfg["paths"]["booking_calendars"])))
    n = bc.decode_nights(row["first_date"], row["nights"])
    assert n[TODAY.isoformat()]["price"] == 102.0                    # near: this pass
    assert n[(TODAY + timedelta(days=180)).isoformat()]["price"] == 104.0   # far: kept from the full pass
    assert len(row["nights"].split(";")) == 61 * 4


def test_stops_when_booking_blocks(cfg):
    cfg["booking_scan"].update(stop_after_failures=3, shards=1)
    with open(cfg["paths"]["watchlist"], "w", newline="") as f:
        w = csv.DictWriter(f, ["pid", "name", "city"]); w.writeheader()
        for i in range(8):
            w.writerow({"pid": f"ta:g1-d{i}", "name": f"H{i}", "city": "X"})
    with open(cfg["paths"]["booking_ids"], "w", newline="") as f:
        w = csv.DictWriter(f, bc.ID_FIELDS); w.writeheader()
        for i in range(8):
            w.writerow({"pid": f"ta:g1-d{i}", "booking_id": f"si/h{i}", "booking_name": f"H{i}", "similarity": 1,
                        "status": "ok", "checked_at": "2099-01-01T00:00:00+00:00"})

    class Blocked(FakeSession):
        def year(self, *a):
            self.n = getattr(self, "n", 0) + 1
            raise RuntimeError("challenge")
    s = Blocked({}, {})
    res = bc.run(cfg, s, TODAY, log=lambda m: None, part="calendars")
    assert s.n == 3 and res.failed == 3 and "stopped after repeated failures" in res.errors


def test_due_and_merge(tmp_path):
    from datetime import datetime, timezone
    now = datetime(2026, 11, 23, 16, 0, tzinfo=timezone.utc)
    rows = [{"pid": "a", "status": "ok", "scanned_at": "2026-11-23T12:30:00+00:00"},
            {"pid": "b", "status": "error", "scanned_at": "2026-11-23T15:00:00+00:00"}]
    assert bc.is_due(rows, now, 4) is False                 # newest good calendar 3.5 h old
    assert bc.is_due(rows, now.replace(hour=17), 4) is True and bc.is_due([], now, 4) is True
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    for path, rs in ((a, [{"pid": "x", "status": "ok", "scanned_at": "2026-11-23T10:00:00+00:00", "nights": "1"},
                          {"pid": "y", "status": "ok", "scanned_at": "2026-11-23T14:00:00+00:00", "nights": "y-new"}]),
                     (b, [{"pid": "x", "status": "ok", "scanned_at": "2026-11-23T15:00:00+00:00", "nights": "x-new"},
                          {"pid": "y", "status": "error", "scanned_at": "2026-11-23T15:30:00+00:00", "nights": ""}])):
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, ["pid", "status", "scanned_at", "nights"]); w.writeheader(); w.writerows(rs)
    merged = {r["pid"]: r["nights"] for r in bc.merge_files([str(a), str(b)])}
    assert merged == {"x": "x-new", "y": "y-new"}             # a failure never replaces a good calendar
