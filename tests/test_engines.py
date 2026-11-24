"""Hotels' own booking engines: codes from the calls a booking page makes, price calendars (real captured answers)."""
import csv
import json
from datetime import date
from pathlib import Path

from hotel_scraper import config, engines
from hotel_scraper.http import Response

FX = Path(__file__).parent / "fixtures"
CFG = config.load()


def req(name):
    return (FX / f"engine_request_{name}.txt").read_text().split(" ", 1)[1].strip()


def test_codes_from_real_booking_page_calls():
    assert engines.code_from_requests("seekda", [req("seekda_Adler_Inn_Tyrol_Mountain_Resort_Superior.api2")]) == {"code": "AT_TUX_BERGLAND"}
    assert engines.code_from_requests("seekda", [req("seekda_Hotel_Alpenhof.api1")]) == {"code": "AT_HINTER_ALPEN"}
    d = engines.code_from_requests("d_edge", ["https://x/other", req("d_edge_Domaine_de_Montagne_Chalet_Fr_re_Joseph.api2")])
    assert d["code"] == "J1I6-29552" and d["group"] == "17812" and d["auth"].startswith("eyJ")
    assert engines.code_from_requests("seekda", ["https://example.org/"]) is None
    sid = engines.seller_id("d_edge", d)
    assert engines.parse_seller_id(sid) == ("d_edge", d)


def test_seekda_calendar_real_answer():
    n = engines.seekda_nights(json.loads((FX / "seekda_calendar_adler_inn_2026-12.json").read_text()))
    assert len(n) == 19 and n["2026-12-01"] == {"price": 354.0, "min_stay": 1, "url": ""}
    assert "2026-12-19" not in n          # priced but no arrival that day (scope STAY only: inside a longer stay)


def test_dedge_calendar_real_answer():
    n = engines.dedge_nights(json.loads((FX / "dedge_bestprice_domaine_montagne_2026-12.json").read_text()))
    assert n["2026-12-01"]["price"] == 230.0 and "arrivalDate=2026-12-01" in n["2026-12-01"]["url"]
    assert "2026-12-12" not in n                                                     # NotAvailable that night


def test_months_cover_the_horizon():
    m = engines.months(date(2026, 11, 25), 365)
    assert m[0] == (date(2026, 11, 25), date(2026, 11, 30)) and m[1] == (date(2026, 12, 1), date(2026, 12, 31))
    assert m[-1][1] == date(2027, 11, 25) and len(m) == 13


