"""HRS reader (hotel_scraper/hrs.py) on HRS's real answers, captured by the seller probe (2026-11-23): its search-box
suggestions for "Hotel Artemide Rome" and its hotel search for Rome on 23 Nov 2026 (5 hotels kept)."""
import csv
import json
from datetime import date, datetime, timezone
from pathlib import Path

from hotel_scraper import config, hrs

FX = Path(__file__).parent / "fixtures"
LOC = json.loads((FX / "hrs_locations_artemide_2026-11.json").read_text())
SEARCH = json.loads((FX / "hrs_search_rome_2026-11-23.json").read_text())
REQUEST = json.loads((FX / "hrs_search_request_2026-11.json").read_text())


def test_suggestions_with_position():
    s = hrs.locations(LOC)
    assert s[0] == {"id": 66796, "name": "Artemide", "lat": 41.900978, "lon": 12.493591, "where": "Rome, Italy Lazio",
                    "city_id": 54084}
    assert len(s) == 5


def test_search_answer_single_and_double_rooms():
    one = hrs.offers(SEARCH, 1)
    assert one[4985] == {"available": True, "price": 175.35, "price_excl": 159.41, "breakfast": "included"}
    assert one[2293795]["available"] is False                 # HRS: NO_VALID_OFFER
    two = hrs.offers(SEARCH, 2)
    # that search was for one adult: a double room let to one person is not a price for two
    assert not any(v["available"] for v in two.values())


def test_search_request_for_our_hotels_and_night():
    b = hrs.search_body(REQUEST, [66796, 4985], date(2027, 2, 14), 2)
    assert b["requestParameter"]["dateRange"] == {"fromDate": "2027-02-14", "toDate": "2027-02-15"}
    rr = b["requestParameter"]["roomRequests"][0]
    assert rr["roomType"] == "DOUBLEROOM" and len(rr["occupancy"]["adults"]) == 2
    assert b["findParameter"]["matchType"] == "HOTEL_IDS" and b["findParameter"]["hotelIds"] == [66796, 4985]
    assert b["recommendationParameter"] == REQUEST["recommendationParameter"]
    assert REQUEST["findParameter"]["hotelIds"] == [66796]                       # template untouched


def test_booking_link():
    u = hrs.booking_url(config.load()["hrs"]["hotel_url"], 66796, date(2026, 12, 31), 2)
    assert "hotelTeaserKey%5B0%5D=66796" in u and "startDateDay=31" in u and "endDateYear=2027" in u
    assert "doubleRooms=1" in u and "adults=2" in u


class FakePage:
    def __init__(self):
        self.suggested, self.calls = [], []

    def suggest(self, q):
        self.suggested.append(q)
        return hrs.locations(LOC) if "Artemide" in q else []

    def prices(self, ids, night, adults):
        self.calls.append((tuple(ids), night, adults))
        doc = json.loads(json.dumps(SEARCH))
        doc["hotels"][0]["hotelId"] = 66796        # the answer for our hotel
        doc["requestedHotels"] = list(ids)
        return hrs.offers(doc, 1)


def test_run_finds_ids_then_prices_many_hotels_per_call(tmp_path):
    cfg = config.load()
    for k in ("watchlist", "seller_ids", "seller_prices", "locations"):
        cfg["paths"][k] = str(tmp_path / f"{k}.csv")
    with open(cfg["paths"]["watchlist"], "w", newline="") as f:
        w = csv.DictWriter(f, ["pid", "name", "city", "lat", "lon", "gone_since"])
        w.writeheader()
        w.writerows([{"pid": "ta:art", "name": "Hotel Artemide", "city": "Rome", "lat": "41.9005", "lon": "12.4930",
                      "gone_since": ""},
                     {"pid": "ta:x", "name": "Unknown Inn", "city": "Nowhere", "lat": "46", "lon": "13", "gone_since": ""}])
    page = FakePage()
    res = hrs.run(cfg, "all", 5, page, today=date(2026, 11, 24), log=lambda m: None, sleep=lambda s: None,
                  now_dt=datetime(2026, 11, 24, 2, tzinfo=timezone.utc))
    assert res["ids_found"] == 1 and res["ids_missed"] == 1 and res["ids_coverage"] == "1/2"
    ids = {r["pid"]: r for r in csv.DictReader(open(cfg["paths"]["seller_ids"]))}
    assert ids["ta:art"]["seller_id"] == "66796" and ids["ta:art"]["seller"] == "hrs"
    n = cfg["hrs"]["nights_per_pass"]
    assert len(page.calls) == n and all(c[0] == (66796,) and c[2] == cfg["scan"]["adults"] for c in page.calls)
    rows = list(csv.DictReader(open(cfg["paths"]["seller_prices"])))
    assert len(rows) == n and rows[0]["price"] == "175.35" and rows[0]["price_excl"] == "159.41"
    assert rows[0]["seller"] == "hrs" and "hotelTeaserKey%5B0%5D=66796" in rows[0]["url"]
