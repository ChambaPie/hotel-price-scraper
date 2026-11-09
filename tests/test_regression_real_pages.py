"""Regression tests on real responses captured from the live sources. If a parser change breaks any of these,
something that worked against the real sites has stopped working."""
import json
from pathlib import Path

from hotel_scraper import booking_browser as bb
from hotel_scraper import spike, ta_refresh
from hotel_scraper.booking_session import parse_price

FIX = Path(__file__).parent / "fixtures"


def test_booking_hotel_page_markers_and_offers():
    # Grand Hotel Duchi d'Aosta, Trieste, captured by headless Chromium on GitHub Actions, 2026-11-09
    html = (FIX / "booking_hotel_page_trimmed.html").read_text()
    m = spike.booking_page_markers(html)
    assert m["csrf_token"] and m["hotel_name"] == "grand-duchi-d-aosta" and m["hotel_country"] == "it"
    offers = bb.offers_from_html(html)
    assert [o["price"] for o in offers] == [304, 364, 425, 583, 765, 972, 1337]
    assert all(o["currency"] == "USD" and o["board"] == ["breakfast_included"] for o in offers)
    assert offers[0]["cancellation"] == "partially refundable"
    assert all(o["cancellation"] == "free cancellation" for o in offers[1:])
    assert offers[0]["room"].startswith("Superior Double or Twin Room")


def test_booking_calendar_price_formats_seen_live():
    # "364" (probe, 2026-11-09) and the abbreviated "1.1K"/"1.3K" that caused false alarm #16
    assert parse_price("364") == 364 and parse_price("1.1K") == 1100 and parse_price("1.3K") == 1300


def test_tripadvisor_list_real_trieste_page():
    items = json.loads((FIX / "xotelo_list_trieste.json").read_text())["result"]["list"]
    sav = next(i for i in items if i["key"] == "g187813-d203235")
    assert ta_refresh.ids(sav) == ("g187813", "g187813-d203235")
    p = ta_refresh.to_property(sav, "g187813", (45.6495, 13.7768), 800, {"hotel": ["Hotel"]})
    assert p.name.startswith("Savoia Excelsior Palace") and p.review_score == 4.7 and p.review_count == 2422
