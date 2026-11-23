"""Hotel positions: a small store reused by every run; moved only on strong evidence; no hotels in the sea."""
import csv
from datetime import date

import pytest

from hotel_scraper import config, locations as L, places

TODAY = date(2026, 11, 23)


def _csv(path, rows, fields=None):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fields or list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


@pytest.fixture
def cfg(tmp_path):
    c = config.load()
    for k in ("watchlist", "osm_catalog", "locations", "location_fixes", "places"):
        c["paths"][k] = str(tmp_path / f"{k}.csv")
    w = {"gone_since": "", "city": "Trieste", "osm_pid": ""}
    _csv(c["paths"]["watchlist"], [
        {**w, "pid": "ta:link", "name": "Hotel Riviera", "lat": "45.70", "lon": "13.70", "osm_pid": "osm:node/1"},
        {**w, "pid": "ta:same", "name": "Albergo Alla Posta", "lat": "45.6500", "lon": "13.7700"},
        {**w, "pid": "ta:stub", "name": "Palazzo Veneziano", "lat": "45.4305", "lon": "12.3229"},
        {**w, "pid": "ta:sea", "name": "Villa Mare Blu", "lat": "45.6000", "lon": "13.6000"},
        {**w, "pid": "ta:none", "name": "Casa Nessuno", "lat": "45.80", "lon": "13.90"},
        {**w, "pid": "ta:gone", "name": "Gone", "lat": "1", "lon": "1", "gone_since": "2026-01-01"}])
    o = {"kind": "hotel", "stars": "", "website": ""}
    _csv(c["paths"]["osm_catalog"], [
        {**o, "pid": "osm:node/1", "name": "Riviera", "lat": "45.7004", "lon": "13.7003"},
        {**o, "pid": "osm:node/2", "name": "Albergo alla Posta", "lat": "45.6510", "lon": "13.7712"},   # 150 m
        {**o, "pid": "osm:node/3", "name": "B&B Venezia", "lat": "45.4270", "lon": "12.3604"},          # another place
        {**o, "pid": "osm:node/4", "name": "Villa Mare Blu", "lat": "45.6150", "lon": "13.6100"}])     # 1.8 km, on land
    _csv(c["paths"]["location_fixes"], [{"pid": "x", "lat": "", "lon": "", "note": ""}])
    _csv(c["paths"]["places"], [{"pid": "ta:sea", "lat": "45.6000", "lon": "13.6000", "coast_m": "900", "coast_side": "water"}])
    return c


def photon_none(w):
    return []


def go(cfg, search=photon_none, today=TODAY):
    return L.run(cfg, 5, search=search, today=today, log=lambda m: None, sleep=lambda s: None)


def rows(cfg):
    return {r["pid"]: r for r in csv.DictReader(open(cfg["paths"]["locations"], encoding="utf-8"))}


def test_sources_and_status(cfg):
    cfg["locations"]["name_match_km"] = 3
    res = go(cfg)
    r = rows(cfg)
    assert set(r) == {"ta:link", "ta:same", "ta:stub", "ta:sea", "ta:none"}            # gone hotels left out
    assert (r["ta:link"]["source"], r["ta:link"]["status"]) == ("osm", "confirmed")
    assert (r["ta:same"]["source"], r["ta:same"]["lat"]) == ("osm", "45.651")            # the building itself
    assert (r["ta:stub"]["source"], r["ta:stub"]["status"]) == ("listing", "listing only")   # "Venezia" is not it
    assert (r["ta:sea"]["source"], r["ta:sea"]["status"], r["ta:sea"]["lat"]) == ("osm", "corrected", "45.615")
    assert (r["ta:none"]["status"]) == "listing only"
    assert res["coverage"] == "5/5" and res["status"]["corrected"] == 1


def test_a_name_match_alone_does_not_move_a_hotel_on_land(cfg):
    cfg["locations"]["name_match_km"] = 3
    _csv(cfg["paths"]["places"], [{"pid": "ta:sea", "lat": "45.6000", "lon": "13.6000", "coast_m": "", "coast_side": "land"}])
    go(cfg)
    r = rows(cfg)["ta:sea"]
    assert (r["source"], r["status"], r["osm_name"]) == ("listing", "check", "Villa Mare Blu")


