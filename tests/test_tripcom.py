"""Trip.com reader (hotel_scraper/tripcom.py) on Trip.com's real answers, captured by the seller probe (2026-11-23):
its search-box suggestions for "Hotel Artemide Rome" and its results list for that hotel on 14 Dec 2026."""
import csv
import json
from datetime import date, datetime, timezone
from pathlib import Path

from hotel_scraper import config, tripcom

FX = Path(__file__).parent / "fixtures"
KEYWORDS = json.loads((FX / "tripcom_keywords_artemide_2026-11.json").read_text())
LIST = json.loads((FX / "tripcom_list_artemide_2026-12-14.json").read_text())
LIST_REQUEST = json.loads((FX / "tripcom_list_request_2026-11.json").read_text())
KW_REQUEST = json.loads((FX / "tripcom_keywords_request_2026-11.json").read_text())


def test_suggestions_keep_hotels_only_with_their_position_and_city():
    ks = tripcom.keywords(KEYWORDS)
    assert ks[0] == {"id": 760667, "name": "Hotel Artemide", "lat": 41.900819, "lon": 12.493655, "city_id": 343,
                     "country_id": 106}
    assert all(k["name"] != "Showroom Artemide Roma" for k in ks)     # a landmark ("LM"), not a hotel


def test_results_list_prices_comparable_with_booking():
    h = tripcom.hotels(LIST)
    assert len(h) == 1 and h[0]["id"] == 760667 and h[0]["available"]
    # with taxes and fees, before the city tax paid at the hotel (as Booking.com and Agoda show it)
    assert h[0]["price"] == 188.77 and h[0]["price_excl"] == 171.61 and h[0]["price_total"] == 203.77
    assert h[0]["currency"] == "EUR"


def test_no_price_means_not_available():
    doc = json.loads(json.dumps(LIST))
    doc["data"]["hotelList"][0]["roomInfo"][0]["priceInfo"].update(price=0, isHiddenPrice=False)
    h = tripcom.hotels(doc)[0]
    assert not h["available"] and h["price"] is None and h["price_total"] is None


def test_money():
    assert tripcom.money("€188.77") == 188.77 and tripcom.money("€1,204") == 1204.0 and tripcom.money("") is None
    assert tripcom.money(172) == 172.0


def test_list_request_for_another_hotel_and_night():
    b = tripcom.list_body(LIST_REQUEST, [1234], 99, 7, date(2027, 3, 1), 2)
    assert b["date"]["dateInfo"] == {"checkInDate": "20270301", "checkOutDate": "20270302"}
    assert b["destination"]["geo"] == {"cityId": 99, "countryId": 7} and "keyword" not in b["destination"]
    ids = [f["value"] for f in b["filters"] if f["type"] == "31"]
    assert ids == ["1234"]                                    # the captured hotel's filter replaced, not kept
    assert [f["value"] for f in b["filters"] if f["type"] == "29"] == ["1|2"]
    assert b["head"] == LIST_REQUEST["head"] and LIST_REQUEST["date"]["dateInfo"]["checkInDate"] == "20261214"


def test_keywords_request():
    b = tripcom.keywords_body(KW_REQUEST, "Hotel Andy Merano")
    assert b["queryInfo"]["keyword"] == "Hotel Andy Merano" and b["head"] == KW_REQUEST["head"]


def test_rotation_moves_on_each_pass():
    rows = list(range(10))
    a = tripcom.rotate(rows, datetime(2026, 11, 24, 1, tzinfo=timezone.utc), 3)
    b = tripcom.rotate(rows, datetime(2026, 11, 24, 5, tzinfo=timezone.utc), 3)
    assert sorted(a) == rows and (b[0] - a[0]) % 10 == 3


class FakePage:
    def __init__(self):
        self.suggested, self.calls = [], []

    def suggest(self, q):
        self.suggested.append(q)
        return tripcom.keywords(KEYWORDS) if "Artemide" in q else []

    def nights(self, ids, city_id, country_id, night, adults):
        self.calls.append((tuple(ids), city_id, country_id, night))
        return tripcom.hotels(LIST)


