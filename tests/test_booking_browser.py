from hotel_scraper import booking_browser as bb


def test_board_words():
    assert bb.board_of("Superior Double Room Breakfast included Free cancellation") == ["breakfast_included"]
    assert bb.board_of("Half board included · Non-refundable") == ["half_board"]
    assert bb.board_of("All-Inclusive package") == ["all_inclusive"] and bb.board_of("Room only") == []


def test_prices():
    assert bb.price_of("Original price € 1,234 Current price € 987") == 987
    assert bb.price_of("€120 €96") == 96 and bb.price_of("€ 1.050,50") == 1050.5 and bb.price_of("no price") is None
    assert bb.price_of("US$1,337") == 1337 and bb.currency_of("US$364") == "USD" and bb.currency_of("€ 90") == "EUR"
    assert bb.cancellation_of("Free cancellation before 7 December 2026") == "free cancellation"
    assert bb.board_of("Superb breakfast included Partially refundable") == ["breakfast_included"]


def test_report():
    md = bb.report_md("github-actions", [{"page": "it/x", "first_status": 202, "challenge_passed": True, "seconds": 9.1,
        "rooms": [{"block": "1", "room": "Double", "price": 120.0, "currency": "EUR", "board": ["breakfast_included"],
                   "cancellation": "free cancellation", "conditions": "Breakfast included"}],
        "calendar": {"status": 200, "days": 61, "priced": 50}}])
    assert "| it/x | 202 | yes | 9.1 | 1 | 1 | HTTP 200: 50 of 61 days priced |" in md


def test_google_probe_summary():
    from hotel_scraper.google_probe import summarize
    s = summarize("Haus Hepi  €149  Official site €152  Booking.com €149  18% less than usual  Great deal")
    assert s["prices_seen"] == 3 and s["sellers_seen"] == ["Official site", "Booking.com"]
    assert s["deal_tags"] == ["18% less than usual", "Great deal"] and not s["consent_wall"]



def test_google_ts_matches_a_real_url_and_seller_prices():
    from datetime import date
    from hotel_scraper.google_probe import encode_ts, seller_prices
    # captured from Google Hotels on 2026-11-10: 17 -> 18 Nov 2026, USD
    assert encode_ts(date(2026, 11, 17), date(2026, 11, 18), "USD") == "CAAaGhIYEhIKBwjqDxALGBESBwjqDxALGBIyAggAKgcKBToDVVNE"
    assert seller_prices("Haus Hepi\nOfficial site\n€152\nBooking.com\n€149\nfree cancellation\nAgoda\nView\n€160") == [
        ("Official site", "€152"), ("Booking.com", "€149"), ("Agoda", "€160")]