def test_run_ids_then_calendars(tmp_path):
    cfg = config.load()
    for k in ("watchlist", "official_sites", "seller_ids", "seller_prices"):
        cfg["paths"][k] = str(tmp_path / f"{k}.csv")
    with open(cfg["paths"]["watchlist"], "w", newline="") as f:
        w = csv.DictWriter(f, ["pid", "gone_since"])
        w.writeheader()
        w.writerows([{"pid": x, "gone_since": ""} for x in "abcd"])
    with open(cfg["paths"]["official_sites"], "w", newline="") as f:
        w = csv.DictWriter(f, ["pid", "name", "engine", "booking_link", "website"])
        w.writeheader()
        w.writerows([{"pid": "a", "name": "Adler Inn", "engine": "seekda", "booking_link": "https://adlerinn.officialbookings.com", "website": ""},
                     {"pid": "b", "name": "Domaine", "engine": "d_edge", "booking_link": "https://domaine.example/booking", "website": ""},
                     {"pid": "c", "name": "Other", "engine": "vertical_booking", "booking_link": "https://x", "website": ""},
                     {"pid": "d", "name": "Albafiorita", "engine": "simplebooking", "website": "",
                      "booking_link": "https://www.simplebooking.it/ibe/hotelbooking/search?hid=8930&lang=it"}])
    seek = (FX / "seekda_calendar_adler_inn_2026-12.json").read_bytes()
    dedge = (FX / "dedge_bestprice_domaine_montagne_2026-12.json").read_bytes()

    def finder(sites, ec, log=None, budget_s=0):
        assert {s["pid"] for s in sites} == {"a", "b"}   # engines with a reader whose code isn't in the link already
        return {"a": {"code": "AT_TUX_BERGLAND"},
                "b": engines.code_from_requests("d_edge", [req("d_edge_Domaine_de_Montagne_Chalet_Fr_re_Joseph.api2")])}
    calls = []

    def get(url):
        calls.append(url)
        if "seekda" in url and "firstDate=2026-12-01" in url:
            return Response(200, {}, seek)
        if "d-edge" in url and "fromDate=2026-12-01" in url:
            return Response(200, {}, dedge)
        return Response(200, {}, b'{"result": [], "data": {"data": []}}')
    sb = (FX / "simplebooking_search_albafiorita_2026-12-14.json").read_bytes()
    posted = []

    def post(url, body):
        posted.append((url, body["variables"]["query"]["checkIn"]))
        return Response(200, {}, sb if len(posted) % 2 else b'{"data": {"hotel": {"result": {"options": []}}}}')
    res = engines.run(cfg, "all", 5, get=get, finder=finder, today=date(2026, 11, 24), log=lambda m: None,
                      sleep=lambda s: None, post=post)
    assert res["ids_found"] == 3 and res["hotels_priced"] == 3 and res["ids_coverage"].startswith("3/3")
    assert len(posted) == 8 and all("hid=8930" in u for u, _ in posted)             # 8 nights per pass
    d = [r for r in csv.DictReader(open(cfg["paths"]["seller_prices"])) if r["pid"] == "d"]
    assert len(d) == 8 and sum(r["available"] == "yes" for r in d) == 4 and {r["price"] for r in d if r["price"]} == {"84.0"}
    assert all("/ibe2/hotel/8930?" in r["url"] for r in d if r["available"] == "yes")
    rows = list(csv.DictReader(open(cfg["paths"]["seller_prices"])))
    a = [r for r in rows if r["pid"] == "a"]
    assert len(a) == 19 and a[0]["seller"] == "official" and a[0]["seller_id"] == "seekda" and a[0]["price"] == "354.0"
    assert "skd-checkin=2026-12-01" in a[0]["url"] and "skd-property-code=AT_TUX_BERGLAND" in a[0]["url"]
    b = {r["night"]: r for r in rows if r["pid"] == "b"}
    assert b["2026-12-01"]["price"] == "230.0" and "secure-hotel-booking.com" in b["2026-12-01"]["url"]
    assert sum(1 for u in calls if "seekda" in u) == 13                              # a month per call, 12 months ahead


def test_simplebooking_real_search_and_request():
    doc = json.loads((FX / "simplebooking_search_albafiorita_2026-12-14.json").read_text())
    assert engines.simplebooking_night(doc) == {"price": 84.0, "min_stay": 1, "url": ""}       # cheapest of 5 rooms
    assert engines.simplebooking_night({"data": {"hotel": {"result": {"options": []}}}}) is None
    tpl = json.loads((FX / "simplebooking_search_request_2026-11.json").read_text())
    b = engines.simplebooking_body(tpl, "1234", date(2027, 3, 1), 3)
    assert b["variables"]["hotelId"] == "1234" and b["variables"]["query"]["checkIn"] == "2027-03-01"
    assert b["variables"]["query"]["checkOut"] == "2027-03-02" and b["variables"]["query"]["allocations"][0]["adults"] == 3
    assert tpl["variables"]["hotelId"] == "8930"                                     # template untouched
    assert engines.code_from_requests("simplebooking", ["https://www.simplebooking.it/ibe/hotelbooking/search?hid=8930&lang=it"]) == {"code": "8930"}
    assert engines.code_from_requests("simplebooking", ["https://www.simplebooking.it/ibe2/hotel/5715?lang=DE"]) == {"code": "5715"}


