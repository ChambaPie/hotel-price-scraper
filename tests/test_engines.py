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
        w.writerows([{"pid": "a", "gone_since": ""}, {"pid": "b", "gone_since": ""}, {"pid": "c", "gone_since": ""}])
    with open(cfg["paths"]["official_sites"], "w", newline="") as f:
        w = csv.DictWriter(f, ["pid", "name", "engine", "booking_link", "website"])
        w.writeheader()
        w.writerows([{"pid": "a", "name": "Adler Inn", "engine": "seekda", "booking_link": "https://adlerinn.officialbookings.com", "website": ""},
                     {"pid": "b", "name": "Domaine", "engine": "d_edge", "booking_link": "https://domaine.example/booking", "website": ""},
                     {"pid": "c", "name": "Other", "engine": "simplebooking", "booking_link": "https://x", "website": ""}])
    seek = (FX / "seekda_calendar_adler_inn_2026-12.json").read_bytes()
    dedge = (FX / "dedge_bestprice_domaine_montagne_2026-12.json").read_bytes()

    def finder(sites, ec, log=None, budget_s=0):
        assert {s["pid"] for s in sites} == {"a", "b"}                               # only engines with a reader
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
    res = engines.run(cfg, "all", 5, get=get, finder=finder, today=date(2026, 11, 24), log=lambda m: None, sleep=lambda s: None)
    assert res["ids_found"] == 2 and res["hotels_priced"] == 2 and res["ids_coverage"].startswith("2/2")
    rows = list(csv.DictReader(open(cfg["paths"]["seller_prices"])))
    a = [r for r in rows if r["pid"] == "a"]
    assert len(a) == 19 and a[0]["seller"] == "official" and a[0]["seller_id"] == "seekda" and a[0]["price"] == "354.0"
    assert "skd-checkin=2026-12-01" in a[0]["url"] and "skd-property-code=AT_TUX_BERGLAND" in a[0]["url"]
    b = {r["night"]: r for r in rows if r["pid"] == "b"}
    assert b["2026-12-01"]["price"] == "230.0" and "secure-hotel-booking.com" in b["2026-12-01"]["url"]
    assert sum(1 for u in calls if "seekda" in u) == 13                              # a month per call, 12 months ahead