def test_store_is_reused_and_only_changes_are_looked_at_again(cfg):
    calls = []

    def search(w):
        calls.append(w["pid"])
        return []
    go(cfg, search)
    assert set(calls) == {"ta:stub", "ta:sea", "ta:none"}                     # OpenStreetMap settled the others
    calls.clear()
    assert go(cfg, search)["due"] == 0 and calls == []                        # nothing to find again
    w = list(csv.DictReader(open(cfg["paths"]["watchlist"], encoding="utf-8")))
    w[4]["lat"] = "45.81"                                                     # the listing moved
    w.append({**w[4], "pid": "ta:new", "name": "Hotel Nuovo"})                # a new hotel
    _csv(cfg["paths"]["watchlist"], w)
    res = go(cfg, search)
    assert res["due"] == 2 and set(calls) == {"ta:none", "ta:new"}
    assert go(cfg, search, today=date(2027, 1, 1))["due"] == 3                # listing-only hotels retried after 30 days


def test_photon_same_name_place_to_stay_confirms_or_two_sources_move(cfg):
    cfg["locations"]["name_match_km"] = 3

    def search(w):
        if w["pid"] == "ta:none":
            return [{"properties": {"name": "Casa Nessuno", "osm_key": "tourism"}, "geometry": {"coordinates": [13.9004, 45.8003]}},
                    {"properties": {"name": "Casa Nessuno", "osm_key": "highway"}, "geometry": {"coordinates": [13.0, 45.0]}}]
        if w["pid"] == "ta:sea":   # Photon and OpenStreetMap agree on a position 1.8 km from the listing
            return [{"properties": {"name": "Villa Mare Blu", "osm_key": "tourism"}, "geometry": {"coordinates": [13.6102, 45.6151]}}]
        return []
    _csv(cfg["paths"]["places"], [{"pid": "x", "lat": "", "lon": "", "coast_m": "", "coast_side": ""}])
    go(cfg, search)
    r = rows(cfg)
    assert (r["ta:none"]["source"], r["ta:none"]["status"], r["ta:none"]["lat"]) == ("photon", "confirmed", "45.8003")
    assert (r["ta:sea"]["source"], r["ta:sea"]["status"]) == ("osm", "corrected")


def test_manual_fix_wins(cfg):
    _csv(cfg["paths"]["location_fixes"], [{"pid": "ta:link", "lat": "45.71", "lon": "13.71", "note": "checked on the map"}])
    go(cfg)
    r = rows(cfg)["ta:link"]
    assert (r["source"], r["status"], r["lat"]) == ("manual", "manual", "45.71")
    assert L.positions(cfg)["ta:link"] == ("45.71", "13.71")


def test_similarity_is_whole_name():
    assert L.similarity("Hotel Albergo Alla Posta", "Albergo alla Posta") == 1.0
    assert L.similarity("Palazzo Veneziano", "B&B Venezia") < 0.85
    assert L.similarity("Hotel Eden Roc Ascona", "Ascona") < 0.85


def test_coast_side_follows_openstreetmap_direction():
    # coastline drawn northwards: land on the left (west), water on the right (east)
    c = places.Coast([[(45.60, 13.70), (45.61, 13.70), (45.62, 13.70)]])
    assert c.side(45.61, 13.69, 3) == {"coast_m": 779, "coast_side": "land"}
    assert c.side(45.61, 13.71, 3)["coast_side"] == "water"
    assert c.side(45.61, 13.80, 3) == {"coast_m": "", "coast_side": ""}       # no coastline within 3 km
    island = places.Coast([[(45.0, 13.0), (45.0, 13.1), (45.1, 13.1), (45.1, 13.0), (45.0, 13.0)]])   # anticlockwise
    assert island.side(45.05, 13.05, 10)["coast_side"] == "land" and island.side(45.05, 13.15, 10)["coast_side"] == "water"