def test_rotating_nights_cover_the_horizon():
    from datetime import datetime, timedelta, timezone
    start, seen = date(2026, 11, 25), set()
    t = datetime(2026, 11, 24, tzinfo=timezone.utc)
    for k in range(46):
        seen.update(engines.rotating_nights("8930", start, 365, 8, 46, t + timedelta(hours=4 * k)))
    assert len(seen) >= 360 and min(seen) >= start and max(seen) < start + timedelta(days=365)


def test_hns_code_and_calendar_real_answer():
    """HNS onepagebooking (Das Naturhotel Chesa Valisa, 2026-11-23): the hotel's code is only in the body of its own
    calendar call; the calendar gives the cheapest room per night for arrival that day."""
    assert engines.code_from_requests("hotelnetsolutions", [req("hns_rateplan")]) == {"code": "naturhotel-chesa-valisa"}
    assert engines.code_from_requests("hotelnetsolutions", ["https://www.naturhotel.at/buchen/?lang=de"]) is None
    n = engines.hns_nights(json.loads((FX / "hns_rateplan_chesa_valisa_2026-11.json").read_text()))
    assert len(n) == 16 and n["2026-12-11"] == {"price": 495.0, "min_stay": 1, "url": ""}
    assert n["2026-12-18"]["min_stay"] == 3 and "2026-11-23" not in n             # not bookable: left out
    tpl = json.loads((FX / "hns_rateplan_request_2026-11.json").read_text())
    b = engines.hns_body(tpl, "hotel-victoria-nuernberg", date(2027, 1, 1), date(2027, 1, 31), 2)
    assert b["code"] == "hotel-victoria-nuernberg" and (b["startDate"], b["endDate"]) == ("01.01.2027", "31.01.2027")
    assert b["outputType"] == 4 and b["rooms"][0]["adults"] == 2 and b["sessionID"] != tpl["sessionID"]
    assert tpl["code"] == "naturhotel-chesa-valisa"                                   # the template is left as it was


def test_ericsoft_code_and_stay_search_real_answers():
    """Ericsoft (Hotel Calissano, 2026-11-23): the code is in the booking link or in the body of its own calls; adults
    are the hotel's own person type; the cheapest room for the night with VAT."""
    link = "https://booking.ericsoft.com/BookingEngine/Book?idh=C4F7F3CFA10444AB&#038;lang=it&#038;cur=EUR"
    assert engines.code_from_requests("ericsoft", [link]) == {"code": "C4F7F3CFA10444AB", "host": "booking.ericsoft.com"}
    assert engines.code_from_requests("ericsoft", ["https://www.ericsoft.biz/it/BookingEngine/Book?idh=6F47713E95525FA7&lang=it"]) \
        == {"code": "6F47713E95525FA7", "host": "www.ericsoft.biz"}
    assert engines.code_from_requests("ericsoft", [req("ericsoft_solutions")])["code"] == "C4F7F3CFA10444AB"
    conf = json.loads((FX / "ericsoft_configuration_calissano_2026-11.json").read_text())
    assert engines.ericsoft_adult(conf) == 4182
    b = engines.ericsoft_body("C4F7F3CFA10444AB", 4182, date(2026, 11, 23), 2)
    assert b == json.loads(req("ericsoft_solutions").split("\n", 1)[1])               # the page's own request, rebuilt
    n = engines.ericsoft_night(json.loads((FX / "ericsoft_solutions_calissano_2026-11-23.json").read_text()))
    assert n == {"price": 148.0, "min_stay": 1, "url": ""}
    assert engines.ericsoft_night({"operationStatus": 0, "data": {"solutions": []}}) is None


