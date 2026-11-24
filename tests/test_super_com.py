"""Super.com reader (hotel_scraper/super_com.py) on Super.com's real answers, captured by the seller probe
(2026-11-24): its own map search around Hotel Artemide, Rome, for 10 Feb 2027 (7 of 132 hotels kept)."""
import csv
import json
from datetime import date, datetime, timezone
from pathlib import Path

from hotel_scraper import config, super_com

FX = Path(__file__).parent / "fixtures"
MAP = json.loads((FX / "super_mapview_rome_2027-02-10.json").read_text())
REQ = json.loads((FX / "super_mapview_request_2026-11.json").read_text())


def test_map_search_answer_only_its_own_sales():
    hs = {h["id"]: h for h in super_com.results(MAP)}
    a = hs["Pjm0wZlg"]                                            # Anantara Palazzo Naiadi
    assert (a["name"], a["own"], a["available"], a["price"], a["price_excl"], a["price_total"], a["currency"]) == \
        ("Anantara Palazzo Naiadi Rome Hotel", True, True, 434.95, 380.37, 457.13, "USD")
    assert a["slug"] == "anantara-palazzo-naiadi-rome-hotel-rome" and (a["lat"], a["lon"]) == (41.90196, 12.49612)
    eden = next(h for h in hs.values() if h["name"].startswith("Hotel Eden"))
    assert eden["own"] is False and eden["available"] is False and eden["price"] is None   # not Super.com's sale


def test_search_request_like_its_own_page():
    """The page's own queryParams (probe): same keys, our hotel's place, night and currency."""
    q = super_com.query(46.6, 14.1, "Techelsberg", date(2027, 3, 1), 2, config.load()["super_com"], "x=1")
    assert set(q) == set(REQ["input"]["queryParams"])
    assert (q["checkin_at"], q["checkout_at"], q["num_adults"], q["currency"]) == ("2027-03-01", "2027-03-02", 2, "EUR")
    assert REQ["answer"]["result"]["data"]["job_id"].startswith("hss-lds:")


class FakePage:
    """Super.com's search: one area around Rome, our Artemide listed (in euros) when asked for."""
    def __init__(self, listed=True, currency="EUR"):
        self.calls, self.listed, self.currency = [], listed, currency

    def search(self, wanted):
        self.calls.append(wanted)
        doc = json.loads(json.dumps(MAP))
        for x in doc["result"]["data"]["query_results"]:
            x["rate"]["currency"] = self.currency
        if self.listed:
            x = json.loads(json.dumps(doc["result"]["data"]["query_results"][0]))
            x["hotel"].update({"id": "elXpOqlk", "name": "Hotel Artemide", "latitude": 41.90081, "longitude": 12.49361,
                               "url_slug": "hotel-artemide-rome"})
            x["rate"].update({"nightly_total_price": 230.5, "nightly_price_before_tax": 205.0, "surcharge": 6.0})
            doc["result"]["data"]["query_results"].append(x)
        return [{"hotels": super_com.results(doc), "region": "37409", "error": ""} for _ in wanted]


def _setup(tmp_path):
    cfg = config.load()
    for k in ("watchlist", "seller_ids", "seller_prices", "locations"):
        cfg["paths"][k] = str(tmp_path / f"{k}.csv")
    with open(cfg["paths"]["watchlist"], "w", newline="") as f:
        w = csv.DictWriter(f, ["pid", "name", "city", "lat", "lon", "gone_since"])
        w.writeheader()
        w.writerows([{"pid": "ta:art", "name": "Hotel Artemide", "city": "Rome", "lat": "41.9005", "lon": "12.4930",
                      "gone_since": ""},
                     {"pid": "ta:x", "name": "Unknown Inn", "city": "Rome", "lat": "41.95", "lon": "12.40", "gone_since": ""}])
    return cfg


def test_run_finds_ids_then_prices_an_area_per_search(tmp_path):
    cfg = _setup(tmp_path)
    page = FakePage()
    now = datetime(2026, 11, 24, 16, tzinfo=timezone.utc)
    res = super_com.run(cfg, "all", 20, page, today=date(2026, 11, 24), log=lambda m: None, now_dt=now)
    assert res["ids_found"] == 1 and res["ids_missed"] == 1 and res["ids_coverage"] == "1/2"
    ids = {r["pid"]: r for r in csv.DictReader(open(cfg["paths"]["seller_ids"]))}
    assert ids["ta:art"]["seller_id"] == "elXpOqlk" and ids["ta:art"]["city_id"] == "37409|hotel-artemide-rome"
    assert ids["ta:x"]["status"] == "not found" and ids["ta:x"]["seller_id"] == ""
    rows = list(csv.DictReader(open(cfg["paths"]["seller_prices"])))
    n = cfg["super_com"]["nights_per_pass"]
    assert len(rows) == n and all(r["pid"] == "ta:art" and r["available"] == "yes" for r in rows)
    assert (rows[0]["price"], rows[0]["price_excl"], rows[0]["price_total"]) == ("230.5", "205.0", "236.5")
    assert "/travel/hotels/hotel-artemide-rome/elXpOqlk?" in rows[0]["url"] and "checkin_at=" in rows[0]["url"]
    assert len(page.calls[-1]) <= cfg["super_com"]["searches_at_once"]


def test_prices_in_another_currency_or_not_listed_are_not_stored(tmp_path):
    """A night Super.com lists our hotel in dollars, or does not list it, says nothing about its price in euros."""
    cfg = _setup(tmp_path)
    super_com.run(cfg, "ids", 20, FakePage(), today=date(2026, 11, 24), log=lambda m: None)
    for page in (FakePage(currency="USD"), FakePage(listed=False)):
        res = super_com.run(cfg, "prices", 20, page, today=date(2026, 11, 24), log=lambda m: None)
        assert res["priced"] == 0
        assert list(csv.DictReader(open(cfg["paths"]["seller_prices"]))) == []
