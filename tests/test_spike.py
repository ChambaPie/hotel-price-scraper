import json
from datetime import date
from pathlib import Path

import yaml

from hotel_scraper import config, spike
from hotel_scraper.http import Response

TODAY = date(2026, 11, 8)
ROOT = Path(__file__).resolve().parent.parent


def jresp(obj, status=200):
    return Response(status, {"Content-Type": "application/json"}, json.dumps(obj).encode(), 0.1, "u")


def test_block_reason():
    assert spike.block_reason(Response(0, error="timed out")).startswith("no answer")
    assert spike.block_reason(Response(202, {"x-amzn-waf-action": "challenge"}, b"")) == "AWS WAF challenge"
    assert "challenge page" in spike.block_reason(Response(200, {}, b"<script>window.gokuProps = {}</script>"))
    assert spike.block_reason(Response(202, {}, b"")) == "HTTP 202 with empty body (challenge)"
    assert spike.block_reason(Response(403, {}, b"denied")) == "HTTP 403"
    assert spike.block_reason(jresp({"ok": 1})) == ""


def test_booking_page_markers():
    html = ("b_csrf_token: 'abc123', hotelName: \"grand-duchi-d-aosta\", hotelCountry: \"it\" "
            "<tr data-block-id='1'>Breakfast included</tr><tr data-block-id='2'>Half board included</tr>")
    m = spike.booking_page_markers(html)
    assert m["csrf_token"] and m["hotel_name"] == "grand-duchi-d-aosta" and m["hotel_country"] == "it"
    assert m["room_blocks"] == 2 and m["meal_mentions"]["breakfast_included"] == 1 and m["meal_mentions"]["half_board"] == 1


def test_calendar_payload_and_summary():
    p = spike.calendar_payload("it", "x", TODAY, 61, 2, 1)
    cfg = p["variables"]["input"]["searchConfig"]
    assert p["operationName"] == "AvailabilityCalendar" and cfg["searchConfigDate"] == {"startDate": "2026-11-08", "amountOfDays": 61}
    days = [{"checkin": "2026-11-08", "available": True, "avgPriceFormatted": "€ 180", "minLengthOfStay": 1},
            {"checkin": "2026-11-09", "available": False, "avgPriceFormatted": None, "minLengthOfStay": 2}]
    sm = spike.summarize_calendar({"data": {"availabilityCalendar": {"hotelId": 1, "days": days}}})
    assert sm["days"] == 2 and sm["available"] == 1 and sm["priced"] == 1 and sm["min_stay_over_1"] == 1
    assert "error" in spike.summarize_calendar({"data": {"availabilityCalendar": {"message": "nope"}}})


def test_summarize_xotelo():
    rates = spike.summarize_xotelo("rates", {"error": None, "result": {"rates": [
        {"code": "BookingCom", "name": "Booking.com", "rate": 180.0}, {"code": "Agoda", "name": "Agoda.com", "rate": 170}]}})
    assert rates == {"sellers": 2, "names": ["Agoda.com", "Booking.com"], "min": 170, "max": 180.0}
    hm = spike.summarize_xotelo("heatmap", {"error": None, "result": {"heatmap": {
        "cheap_price_days": ["2026-12-01"], "average_price_days": ["2026-11-20", "2027-01-05"], "high_price_days": []}}})
    assert hm["days"] == 3 and hm["first"] == "2026-11-20" and hm["last"] == "2027-01-05"
    assert spike.summarize_xotelo("search", {"error": "Not available"}) == {"error": "Not available"}


def test_match_to_catalog():
    rows = [{"pid": "osm:way/202", "name": "Grand Hotel Duchi d'Aosta", "lat": "45.6503", "lon": "13.7673"}]
    items = [{"name": "Grand Hotel Duchi d'Aosta", "geo": {"latitude": 45.6504, "longitude": 13.7672}},
             {"name": "Somewhere Else", "geo": {"latitude": 45.0, "longitude": 13.0}}]
    m = spike.match_to_catalog(items, rows, 0.25, 0.6)
    assert m["matched"] == 1 and m["examples"][0]["pid"] == "osm:way/202"


class FakeSession:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, headers=None):
        self.calls.append(("GET", url, headers))
        return next(r for k, r in self.routes.items() if k in url)

    def post_json(self, url, payload, headers=None):
        self.calls.append(("POST", url, headers, payload))
        return next(r for k, r in self.routes.items() if k in url)


def make(tmp_path):
    cfg = config.load()
    cfg["paths"]["catalog"] = str(tmp_path / "none.csv")
    scfg = yaml.safe_load((ROOT / "config/spike.yaml").read_text())
    scfg["xotelo"].update(rates_offsets_days=[7, 300], heatmap_checkout_offsets_days=[90], heatmap_hotels=1, hotels_from_list=2)
    return spike.Spike(cfg, scfg, tmp_path / "spike", TODAY, sleep=lambda s: None)


XOTELO = {
    "/list?": jresp({"error": None, "result": {"total_count": 456, "list": [
        {"name": "A", "key": "g187813-d1", "accommodation_type": "Hotel", "geo": {"latitude": 45.6, "longitude": 13.7}},
        {"name": "B", "key": "g187813-d2", "accommodation_type": "B&B", "geo": {"latitude": 45.7, "longitude": 13.8}}]}}),
    "/rates?": jresp({"error": None, "result": {"rates": [{"code": "BookingCom", "name": "Booking.com", "rate": 150}]}}),
    "/heatmap?": jresp({"error": None, "result": {"heatmap": {"cheap_price_days": ["2026-12-01"]}}}),
    "/search?": jresp({"error": "This endpoint is available on RapidAPI"}),
}


def test_run_with_booking_blocked(tmp_path):
    sp = make(tmp_path)
    booking = FakeSession({"/hotel/": Response(202, {"x-amzn-waf-action": "challenge"}, b"", 0.3, "u")})
    sp.run(FakeSession(XOTELO), booking)
    kinds = [(p.source, p.kind, p.ok) for p in sp.probes]
    assert kinds.count(("xotelo", "rates", True)) == 4 and ("xotelo", "search", False) in kinds
    assert ("booking", "page", False) in kinds and not any(k[1] == "calendar" for k in kinds)   # no token, no calendar call
    md = spike.report_md("test", sp.probes, sp.extra, TODAY)
    assert "| booking | page | 1 | 0 | AWS WAF challenge |" in md
    assert "no data/catalog.csv yet" in md
    assert len(list((tmp_path / "spike" / "raw").iterdir())) == len(sp.probes)


def test_run_with_booking_open(tmp_path):
    sp = make(tmp_path)
    page = Response(200, {}, b"b_csrf_token: 'tok', hotelName: \"grand-duchi-d-aosta\", hotelCountry: \"it\"", 0.5, "u")
    cal = jresp({"data": {"availabilityCalendar": {"hotelId": 9, "days": [
        {"checkin": "2026-11-08", "available": True, "avgPriceFormatted": "€ 200", "minLengthOfStay": 1}]}}})
    booking = FakeSession({"/hotel/": page, "/dml/graphql": cal})
    sp.run(FakeSession(XOTELO), booking)
    post = next(c for c in booking.calls if c[0] == "POST")
    assert post[2]["x-booking-csrf-token"] == "tok"
    assert post[3]["variables"]["input"]["pagenameDetails"] == {"countryCode": "it", "pagename": "grand-duchi-d-aosta"}
    calp = next(p for p in sp.probes if p.kind == "calendar")
    assert calp.ok and calp.detail["priced"] == 1