class FakeBrowser:
    """The hotel's booking page: the call it makes (real captured requests and headers) and the engine's answers."""
    def __init__(self):
        self.opened, self.posts = [], []

    def open(self, url, match, wait_s):
        self.opened.append((url, match))
        if "onepagebooking" in match:
            return {"url": "https://v6.onepagebooking.com/api/RatePlan", "headers": {"authorization": "Bearer t"},
                    "body": json.loads((FX / "hns_rateplan_request_2026-11.json").read_text())}
        if "GetSolutions" in match:
            return {"url": "https://booking.ericsoft.com/api/book/GetSolutions", "headers": {"x-xsrf-token": "x"},
                    "body": json.loads(req("ericsoft_solutions").split("\n", 1)[1])}
        if "getProductAvailability" in match:
            return {"url": "https://be.synxis.com/gw/product/v1/getProductAvailability", "headers": {"context": "BE"},
                    "body": json.loads((FX / "synxis_availability_request_2026-11.json").read_text())}
        if "bookingengine/search" in match:
            return {"url": "https://api.foxtechnologies.it/api/be-frontdoor/bookingengine/search",
                    "headers": json.loads((FX / "blastness_search_headers_2026-11.json").read_text()),
                    "body": {"CheckIn": "2026-11-23T00:00:00.000Z", "CheckOut": "2026-11-24T00:00:00.000Z",
                             "LanguageCode": "en", "CurrencyCode": "EUR", "Rooms": [{"Adults": 2, "ChildrenAges": []}]}}
        return None

    def post(self, url, body, headers):
        self.posts.append((url, body, headers))
        if "onepagebooking" in url:
            doc = json.loads((FX / "hns_rateplan_chesa_valisa_2026-11.json").read_text()) \
                if body["startDate"] == "01.12.2026" else {"RatePlans": []}
        elif "ericsoft" in url:
            doc = json.loads((FX / "ericsoft_solutions_calissano_2026-11-23.json").read_text()) \
                if body["startDate"] < "2027-03-01" else {"data": {"solutions": []}}
        elif "synxis" in url:
            doc = json.loads((FX / "synxis_availability_monte_cenci_2026-12-14.json").read_text())
        else:
            doc = json.loads((FX / "blastness_search_argentina_2026-11-23.json").read_text())
        return 200, doc

    def close(self):
        self.closed = True


