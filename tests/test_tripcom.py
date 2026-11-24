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
    b = tripcom.list_body(LIST_REQUEST, [1234], 99, 7, date(2027, 3, 1), 2, "Hotel Andy")
    assert b["date"]["dateInfo"] == {"checkInDate": "20270301", "checkOutDate": "20270302"}
    assert b["destination"]["geo"] == {"cityId": 99, "countryId": 7}
    assert b["destination"]["keyword"] == {"word": "Hotel+Andy"}     # as the page sends it (run 45277915620)
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

    def nights(self, ids, city_id, country_id, night, adults, name=""):
        self.calls.append((tuple(ids), city_id, country_id, night))
        self.names = getattr(self, "names", set()) | {name}
        return tripcom.hotels(LIST)


def _setup(tmp_path):
    cfg = config.load()
    for k in ("watchlist", "seller_ids", "seller_prices", "locations"):
        cfg["paths"][k] = str(tmp_path / f"{k}.csv")
    cfg["paths"]["out_dir"] = str(tmp_path / "out")
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
    assert page.names == {"Hotel Artemide"}        # the page sends the name with the id (run 45277915620)
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
        def nights(self, ids, *a, **k):
            doc = json.loads(json.dumps(LIST))
            doc["data"]["hotelList"][0]["hotelInfo"]["summary"]["hotelId"] = "1"
            return tripcom.hotels(doc)
    pg = Others()
    pg.last = {"data": {"hotelList": []}}
    res = tripcom.run(cfg, "prices", 5, pg, today=date(2026, 11, 24), log=lambda m: None, sleep=lambda s: None)
    assert res["calls"] > 0 and res["priced"] == 0 and res["missing"] == res["calls"]
    assert (tmp_path / "out" / "tripcom_miss_1.json").exists()      # what came back instead, kept to see why
    assert [r for r in csv.DictReader(open(cfg["paths"]["seller_prices"])) if r["seller"] == "trip_com"] == []


def test_list_request_is_a_new_search_not_a_refresh():
    """Regression (run 45333656075): the captured request carried the page's refresh token and session, so Trip.com
    re-priced the captured list (Hotel Artemide) whatever hotel was asked: 912 answers of 912."""
    b = tripcom.list_body(LIST_REQUEST, [1234], 99, 7, date(2027, 3, 1), 2, "Hotel Andy")
    assert "dynamicRefresh" not in b and b["extraFilter"]["sessionId"] != LIST_REQUEST["extraFilter"]["sessionId"]
    assert "dynamicRefresh" in LIST_REQUEST                                         # the template is left as it was


def test_results_page_address():
    u = tripcom.list_url(config.load()["tripcom"]["list_page"], 760667, 343, 106, date(2026, 12, 14), 2)
    assert "optionId=760667" in u and "cityId=343" in u and "checkin=2026-12-14" in u and "checkout=2026-12-15" in u
    assert "searchValue=31%7C760667" in u and "adult=2" in u


def test_empty_answer_about_our_hotel_is_not_taken_as_sold_out():
    """Runs 45370159785 / 45374257349: asked as a new search, Trip.com answers about exactly our hotel (queryType
    "Single", its filter 31|4340342) with an empty list. Taken as "nothing to sell", it gave 0 available of 276, while
    Trip.com's own results pages gave 27 of 96 the hour before: such answers say nothing. Prices come from its
    results pages (price_mode: pages)."""
    doc = json.loads((FX / "tripcom_list_empty_single_2026-11-24.json").read_text())
    assert tripcom.asked_for(doc) == {4340342} and tripcom.hotels(doc) == []
    assert config.load()["tripcom"]["price_mode"] == "pages"


def test_not_available_rows_from_empty_answers_are_dropped(tmp_path):
    """Run 45374257349 stored 'not available' for 276 hotel-nights from list answers that said nothing: dropped."""
    cfg = _setup(tmp_path)
    tripcom.run(cfg, "ids", 5, FakePage(), today=date(2026, 11, 24), log=lambda m: None, sleep=lambda s: None)
    with open(cfg["paths"]["seller_prices"], "w", newline="") as f:
        w = csv.DictWriter(f, tripcom.PRICE_FIELDS)
        w.writeheader()
        w.writerow({"pid": "ta:art", "seller": "trip_com", "night": "2027-05-01", "available": "no", "seen_at": "2026-11-24T10:35:00+00:00"})
        w.writerow({"pid": "ta:art", "seller": "agoda", "night": "2027-05-01", "available": "no", "seen_at": "2026-11-24T10:35:00+00:00"})

    class Nothing(FakePage):
        def nights(self, *a, **k):
            return []
    tripcom.run(cfg, "prices", 5, Nothing(), today=date(2026, 11, 24), log=lambda m: None, sleep=lambda s: None)
    rows = list(csv.DictReader(open(cfg["paths"]["seller_prices"])))
    assert [r["seller"] for r in rows] == ["agoda"]


def test_results_page_with_an_empty_list_says_nothing():
    """Regression (runs of 2026-11-24 11:05-11:20): Trip.com's results pages answered an empty list about our hotel for
    40 of 40 pages, with no hotel at all on the page (Trip.com holding back, not a sold-out hotel; taken as "not
    available" for an hour). Such pages now record nothing."""
    empty = json.loads((FX / "tripcom_list_empty_single_2026-11-24.json").read_text())
    assert tripcom.page_answer([empty], 4340342) == []
    assert tripcom.page_answer([empty, LIST], 760667)[0]["price"] == 188.77


def test_card_on_its_own_results_page_real_pages():
    """Trip.com draws the hotel list into its results page (no separate call names our hotel; probe 2026-11-24):
    our hotel's card is read from the page."""
    ok = (FX / "tripcom_listpage_corinthia_available_2026-11.txt").read_text(encoding="utf-8")
    c = tripcom.card_from_text(ok, "Corinthia Budapest")
    assert c["available"] and c["price"] == 213.0 and c["price_excl"] == 196.0 and c["price_total"] == 213.0
    no = (FX / "tripcom_listpage_seefels_noroom_2026-11-24.txt").read_text(encoding="utf-8")
    c = tripcom.card_from_text(no, "Hotel Schloss Seefels")
    assert c["available"] is False and c["price"] is None      # "No rooms available for your selected dates"
    assert tripcom.card_from_text(ok, "Hotel Artemide") is None                               # not on the page


def test_results_page_address_like_its_search_box():
    u = tripcom.list_url(config.load()["tripcom"]["list_page"], 4378634, 37324, 14, date(2026, 12, 14), 2,
                         "Hotel Schloss Seefels")
    assert "optionId=4378634" in u and "searchWord=Hotel%20Schloss%20Seefels" in u and "old=1" in u
    assert "listFilters=29~1*29*1~2*2" in u and "checkin=2026-12-14" in u
