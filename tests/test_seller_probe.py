"""Probe S0: sellers' own websites and the booking engines of official hotel sites (no network here)."""
from hotel_scraper import config, seller_probe as sp
from hotel_scraper.http import Response

CFG = config.load()

HOME = """<html><head><script src="https://www.simplebooking.it/search2/js/qs.js"></script></head><body>
<a href="/en/rooms">Rooms</a> <a href="https://www.booking.com/hotel/it/x.html">Booking.com</a>
<a href="https://www.facebook.com/x">Facebook</a> <a href="mailto:info@x.it">Mail</a>
<a class="btn" href="https://book.example-engine.net/ibe?hotel=12">Prenota ora</a></body></html>"""


def test_engine_from_scripts_and_links():
    assert sp.engine_of(HOME, "https://www.hotel-x.it/", CFG["seller_probe"]["engines"]) == ("simplebooking", "simplebooking.it")
    assert sp.engine_of("<a href='https://app.thebookingbutton.com/x'>Book</a>", "", CFG["seller_probe"]["engines"])[0] == "siteminder"
    assert sp.engine_of("<p>nothing</p>", "https://x.at/", CFG["seller_probe"]["engines"]) == ("", "")


def test_booking_links_skip_sellers_and_social_and_prefer_other_sites():
    got = sp.booking_links(HOME + '<a href="/booking">Book</a>', "https://www.hotel-x.it/", CFG["seller_probe"]["booking_words"],
                           CFG["seller_probe"]["not_engines"])
    assert got == ["https://book.example-engine.net/ibe?hotel=12", "https://www.hotel-x.it/booking"]


def test_survey_follows_the_booking_link_to_find_the_engine():
    pages = {"https://hotel-y.at": Response(200, {}, b'<a href="https://hotel-y.at/anfrage">Anfrage</a>'
                                            b'<a href="https://ibe.example.org/y">Jetzt buchen</a>', 0.1, "https://hotel-y.at/"),
             "https://ibe.example.org/y": Response(200, {}, b"<script src='https://ibe.seekda.com/x.js'></script>", 0.1,
                                                   "https://ibe.example.org/y")}
    row = {"pid": "ta:1", "name": "Y", "country": "AT", "website": "hotel-y.at"}
    out = sp.survey_site(row, CFG, fetch=lambda u: pages.get(u, Response(404, url=u)))
    assert (out["engine"], out["booking_host"], out["pages"]) == ("seekda", "ibe.example.org", 2)
    out = sp.survey_site({**row, "website": "https://gone.example"}, CFG, fetch=lambda u: Response(0, error="timed out"))
    assert out["status"].startswith("error") and out["engine"] == ""


def test_page_facts_and_blocks():
    f = sp.page_facts("Hotel Schloss Seefels\n€ 312 per night\n€ 290", "Hotels in Pörtschach", "Hotel Schloss Seefels",
                      CFG["seller_probe"]["block_phrases"])
    assert f["blocked"] == "" and f["prices"] == 2 and f["name_found"]
    assert sp.page_facts("Show us your human side", "Bot or Not?", "X", CFG["seller_probe"]["block_phrases"])["blocked"]


def test_sample_one_country_at_a_time():
    watch = [{"pid": f"ta:{i}", "country": c, "review_count": str(100 - i), "gone_since": ""}
             for i, c in enumerate(["IT", "IT", "IT", "AT", "CH", "AT"])]
    ids = {w["pid"]: {"status": "ok"} for w in watch}
    assert [w["pid"] for w in sp.sample_hotels(watch, ids, 4)] == ["ta:0", "ta:3", "ta:4", "ta:1"]


def test_report_counts():
    md = sp.report([{"seller": "expedia", "blocked": "Bot or Not", "prices": 0, "name_found": False, "json_saved": 0}],
                   [{"status": "200", "engine": "seekda", "booking_host": "", "website": "https://a.at"},
                    {"status": "200", "engine": "", "booking_host": "ibe.new.com", "website": "https://b.at"},
                    {"status": "error: timed out", "engine": "", "booking_host": "", "website": "https://c.at"}], 1500)
    assert "| expedia | 1 | 1 | 0 | 0 | 0 |" in md and "| seekda | 1 |" in md and "- ibe.new.com: 1" in md


def test_http_body_cut_off_midway_is_a_failed_response_not_a_crash(monkeypatch):
    """Regression (seller probe run 45211139318): an official site cut its chunked body off (IncompleteRead) and the
    exception stopped the whole survey."""
    import http.client
    from hotel_scraper.http import Session
    s = Session("ua")

    def boom(req, timeout=0):
        raise http.client.IncompleteRead(b"x" * 10)
    monkeypatch.setattr(s.opener, "open", boom)
    r = s.request("GET", "https://cut.example/")
    assert r.status == 0 and r.error


def test_bot_walls_seen_on_github():
    """Expedia answered 429 'Sei un robot?' and Hotels.com refused the connection (2026-11-23 probe)."""
    assert sp.page_facts("Mostraci il tuo lato umano...", "Sei un robot?", "X", CFG["seller_probe"]["block_phrases"])["blocked"]


def test_engine_url_from_the_booking_link_or_the_link_itself():
    from datetime import date
    tpl = CFG["seller_probe"]["engine_pages"]["urls"]["simplebooking"]
    row = {"booking_link": "https://www.simplebooking.it/ibe2/hotel/5715?lang=DE&amp;cur=CHF", "website": "https://margna.ch"}
    u = sp.engine_url(row, tpl, date(2026, 12, 14), 2)
    assert u.startswith("https://www.simplebooking.it/ibe2/hotel/5715?") and "in=2026-12-14" in u and "out=2026-12-15" in u
    assert sp.engine_url({"booking_link": "https://be.synxis.com/?hotel=22859&amp;adult=2"}, None, date(2026, 12, 14), 2) \
        == "https://be.synxis.com/?hotel=22859&adult=2"
    assert sp.engine_url({"booking_link": "", "website": "https://x.at"}, tpl, date(2026, 12, 14), 2) == "https://x.at"


def test_replay_asks_the_page_api_for_one_property(tmp_path):
    import json
    from types import SimpleNamespace as NS
    body = {"operationName": "priceTrendSearch", "variables": {"PriceTrendSearchRequest": {"cityId": 16594, "currency": "USD"}}}
    req = NS(post_data=json.dumps(body), headers={"content-type": "application/json", "cookie": "x", "ag-language-id": "1"})
    sent = []

    class Page:
        url = "https://www.agoda.com/en-gb/search?hotel=1081&selectedproperty=1081&checkIn=2026-12-02"

        def evaluate(self, js, args):
            sent.append(args)
            return [200, '{"data": {}}']
    rp = CFG["seller_probe"]["sellers"]["agoda"]["replay"]
    sp.replay(Page(), [NS(url="https://www.agoda.com/graphql/search", request=req)], rp, tmp_path / "agoda_x", log=lambda m: None)
    url, b, headers = sent[0]
    got = json.loads(b)["variables"]["PriceTrendSearchRequest"]
    assert url.endswith("/graphql/search") and got == {"cityId": 16594, "currency": "EUR", "propertyId": 1081}
    assert "cookie" not in headers and len(sent) == len(rp["variants"]) and (tmp_path / "agoda_x.replay0.json").exists()