def test_run_prices_engines_that_answer_only_their_own_page(tmp_path):
    """Run 45333656075: HNS answered plain calls 401 (its page's session token) and Ericsoft 400 (its anti-forgery
    cookie and header). The hotel's booking page is opened, its own call taken and repeated for other dates, with the
    page's headers and cookies; SynXis and Blastness the same way."""
    cfg = config.load()
    for k in ("watchlist", "official_sites", "seller_ids", "seller_prices"):
        cfg["paths"][k] = str(tmp_path / f"{k}.csv")
    with open(cfg["paths"]["watchlist"], "w", newline="") as f:
        w = csv.DictWriter(f, ["pid", "gone_since"])
        w.writeheader()
        w.writerows([{"pid": x, "gone_since": ""} for x in "hesb"])
    with open(cfg["paths"]["official_sites"], "w", newline="") as f:
        w = csv.DictWriter(f, ["pid", "name", "engine", "booking_link", "website"])
        w.writeheader()
        w.writerows([{"pid": "h", "name": "Chesa Valisa", "engine": "hotelnetsolutions",
                      "booking_link": "https://www.naturhotel.at/buchen/?lang=de", "website": ""},
                     {"pid": "e", "name": "Calissano", "engine": "ericsoft", "website": "",
                      "booking_link": "https://booking.ericsoft.com/BookingEngine/Book?idh=C4F7F3CFA10444AB&#038;lang=it"},
                     {"pid": "s", "name": "Monte Cenci", "engine": "synxis", "website": "",
                      "booking_link": "https://be.synxis.com/?adult=2&#038;chain=5375&#038;currency=EUR&#038;hotel=71264&#038;locale=en-US"},
                     {"pid": "b", "name": "Argentina", "engine": "blastness", "website": "",
                      "booking_link": "https://book.blastness.com/results?lingua_int=eng&id_albergo=12102&dc=3546&id_stile=224&notti_1=1"}])

    def finder(sites, ec, log=None, budget_s=0):
        assert [s["pid"] for s in sites] == ["h"]          # the others' codes are in their links: no browser needed
        return {"h": engines.code_from_requests("hotelnetsolutions", [req("hns_rateplan")])}
    fb = FakeBrowser()
    res = engines.run(cfg, "all", 5, finder=finder, today=date(2026, 11, 24), log=lambda m: None, sleep=lambda s: None,
                      post=lambda u, b: None, get=lambda u: None, browser_factory=lambda ec: fb)
    assert res["ids_found"] == 4 and res["hotels_priced"] == 4 and res["errors"] == 0 and fb.closed
    opened = dict((m, u) for u, m in fb.opened)
    assert opened["onepagebooking.com/api/RatePlan"] == "https://www.naturhotel.at/buchen/?lang=de"
    assert "hotel=71264" in opened["getProductAvailability"] and "chain=5375" in opened["getProductAvailability"]
    assert "id_albergo=12102&dc=3546" in opened["bookingengine/search"]
    hn = [b for u, b, h in fb.posts if "onepagebooking" in u]
    assert len(hn) == 13 and {b["code"] for b in hn} == {"naturhotel-chesa-valisa"} and all(b["outputType"] == 4 for b in hn)
    tpl = json.loads((FX / "hns_rateplan_request_2026-11.json").read_text())
    assert {b["sessionID"] for b in hn} == {tpl["sessionID"]}       # the page's session goes with its token
    es = [b for u, b, h in fb.posts if "ericsoft" in u]
    assert len(es) == 8 and {g["personTypeId"] for b in es for g in b["resources"][0]["guests"]} == {4182}
    assert all(h == {"x-xsrf-token": "x"} for u, b, h in fb.posts if "ericsoft" in u)
    sx = [b for u, b, h in fb.posts if "synxis" in u]
    assert len(sx) == 8 and all(b["ProductAvailabilityQuery"]["RoomStay"]["GuestCount"][0]["NumGuests"] == 2 for b in sx)
    rows = list(csv.DictReader(open(cfg["paths"]["seller_prices"])))
    by = {p: [r for r in rows if r["pid"] == p] for p in "hesb"}
    assert len(by["h"]) == 16 and by["h"][0]["seller_id"] == "hotelnetsolutions" and by["h"][0]["price"] == "495.0"
    assert {r["price"] for r in by["e"] if r["available"] == "yes"} == {"148.0"} and any(r["available"] == "no" for r in by["e"])
    assert {r["price"] for r in by["s"]} == {"254.32"} and "hotel=71264" in by["s"][0]["url"]
    assert {r["price"] for r in by["b"]} == {"212.0"} and "id_albergo=12102" in by["b"][0]["url"]


def test_synxis_and_blastness_codes_and_answers():
    assert engines.code_from_requests("synxis", ["https://be.synxis.com/?adult=2&#038;chain=5375&#038;currency=EUR&#038;hotel=71264"]) \
        == {"code": "71264", "chain": "5375", "host": "be.synxis.com"}
    assert engines.code_from_requests("synxis", ["https://reservations.villacora.it/?chain=24447&amp;hotel=6861&amp;filter=SBE"]) \
        == {"code": "6861", "chain": "24447", "host": "reservations.villacora.it"}
    assert engines.code_from_requests("synxis", ["https://gc.synxis.com/rez.aspx?Hotel=22894&Chain=8565&Lang=5"])["host"] == "be.synxis.com"
    assert engines.code_from_requests("synxis", [req("synxis_availability")]) is not None        # its own call names it
    assert engines.code_from_requests("synxis", ["https://bookings.travelclick.com/107183?hotelId=107183"]) is None
    assert engines.code_from_requests("blastness", ["https://book.blastness.com/results?lingua_int=ita&id_albergo=11153&dc=5562147&id_stile="]) \
        == {"code": "11153", "dc": "5562147"}
    n = engines.synxis_night(json.loads((FX / "synxis_availability_monte_cenci_2026-12-14.json").read_text()))
    assert n == {"price": 254.32, "min_stay": 1, "url": ""}                     # cheapest available, taxes included
    b = engines.synxis_body(json.loads((FX / "synxis_availability_request_2026-11.json").read_text()), date(2027, 2, 1), 2)
    assert b["ProductAvailabilityQuery"]["RoomStay"]["StartDate"] == "2027-02-01" and b["ProductAvailabilityQuery"]["Hotel"]["Id"] == "71264"
    assert engines.blastness_night(json.loads((FX / "blastness_search_argentina_2026-11-23.json").read_text())) == \
        {"price": 212.0, "min_stay": 1, "url": ""}                                # best available rate, VAT included
    assert engines.blastness_night({"IsSucceded": True, "Data": None}) is None


