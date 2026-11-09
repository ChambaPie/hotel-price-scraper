import csv

import pytest

from hotel_scraper import config, landscape as ls, selection

LC = config.load()["landscape"]


def test_features_from_overpass():
    els = [
        {"type": "way", "tags": {"natural": "coastline"}, "geometry": [{"lat": 45.6, "lon": 13.7 + i / 100} for i in range(9)]},
        {"type": "relation", "tags": {"natural": "water", "water": "lake", "name": "Lago"},
         "members": [{"geometry": [{"lat": 46.0, "lon": 14.0}, {"lat": 46.0, "lon": 14.01}]}]},
        {"type": "way", "tags": {"natural": "beach"}, "center": {"lat": 45.61, "lon": 13.75}},
        {"type": "node", "tags": {"place": "city", "name": "Trieste"}, "lat": 45.65, "lon": 13.77},
        {"type": "way", "tags": {"landuse": "winter_sports"}, "center": {"lat": 46.5, "lon": 13.6}},
    ]
    f = ls.features_from_overpass(els, step=3)
    assert len(f["sea"]) == 3 and len(f["lake"]) == 1 and f["beach"] == [(45.61, 13.75)] and len(f["city"]) == 1 and len(f["ski"]) == 1


def test_grid_nearest():
    g = ls.Grid([(45.6, 13.7), (46.0, 14.0)])
    assert 0 < g.nearest_km(45.61, 13.7, 5) < 1.2 and g.nearest_km(45.0, 13.0, 5) is None


def test_tags():
    assert ls.tags_for({"elevation_m": "1200", "ski_km": "", "sea_km": ""}, LC) == "mountain"
    assert ls.tags_for({"elevation_m": "300", "ski_km": "3", "lake_km": "0.5"}, LC) == "mountain,ski,lake"
    assert ls.tags_for({"elevation_m": "5", "sea_km": "0.3", "beach_km": "0.4", "city_km": "20"}, LC) == "sea,beach"
    assert ls.tags_for({"elevation_m": "200"}, LC) == "countryside"


def test_selection_by_landscape():
    rule = {"property_type": "hotel", "landscape": ["sea", "lake"]}
    assert selection.rule_matches({"kind": "hotel", "landscape": "sea,beach"}, rule)
    assert not selection.rule_matches({"kind": "hotel", "landscape": "mountain"}, rule)
    assert not selection.rule_matches({"kind": "hotel", "landscape": ""}, rule)


def test_run_with_fakes(tmp_path):
    cfg = config.load()
    cfg["paths"].update(catalog=str(tmp_path / "catalog.csv"), landscape=str(tmp_path / "land.csv"),
                        landscape_tiles=str(tmp_path / "lt.json"))
    with open(cfg["paths"]["catalog"], "w", newline="") as f:
        w = csv.DictWriter(f, ["pid", "lat", "lon", "gone_since"]); w.writeheader()
        w.writerow({"pid": "ta:g1-d1", "lat": "45.6495", "lon": "13.7768", "gone_since": ""})   # Trieste waterfront
        w.writerow({"pid": "ta:g1-d2", "lat": "46.50", "lon": "13.60", "gone_since": ""})       # Alps
    feats = {"elements": [
        {"type": "way", "tags": {"natural": "coastline"}, "geometry": [{"lat": 45.649, "lon": 13.77}] * 3},
        {"type": "way", "tags": {"landuse": "winter_sports"}, "center": {"lat": 46.51, "lon": 13.61}}]}
    res = ls.run(cfg, 5, fetch=lambda q: feats, elev=lambda pts: [2 if p[0] < 46 else 1500 for p in pts],
                 log=lambda m: None)
    tags = {r["pid"]: r["tags"] for r in csv.DictReader(open(cfg["paths"]["landscape"]))}
    assert tags["ta:g1-d1"] == "sea" and tags["ta:g1-d2"] == "mountain,ski"
    assert res["elevations"] == 2 and res["tiles_done"] >= 2
