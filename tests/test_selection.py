from pathlib import Path

from hotel_scraper import selection as s

SEL = {"auto_include": {"enabled": True, "rules": [
    {"property_type": "hotel", "min_stars": 4},
    {"property_type": "bed_and_breakfast", "min_review_score": 9.0}]}}


def r(pid, kind="hotel", stars="4", **kw):
    return {"pid": pid, "kind": kind, "stars": stars, "country": "IT", "distance_km": "50", "gone_since": "", **kw}


def test_rules_include_exclude():
    rows = [r("osm:node/1"), r("osm:node/2", stars="3"), r("osm:node/3", kind="bed_and_breakfast", stars=""),
            r("osm:node/4", kind="bed_and_breakfast", stars="", review_score="9.3"), r("osm:node/5"),
            r("osm:node/6", gone_since="2026-11-08")]
    got = {x["pid"]: x["watch_reason"] for x in s.resolve(rows, SEL, include=["osm:node/2"], exclude=["osm:node/5"])}
    assert got == {"osm:node/1": "rule 1", "osm:node/2": "include", "osm:node/4": "rule 2"}


def test_rules_can_be_switched_off():
    assert s.resolve([r("osm:node/1")], {"auto_include": {"enabled": False, "rules": SEL["auto_include"]["rules"]}}, [], []) == []


def test_drive_time_rule_needs_known_time():
    rule = {"property_type": "hotel", "max_drive_hours": 4}
    assert s.rule_matches(r("osm:node/1", drive_hours="3.5"), rule)
    assert not s.rule_matches(r("osm:node/1", drive_hours="5"), rule)
    assert not s.rule_matches(r("osm:node/1", drive_hours=""), rule)


def test_country_and_distance_filters():
    rule = {"property_type": "hotel", "countries": ["SI"], "max_distance_km": 100}
    assert not s.rule_matches(r("osm:node/1"), rule)
    assert s.rule_matches(r("osm:node/1", country="SI"), rule)
    assert not s.rule_matches(r("osm:node/1", country="SI", distance_km="150"), rule)


def test_edit_moves_ids_between_lists(tmp_path):
    inc, exc = tmp_path / "include.txt", tmp_path / "exclude.txt"
    inc.write_text("# header\n", encoding="utf-8")
    exc.write_text("osm:way/7  # Some Hotel\n", encoding="utf-8")
    known = {"osm:way/7": {"name": "Some Hotel", "city": "Bled", "country": "SI"}, "osm:node/8": {}}
    assert s.edit(inc, exc, ["osm:way/7"], ["osm:node/8"], known) == []
    assert s.read_ids(inc) == ["osm:way/7"] and s.read_ids(exc) == ["osm:node/8"]
    assert "Some Hotel, Bled, SI" in inc.read_text()
    assert s.edit(inc, exc, ["osm:way/7"], [], known) == [] and s.read_ids(inc) == ["osm:way/7"]   # idempotent
    assert s.edit(inc, exc, ["osm:way/999", "bogus"], [], known) == ["bogus", "osm:way/999"]
