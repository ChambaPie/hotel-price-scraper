"""Who sells each hotel, from Google Hotels' pages (real captures): names only, never Google's prices."""
import csv
from datetime import date
from pathlib import Path

from hotel_scraper import config, google_vendors as gv

FX = Path(__file__).parent / "fixtures"
CFG = config.load()


def test_prices_page_lists_every_vendor_and_the_official_site():
    text = (FX / "google_hotels_prices_palazzo_baj_2026-11.txt").read_text(encoding="utf-8")
    vendors, official = gv.vendors_from_text(text, CFG["google_vendors"]["known"])
    assert official and vendors[:2] == ["Expedia.com", "Booking.com"]          # the featured part first
    assert {"Official site", "Hotels.com", "eDreams", "dealbase.com", "Bluepillow.com", "müv AI", "Best Hotel Deals"} <= set(vendors)
    assert not any("Room" in v or "Suite" in v for v in vendors)               # rooms of a vendor are not vendors
    assert "Trastevere Roma | UNA Esperienze" not in vendors                    # "similar hotels" left out


def test_overview_page_vendors():
    text = (FX / "google_hotels_entity_posthotel_achenkirch_2026-11.txt").read_text(encoding="utf-8")
    vendors, official = gv.vendors_from_text(text, CFG["google_vendors"]["known"])
    assert vendors == ["Expedia.com", "Booking.com", "Bookhotel.direct"] and not official


def test_canonical_names():
    a = CFG["google_vendors"]["aliases"]
    assert gv.canonical("Expedia.com", a) == "Expedia" and gv.canonical("Bookhotel.direct", a) == "Bookhotel.direct"


def test_run_rolls_and_counts(tmp_path):
    cfg = config.load()
    for k in ("watchlist", "hotel_vendors"):
        cfg["paths"][k] = str(tmp_path / f"{k}.csv")
    with open(cfg["paths"]["watchlist"], "w", newline="") as f:
        w = csv.DictWriter(f, ["pid", "name", "city", "gone_since"])
        w.writeheader()
        w.writerows([{"pid": "a", "name": "Palazzo Baj", "city": "Rome", "gone_since": ""},
                     {"pid": "b", "name": "Nowhere Inn", "city": "X", "gone_since": ""}])
    text = (FX / "google_hotels_prices_palazzo_baj_2026-11.txt").read_text(encoding="utf-8")
    pages = {"Palazzo Baj": (text, "Palazzo Baj in Trastevere", "https://www.google.com/travel/hotels/entity/x",
                             "https://www.palazzobaj.com/"), "Nowhere Inn": ("", "", "", "")}
    res = gv.run(cfg, 5, lambda w: pages[w["name"]], today=date(2026, 11, 24), log=lambda m: None, sleep=lambda s: None)
    assert res["coverage"] == "1/2" and res["vendors"]["Expedia"] == 1 and res["with_official_site"] == 1
    rows = {r["pid"]: r for r in csv.DictReader(open(cfg["paths"]["hotel_vendors"]))}
    assert rows["a"]["website"] == "https://www.palazzobaj.com/" and rows["b"]["status"] == "not found"
    assert gv.run(cfg, 5, lambda w: pages[w["name"]], today=date(2026, 11, 25), log=lambda m: None,
                  sleep=lambda s: None)["due"] == 0


def test_fewer_options_button_is_not_a_vendor():
    """Regression (run 45271962537): once the list is opened up, its "Fewer options" button was counted as a vendor."""
    text = (FX / "google_hotels_prices_palazzo_baj_2026-11.txt").read_text(encoding="utf-8")
    text = text.replace("* \nThis price was customised", "Fewer options\n* \nThis price was customised")
    vendors, _ = gv.vendors_from_text(text, CFG["google_vendors"]["known"])
    assert "Fewer options" not in vendors and "Bluepillow.com" in vendors


def test_no_sellers_that_night_tries_other_nights(tmp_path):
    """Run 45271962537: most small hotels showed no sellers for the night asked ("availability for a 3-night stay",
    Haus Patricia); similar hotels' sellers on that page are not this hotel's."""
    miss = (FX / "google_hotels_prices_no_vendors_3night_2026-11.txt").read_text(encoding="utf-8")
    assert gv.vendors_from_text(miss, CFG["google_vendors"]["known"]) == ([], False)
    cfg = config.load()
    for k in ("watchlist", "hotel_vendors"):
        cfg["paths"][k] = str(tmp_path / f"{k}.csv")
    with open(cfg["paths"]["watchlist"], "w", newline="") as f:
        w = csv.DictWriter(f, ["pid", "name", "city", "gone_since"])
        w.writeheader()
        w.writerow({"pid": "p", "name": "Haus Patricia", "city": "Lofer", "gone_since": ""})
    text = (FX / "google_hotels_prices_palazzo_baj_2026-11.txt").read_text(encoding="utf-8")
    asked = []

    def open_fn(w, off=None):
        asked.append(off)
        return (miss if off is None else text), "Haus Patricia", "https://www.google.com/travel/hotels/entity/p", ""
    res = gv.run(cfg, 5, open_fn, today=date(2026, 11, 24), log=lambda m: None, sleep=lambda s: None)
    assert asked == [None, CFG["google_vendors"]["retry_offsets_days"][0]] and res["coverage"] == "1/1"
