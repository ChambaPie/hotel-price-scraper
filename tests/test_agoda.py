"""Agoda's own prices: real captured answers (seller probe, 2026-11-23) and a fake page for the run."""
import csv
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from hotel_scraper import agoda, config

FX = Path(__file__).parent / "fixtures"
CITY = json.loads((FX / "agoda_citysearch_rome_2026-12-14.json").read_text())
REQUEST = json.loads((FX / "agoda_citysearch_request_2026-11.json").read_text())


def test_prices_from_a_real_city_search():
    props = {p["id"]: p for p in agoda.properties(CITY)}
    a = props[1081]
    assert a["name"] == "Hotel Artemide" and a["city_id"] == 16594 and a["available"]
    assert (a["price_excl"], a["price"], a["price_total"], a["currency"]) == (170.95, 188.05, 203.05, "EUR")
    assert a["cancellation"] == "NonRefundable" and a["rooms_left"] == 2
    assert a["lat"] == pytest.approx(41.9009, abs=1e-3)
    gone = props[66055]                                   # Hotel Fiori: not available that night
    assert not gone["available"] and gone["price"] is None
    assert all(p["price"] for p in props.values() if p["available"])


def test_city_search_request_for_another_city_and_night_with_our_hotels_pinned():
    b = agoda.city_search_body(REQUEST, 7401, date(2027, 2, 3), [11, 22], "EUR", 2,
                               now=datetime(2026, 11, 24, 8, 0, tzinfo=timezone.utc))
    csr = b["variables"]["CitySearchRequest"]
    sc = csr["searchRequest"]["searchCriteria"]
    assert csr["cityId"] == 7401 and sc["localCheckInDate"] == "2027-02-03" and sc["checkInDate"] == "2027-02-03T00:00:00.000Z"
    assert csr["searchRequest"]["extraHotels"]["extraHotelIds"] == [11, 22] and csr["searchRequest"]["page"]["pageSize"] == 5
    pr = b["variables"]["PricingSummaryRequest"]["pricing"]
    assert (pr["localCheckInDate"], pr["localCheckoutDate"], pr["currency"], pr["mseHotelIds"]) == ("2027-02-03", "2027-02-04", "EUR", [11, 22])
    assert b["variables"]["ContentSummaryRequest"]["context"]["searchCriteria"] == {"cityId": 7401}
    assert REQUEST["variables"]["CitySearchRequest"]["cityId"] == 16594          # the template is left as it was
    assert b["query"] == REQUEST["query"]


def test_match_checks_name_and_place():
    ac = config.load()["agoda"]
    props = agoda.properties(CITY)
    w = {"name": "Hotel Artemide", "lat": "41.9005", "lon": "12.4930"}
    m = agoda.match(w, "https://www.agoda.com/en-gb/search?city=16594&selectedproperty=1081", props, ac)
    assert m["ok"] and m["id"] == 1081 and m["km"] < 0.1
    far = agoda.match({**w, "lat": "45.65", "lon": "13.77"}, "https://x/?selectedproperty=1081", props, ac)
    assert not far["ok"]                                                            # same name, other city
    other = agoda.match({**w, "name": "Grand Hotel Plaza"}, "https://x/?selectedproperty=1081", props, ac)
    assert not other["ok"]                                                          # nearby, another hotel
    assert agoda.match(w, "https://x/?city=16594", props, ac) is None               # Agoda pinned nothing
    # regression (run 45250329826): the same spot, Agoda's longer name ("Hotel Gesser Sillian Hochpustertal")
    near = agoda.match({"name": "Hotel", "lat": "41.9009", "lon": "12.4935"}, "https://x/?selectedproperty=1081", props, ac)
    assert not near["ok"]                                                            # a generic stub is not a name
    longer = agoda.match({"name": "Artemide", "lat": "41.9009", "lon": "12.4935"}, "https://x/?selectedproperty=1081", props, ac)
    assert longer["ok"] and agoda.contains("Hotel Gesser", "Hotel Gesser Sillian Hochpustertal")


def test_nights_walk_the_whole_horizon():
    state, today, seen = {}, date(2026, 11, 24), set()
    for _ in range(182):
        seen.update(agoda.nights_for("16594", state, today, 730, 4, 182))
    assert len(seen) == 728 and min(seen) == today                                   # 4 x 182 nights in 182 passes
    first = agoda.nights_for("1", {}, today, 730, 4, 182)
    assert [(n - today).days for n in first] == [0, 182, 364, 546]
    assert [(n - today).days for n in agoda.nights_for("1", {}, today, 730, 2, 182, min_lead=1)] == [1, 183]