def _setup(tmp_path):
    cfg = config.load()
    for k in ("watchlist", "seller_ids", "seller_prices", "locations"):
        cfg["paths"][k] = str(tmp_path / f"{k}.csv")
    with open(cfg["paths"]["watchlist"], "w", newline="") as f:
        w = csv.DictWriter(f, ["pid", "name", "city", "lat", "lon", "gone_since"])
        w.writeheader()
        w.writerows([{"pid": "ta:art", "name": "Hotel Artemide", "city": "Rome", "lat": "41.9005", "lon": "12.4930",
                      "gone_since": ""},
                     {"pid": "ta:x", "name": "Unknown Inn", "city": "Nowhere", "lat": "46", "lon": "13", "gone_since": ""}])
    with open(cfg["paths"]["seller_ids"], "w", newline="") as f:       # another seller's ids stay untouched
        w = csv.DictWriter(f, tripcom.ID_FIELDS)
        w.writeheader()
        w.writerow({"pid": "ta:art", "seller": "agoda", "seller_id": "1081", "status": "ok", "checked_at": "2026-11-23"})
    return cfg


def test_run_finds_ids_then_prices_nights(tmp_path):
    cfg = _setup(tmp_path)
    page = FakePage()
    now = datetime(2026, 11, 24, 2, tzinfo=timezone.utc)
    res = tripcom.run(cfg, "all", 5, page, today=date(2026, 11, 24), log=lambda m: None, sleep=lambda s: None,
                      now_dt=now)
    assert res["ids_found"] == 1 and res["ids_missed"] == 1 and res["ids_coverage"] == "1/2"
    assert page.suggested == ["Hotel Artemide Rome", "Unknown Inn Nowhere", "Unknown Inn"]
    ids = {(r["seller"], r["pid"]): r for r in csv.DictReader(open(cfg["paths"]["seller_ids"]))}
    assert ids[("agoda", "ta:art")]["seller_id"] == "1081"
    t = ids[("trip_com", "ta:art")]
    assert t["seller_id"] == "760667" and t["city_id"] == "343|106" and t["status"] == "ok"
    assert ids[("trip_com", "ta:x")]["status"] == "not found"
    n = cfg["tripcom"]["nights_per_pass"]
    assert len(page.calls) == n and {c[:3] for c in page.calls} == {((760667,), 343, 106)}
    assert min(c[3] for c in page.calls) >= date(2026, 11, 25)                 # tonight skipped (min_lead_days)
    rows = list(csv.DictReader(open(cfg["paths"]["seller_prices"])))
    assert len(rows) == n and rows[0]["seller"] == "trip_com" and rows[0]["price"] == "188.77"
    assert rows[0]["price_excl"] == "171.61" and rows[0]["price_total"] == "203.77" and rows[0]["available"] == "yes"
    assert "hotelId=760667" in rows[0]["url"] and f"checkIn={rows[0]['night']}" in rows[0]["url"]
    # next pass: ids reused (no searches), other nights
    page2 = FakePage()
    tripcom.run(cfg, "all", 5, page2, today=date(2026, 11, 24), log=lambda m: None, sleep=lambda s: None,
                now_dt=datetime(2026, 11, 24, 6, tzinfo=timezone.utc))
    assert page2.suggested == [] and {c[3] for c in page2.calls} != {c[3] for c in page.calls}
    assert len(list(csv.DictReader(open(cfg["paths"]["seller_prices"])))) > n


def test_other_hotels_in_the_answer_are_not_taken_for_ours(tmp_path):
    cfg = _setup(tmp_path)
    tripcom.run(cfg, "ids", 5, FakePage(), today=date(2026, 11, 24), log=lambda m: None, sleep=lambda s: None)

    class Others(FakePage):
        def nights(self, ids, *a):
            doc = json.loads(json.dumps(LIST))
            doc["data"]["hotelList"][0]["hotelInfo"]["summary"]["hotelId"] = "1"
            return tripcom.hotels(doc)
    res = tripcom.run(cfg, "prices", 5, Others(), today=date(2026, 11, 24), log=lambda m: None, sleep=lambda s: None)
    assert res["calls"] > 0 and res["priced"] == 0
    assert [r for r in csv.DictReader(open(cfg["paths"]["seller_prices"])) if r["seller"] == "trip_com"] == []