def test_codes_found_before_the_fix_are_looked_up_again_and_shared_codes_distrusted(tmp_path):
    """Regression (run 45333656075): the code finder reused one page, so the previous hotel's late calls gave the next
    hotel its code (Hotel Grossarler Hof got Hotel Valserhof's IT_VAL_VALSERHOF). Codes found before the fix are looked
    up again, a code given to two hotels is not trusted, and a hotel whose code is redone loses its old prices."""
    cfg = config.load()
    for k in ("watchlist", "official_sites", "seller_ids", "seller_prices"):
        cfg["paths"][k] = str(tmp_path / f"{k}.csv")
    with open(cfg["paths"]["watchlist"], "w", newline="") as f:
        w = csv.DictWriter(f, ["pid", "gone_since"])
        w.writeheader()
        w.writerows([{"pid": x, "gone_since": ""} for x in ("v", "g", "s")])
    with open(cfg["paths"]["official_sites"], "w", newline="") as f:
        w = csv.DictWriter(f, ["pid", "name", "engine", "booking_link", "website"])
        w.writeheader()
        w.writerows([{"pid": "v", "name": "Hotel Valserhof", "engine": "seekda", "booking_link": "https://v.example", "website": ""},
                     {"pid": "g", "name": "Hotel Grossarler Hof", "engine": "seekda", "booking_link": "https://g.example", "website": ""},
                     {"pid": "s", "name": "Hotel Steirerhof", "engine": "seekda", "booking_link": "https://s.example", "website": ""}])
    old = "2026-11-24T05:37:23+00:00"
    with open(cfg["paths"]["seller_ids"], "w", newline="") as f:
        w = csv.DictWriter(f, engines.ID_FIELDS)
        w.writeheader()
        w.writerow({"pid": "g", "seller": "official", "seller_id": 'seekda:{"code":"IT_VAL_VALSERHOF"}', "status": "ok",
                    "checked_at": old, "seller_name": "Hotel Grossarler Hof"})
    with open(cfg["paths"]["seller_prices"], "w", newline="") as f:
        w = csv.DictWriter(f, engines.PRICE_FIELDS)
        w.writeheader()
        w.writerow({"pid": "g", "seller": "official", "seller_id": "seekda", "night": "2027-01-10", "price": "99",
                    "available": "yes", "seen_at": old})
    assert {s["pid"] for s in engines.ids_due(list(csv.DictReader(open(cfg["paths"]["official_sites"]))),
            {"g": {"status": "ok", "checked_at": old}}, ["seekda"], date(2026, 11, 24), 30, cfg["engines"]["ids_valid_from"])} == {"v", "g", "s"}

    def finder(sites, ec, log=None, budget_s=0):        # two hotels "found" with one code: neither is trusted
        return {"v": {"code": "IT_VAL_VALSERHOF"}, "g": {"code": "IT_VAL_VALSERHOF"}, "s": {"code": "AT_GROSSARLER"}}
    res = engines.run(cfg, "all", 5, finder=finder, today=date(2026, 11, 24), log=lambda m: None, sleep=lambda s: None,
                      get=lambda u: Response(200, {}, b'{"result": []}'), post=lambda u, b: None)
    ids = {r["pid"]: r for r in csv.DictReader(open(cfg["paths"]["seller_ids"]))}
    assert ids["v"]["status"] == ids["g"]["status"] == "shared code" and ids["s"]["status"] == "ok"
    assert res["shared_codes"] == 2 and res["ids_found"] == 1
    assert [r for r in csv.DictReader(open(cfg["paths"]["seller_prices"])) if r["pid"] == "g"] == []
