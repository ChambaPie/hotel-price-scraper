import json

from hotel_scraper import config, discover_osm as d
from conftest import FIXTURE

TRIESTE = (45.6495, 13.7768)
TYPES = config.load()["universe"]["property_types"]


def props():
    return d.parse_elements(json.loads(FIXTURE.read_text())["elements"], *TRIESTE, 700, TYPES)


def test_parse_stars():
    for raw, want in [("4", 4), ("5S", 5), ("4 S", 4), ("4 superior", 4), ("4+", 4), ("4,5", 4), ("3", 3)]:
        assert d.parse_stars(raw) == want, raw
    for raw in ["45", "4-5", "40", "", None, "five"]:
        assert d.parse_stars(raw) is None, raw


def test_query_is_built_from_config():
    q = d.build_query(TYPES, (1, 2, 3, 4), 99)
    assert '[timeout:99]' in q
    assert 'nwr["tourism"="hotel"]["stars"](1,2,3,4);' in q          # stars required -> filtered server-side
    assert 'nwr["tourism"="guest_house"](1,2,3,4);' in q             # B&Bs: no star filter
    assert 'nwr["tourism"="bed_and_breakfast"](1,2,3,4);' in q


def test_filters_kinds_stars_radius_names():
    by_name = {p.name: p for p in props()}
    assert by_name["Rosa Alpina"].kind == "hotel" and by_name["Rosa Alpina"].pid == "osm:way/303"
    assert by_name["B&B Villa Merano"].kind == "bed_and_breakfast" and by_name["B&B Villa Merano"].stars is None
    assert by_name["Rovinj Sea B&B"].kind == "bed_and_breakfast"     # B&B stars don't matter
    for gone in ["Paris Palace", "Small Venice Inn", "Odd Tag Hotel", "Not A Hotel Apartments"]:
        assert gone not in by_name, gone


def test_star_rules_follow_config():
    types = {**TYPES, "hotel": {**TYPES["hotel"], "stars": [3, 4, 5]}}
    names = {p.name for p in d.parse_elements(json.loads(FIXTURE.read_text())["elements"], *TRIESTE, 700, types)}
    assert "Small Venice Inn" in names


def test_dedupe_keeps_outline_and_borrows_tags():
    duchi = [p for p in d.dedupe(props()) if p.name.startswith("Grand Hotel Duchi")]
    assert len(duchi) == 1 and duchi[0].pid == "osm:way/202"
    assert duchi[0].website == "https://example.org/duchi"