def test_parallel_id_jobs_merge(tmp_path):
    f = ["pid", "seller", "seller_id", "city_id", "seller_name", "lat", "lon", "km", "similarity", "status", "checked_at"]
    def write(name, rows):
        with open(tmp_path / name, "w", newline="") as fh:
            w = csv.DictWriter(fh, f)
            w.writeheader()
            w.writerows([{k: r.get(k, "") for k in f} for r in rows])
        return str(tmp_path / name)
    a = write("a.csv", [{"pid": "p1", "seller": "agoda", "seller_id": "1", "status": "ok", "checked_at": "2026-11-23T10:00"},
                        {"pid": "p2", "seller": "agoda", "status": "not found", "checked_at": "2026-11-23T10:00"}])
    b = write("b.csv", [{"pid": "p1", "seller": "agoda", "status": "not found", "checked_at": "2026-11-24T10:00"},
                        {"pid": "p2", "seller": "agoda", "seller_id": "2", "status": "ok", "checked_at": "2026-11-23T09:00"}])
    out = str(tmp_path / "ids.csv")
    assert agoda.merge_ids([a, b], out) == 2
    got = {r["pid"]: r for r in csv.DictReader(open(out))}
    assert got["p1"]["seller_id"] == "1" and got["p2"]["seller_id"] == "2"         # a found id beats a later miss
    assert {agoda.shard_of(p, 3) for p in ("ta:1", "ta:2", "ta:3", "ta:4", "ta:5", "ta:6")} <= {0, 1, 2}


def test_merge_keeps_latest_and_drops_past():
    old = [{"pid": "a", "night": "2026-11-20", "price": "1"}, {"pid": "a", "night": "2026-12-01", "price": "2"}]
    new = [{"pid": "a", "night": "2026-12-01", "price": "3"}]
    assert agoda.merge_prices(old, new, date(2026, 11, 24)) == [{"pid": "a", "night": "2026-12-01", "price": "3"}]


class FakePage:
    def __init__(self):
        self.template, self.searches, self.calls = None, [], []

    def search(self, q):
        self.searches.append(q)
        self.template = REQUEST
        if q.startswith("Hotel Artemide"):
            return "https://www.agoda.com/en-gb/search?selectedproperty=1081&city=16594", agoda.properties(CITY)
        return "https://www.agoda.com/en-gb/search?city=1", []

    def city_prices(self, city_id, night, ids, adults):
        self.calls.append((city_id, night, tuple(ids)))
        return agoda.properties(CITY)


def test_run_finds_ids_then_prices_nights(tmp_path):
    cfg = config.load()
    for k in ("watchlist", "seller_ids", "seller_prices", "agoda_state", "locations"):
        cfg["paths"][k] = str(tmp_path / f"{k}.csv")
    with open(cfg["paths"]["watchlist"], "w", newline="") as f:
        w = csv.DictWriter(f, ["pid", "name", "city", "lat", "lon", "gone_since"])
        w.writeheader()
        w.writerows([{"pid": "ta:art", "name": "Hotel Artemide", "city": "Rome", "lat": "41.9005", "lon": "12.4930", "gone_since": ""},
                     {"pid": "ta:x", "name": "Unknown Inn", "city": "Nowhere", "lat": "46", "lon": "13", "gone_since": ""}])
    page = FakePage()
    res = agoda.run(cfg, "all", 5, page, today=date(2026, 11, 24), log=lambda m: None, sleep=lambda s: None)
    assert res["ids_found"] == 1 and res["ids_missed"] == 1 and res["ids_coverage"] == "1/2"
    assert len(page.calls) == 4 and {c[2] for c in page.calls} == {(1081,)} and page.calls[0][0] == 16594
    rows = list(csv.DictReader(open(cfg["paths"]["seller_prices"])))
    assert len(rows) == 4 and rows[0]["seller"] == "agoda" and rows[0]["price"] == "188.05" and rows[0]["available"] == "yes"
    assert "hid=1081" in rows[0]["url"] and "checkin=2026-11-25" in rows[0]["url"]   # tonight skipped (min_lead_days)
    ids = {r["pid"]: r for r in csv.DictReader(open(cfg["paths"]["seller_ids"]))}
    assert ids["ta:art"]["seller_id"] == "1081" and ids["ta:x"]["status"] == "not found"
    page2 = FakePage()                                                               # next pass: ids reused, next nights
    agoda.run(cfg, "all", 5, page2, today=date(2026, 11, 24), log=lambda m: None, sleep=lambda s: None)
    # a mapped hotel's search, not a city's, gives the request to repeat (regression: run 45253285417, HTTP 502)
    assert page2.searches == ["Hotel Artemide Rome"] and page2.calls[0][1] == date(2026, 11, 26)
