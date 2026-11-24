"""eDreams reader (hotel_scraper/edreams.py) on eDreams' real answers, captured by the seller probe (2026-11-24): its
search-box suggestions for Hotel Artemide (Rome) and Hotel Schloss Seefels, Artemide's position, and its hotel search
around Artemide for 10 Feb 2027 (6 of 180 hotels kept)."""
import csv
import json
from datetime import date, datetime, timezone
from pathlib import Path

from hotel_scraper import config, edreams

FX = Path(__file__).parent / "fixtures"
SUG = json.loads((FX / "edreams_autocomplete_artemide_2026-11.json").read_text())
SUG2 = json.loads((FX / "edreams_autocomplete_seefels_2026-11.json").read_text())
LOC = json.loads((FX / "edreams_location_artemide_2026-11.json").read_text())
SEARCH = json.loads((FX / "edreams_search_artemide_2027-02-10.json").read_text())
CFG = config.load()


def test_suggestions_and_position():
    assert edreams.suggestions(SUG) == [{"id": "46539", "name": "Hotel Artemide", "where": "Repubblica, Roma, Lazio, Italia"}]
    assert edreams.suggestions(SUG2)[0]["id"] == "39890"
    assert edreams.position(LOC) == (41.900403, 12.49278)


def test_search_prices_our_hotel_and_its_neighbours():
    o = edreams.offers(SEARCH)
    a = o["46539"]                                                    # our hotel comes first
    assert (a["name"], a["price"], a["breakfast"], a["cancellation"], a["taxes"]) == \
        ("Hotel Artemide", 205.02, "", "", "TAXES_INCLUDED")
    assert round(a["lat"], 4) == 41.9008
    valle = o["68"]                                                   # a neighbour, with breakfast, free cancellation
    assert (valle["name"], valle["price"], valle["breakfast"], valle["cancellation"]) == \
        ("Hotel Valle", 76.4, "breakfast", "free cancellation")
    assert len(o) == 6


def test_search_request_like_its_results_page():
    t = json.loads(Path(CFG["edreams"]["search_template"]).read_text())
    b = edreams.search_body(t, "39890", date(2027, 3, 1), 2)
    r = b["variables"]["accommodationSearchRequest"]
    assert (r["checkInDate"], r["checkOutDate"], r["geoAccommodation"]) == \
        ("2027-03-01", "2027-03-02", {"geoId": "39890", "geoType": "ACCOMMODATION"})
    assert r["roomRequests"] == [{"numAdults": 2, "childrenAges": []}] and "dealsRecommendedId" not in r
    assert b["operationName"] == "searchAccommodation" and "searchAccommodation(" in b["query"]
    assert t["variables"]["accommodationSearchRequest"]["geoAccommodation"]["geoId"] == "46539"   # template untouched
    loc = edreams.location_body(json.loads(Path(CFG["edreams"]["location_template"]).read_text()), "39890")
    assert loc["variables"]["geoAccommodationLocationRequest"]["geoId"] == "39890"


class FakePage:
    def __init__(self):
        self.searches = []

    def suggest(self, q):
        return edreams.suggestions(SUG) if "Artemide" in q else []

    def position(self, geo_id):
        return edreams.position(LOC)

    def search(self, wanted):
        self.searches.append(wanted)
        return [{"offers": edreams.offers(SEARCH), "error": ""} for _ in wanted]


def _setup(tmp_path, rows):
    cfg = config.load()
    for k in ("watchlist", "seller_ids", "seller_prices", "locations"):
        cfg["paths"][k] = str(tmp_path / f"{k}.csv")
    with open(cfg["paths"]["watchlist"], "w", newline="") as f:
        w = csv.DictWriter(f, ["pid", "name", "city", "lat", "lon", "gone_since"])
        w.writeheader()
        w.writerows(rows)
    return cfg


def test_run_finds_ids_then_prices_with_neighbours(tmp_path):
    cfg = _setup(tmp_path, [
        {"pid": "ta:art", "name": "Hotel Artemide", "city": "Rome", "lat": "41.9005", "lon": "12.4930", "gone_since": ""},
        {"pid": "ta:x", "name": "Unknown Inn", "city": "Nowhere", "lat": "46", "lon": "13", "gone_since": ""}])
    with open(cfg["paths"]["seller_ids"], "w", newline="") as f:      # a neighbour mapped before
        w = csv.DictWriter(f, edreams.ID_FIELDS)
        w.writeheader()
        w.writerow({"pid": "ta:valle", "seller": "edreams", "seller_id": "68", "seller_name": "Hotel Valle",
                    "status": "ok", "checked_at": "2026-11-20T00:00:00+00:00"})
    page = FakePage()
    now = datetime(2026, 11, 24, 16, tzinfo=timezone.utc)
    res = edreams.run(cfg, "all", 20, page, today=date(2026, 11, 24), log=lambda m: None, now_dt=now)
    assert res["ids_found"] == 1 and res["ids_missed"] == 1
    ids = {r["pid"]: r for r in csv.DictReader(open(cfg["paths"]["seller_ids"]))}
    assert ids["ta:art"]["seller_id"] == "46539" and ids["ta:art"]["status"] == "ok"
    rows = list(csv.DictReader(open(cfg["paths"]["seller_prices"])))
    n = cfg["edreams"]["nights_per_pass"]
    art = [r for r in rows if r["pid"] == "ta:art"]
    valle = [r for r in rows if r["pid"] == "ta:valle"]
    assert len(art) >= n and art[0]["price"] == "205.02" and art[0]["available"] == "yes"
    assert "geoId=46539%2BACCOMMODATION" in art[0]["url"] and "checkIn=" in art[0]["url"]
    assert valle and valle[0]["price"] == "76.4" and valle[0]["cancellation"] == "free cancellation"
    # a night already priced by the search around the neighbour is not searched again
    searched = [(g, nt) for call in page.searches for g, nt, _ in call]
    assert len(searched) == len(set(nt for _, nt in searched))


def test_calls_accept_what_its_page_accepts():
    """Regression (run 45429732636): eDreams' suggestion service answered HTTP 406 "No match for accept header" to
    'accept: application/json' alone (every hotel failed). The page's own value is sent."""
    assert "'accept': 'application/json, text/plain, */*'" in edreams.CALLS_JS


def test_position_from_its_search_when_its_position_call_fails():
    """Regression (run 45435697486): eDreams' geoAccommodationLocation answered HTTP 500 for 12 suggested hotels (each
    counted as an error, ending the pass). The hotel search around that hotel gives its coordinates instead."""
    class Failing(FakePage):
        def position(self, geo_id):
            raise RuntimeError('eDreams HTTP 500: {"errors":[{"message":"Internal server error"}]}')
    page = Failing()
    w = {"name": "Hotel Artemide", "city": "Rome", "lat": "41.9005", "lon": "12.4930"}
    stats = {"errors": 0}
    m = edreams.find_id(page, w, CFG["edreams"], stats, log=lambda m: None, night=date(2027, 2, 10))
    assert m["ok"] and m["id"] == "46539" and stats["errors"] == 0
    assert page.searches == [[("46539", date(2027, 2, 10), 2)]]
