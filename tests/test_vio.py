"""Vio reader (hotel_scraper/vio.py). Answers in the shape Vio's own SDK documents (@findhotel/sapi 1.35.2 README:
suggest, hotelAvailability); real captured answers are added from the first live run (vio_samples)."""
import csv
from datetime import date, datetime, timezone
from types import SimpleNamespace

from hotel_scraper import config, vio

SUGGEST = [{"highlightValue": "<em>London</em>", "objectID": "158584", "objectType": "place",
            "placeDisplayName": "United Kingdom", "placeTypeName": "city", "value": "London"},
           {"highlightValue": "Park Plaza Westminster Bridge <em>London</em>", "objectID": "1546646",
            "objectType": "hotel", "placeDisplayName": "London, United Kingdom", "placeTypeName": "property",
            "value": "Park Plaza Westminster Bridge London"}]


def offer(base, taxes, intermediary="FHT", tier="sensitive", provider="WTM", amenities=("breakfast",)):
    return {"id": "o1", "currency": "EUR", "url": "https://r.vio.com?ofd=x", "rate": {"base": base, "taxes": taxes, "hotelFees": 3},
            "accessTier": tier, "providerCode": provider, "intermediaryProvider": intermediary,
            "package": {"amenities": list(amenities), "canPayLater": False},
            "cancellationPenalties": [{"start": "2023-07-30T10:00:00Z", "end": "2023-08-02T09:59:00Z", "amount": 0, "currency": "EUR"}]}


def test_suggestions_keep_hotels():
    assert vio.suggestions(SUGGEST) == [{"id": "1546646", "name": "Park Plaza Westminster Bridge London",
                                         "where": "London, United Kingdom"}]


def test_only_offers_vio_sells_itself():
    doc = {"availability": {"1380416": {
        "2023-08-05": {"hotelID": "1380416", "offers": [offer(531.99, 34.73), offer(400, 30, intermediary="", provider="BKS")]},
        "2023-08-06": {"hotelID": "1380416", "offers": [offer(300, 20, intermediary="", provider="BKS")]},
        "2023-08-07": {"hotelID": "1380416", "offers": [offer(200, 20, tier="signed_in")]}}}}
    got = vio.availability(doc, ["", "sensitive"])
    a = got[("1380416", "2023-08-05")]
    # Booking.com's offer shown on Vio (400) is a comparison, not a sale: Vio's own (566.72) is the price
    assert a["available"] and a["price"] == 566.72 and a["price_excl"] == 531.99 and a["price_total"] == 569.72
    assert a["breakfast"] == "included" and a["cancellation"] == "free until 2023-08-02" and a["provider"] == "WTM"
    assert got[("1380416", "2023-08-06")] == {"available": False}
    assert got[("1380416", "2023-08-07")] == {"available": False}       # members only: not what a visitor sees


def test_calendar_windows_of_61_nights():
    w = vio.windows(date(2026, 11, 25), 364, 61)
    assert w[0] == (date(2026, 11, 25), date(2027, 1, 24)) and w[-1][1] == date(2027, 11, 24) and len(w) == 6


def test_calendar_polls_until_complete():
    pages = [{"availability": {"1": {"2027-01-01": {"offers": []}}}, "status": {"complete": False, "nextToken": "t1"}},
             {"availability": {"1": {"2027-01-02": {"offers": [offer(100, 10)]}}}, "status": {"complete": True}}]
    asked = []

    def get(url):
        asked.append(url)
        return SimpleNamespace(status=200, json=lambda: pages[len(asked) - 1], text=lambda: "")
    api = vio.Vio({**config.load()["vio"], "poll_seconds": 0}, get=get)
    doc = api.calendar(["1"], date(2027, 1, 1), date(2027, 1, 2), 2)
    assert set(doc["availability"]["1"]) == {"2027-01-01", "2027-01-02"} and "token=t1" in asked[1]
    assert "hotelIds=1" in asked[0] and "rooms=2" in asked[0] and "nights=1" in asked[0] and "brand=vio" in asked[0]


class FakeApi:
    def __init__(self):
        self.calls = []

    def suggest(self, q):
        return [{"id": "1546646", "name": "Hotel Artemide", "where": "Rome"}] if "Artemide" in q else []

    def position(self, hid):
        return 41.900819, 12.493655

    def calendar(self, ids, first, last, adults):
        self.calls.append((tuple(ids), first, last))
        return {"availability": {"1546646": {first.isoformat(): {"offers": [offer(150, 15)]},
                                             last.isoformat(): {"offers": []}}}}


def test_run_ids_then_calendars(tmp_path):
    cfg = config.load()
    for k in ("watchlist", "seller_ids", "seller_prices", "locations"):
        cfg["paths"][k] = str(tmp_path / f"{k}.csv")
    with open(cfg["paths"]["watchlist"], "w", newline="") as f:
        w = csv.DictWriter(f, ["pid", "name", "city", "lat", "lon", "gone_since"])
        w.writeheader()
        w.writerows([{"pid": "ta:art", "name": "Hotel Artemide", "city": "Rome", "lat": "41.9005", "lon": "12.4930", "gone_since": ""},
                     {"pid": "ta:x", "name": "Unknown Inn", "city": "Nowhere", "lat": "46", "lon": "13", "gone_since": ""}])
    api = FakeApi()
    res = vio.run(cfg, "all", 5, api, today=date(2026, 11, 24), log=lambda m: None, sleep=lambda s: None,
                  now_dt=datetime(2026, 11, 24, 2, tzinfo=timezone.utc))
    assert res["ids_found"] == 1 and res["ids_coverage"] == "1/2"
    assert len(api.calls) == 6 and api.calls[0][1] == date(2026, 11, 25)       # a year in 61-night calendars
    rows = list(csv.DictReader(open(cfg["paths"]["seller_prices"])))
    yes = [r for r in rows if r["available"] == "yes"]
    assert len(rows) == 12 and len(yes) == 6 and yes[0]["price"] == "165" and yes[0]["seller"] == "vio"
    assert yes[0]["url"] == "https://r.vio.com?ofd=x" and yes[0]["cancellation"].startswith("free until")


def test_at_most_five_hotels_per_calendar_call():
    """Regression (run 45374257349): Vio answered 400 "HotelIDs should have at most 5 elements" to 20 per call."""
    assert 1 <= config.load()["vio"]["hotels_per_call"] <= 5
