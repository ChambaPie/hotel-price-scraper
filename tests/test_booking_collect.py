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
        self.cards, self.years, self.token, self.calls = cards, years, "t", 0

    def search(self, q, checkin, wait=20, scrolls=0, raw=False):
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
    assert row["booking_id"] == "si/alpha" and row["nights"].startswith("200;4;")
