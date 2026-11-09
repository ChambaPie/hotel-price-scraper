from hotel_scraper import catalog as cat


def row(pid, name="X", **kw):
    return {"pid": pid, "kind": "hotel", "name": name, "stars": "4", "stars_raw": "4", "lat": "45", "lon": "13",
            "distance_km": "10", "country": "IT", "city": "", "website": "", "wikidata": "", **kw}


def test_first_run_everything_new():
    rows, ch = cat.merge([], [row("osm:node/1"), row("osm:node/2")], "2026-11-07")
    assert len(ch.new) == 2 and not ch.gone and all(r["first_seen"] == "2026-11-07" for r in rows)


def test_gone_returned_and_extra_columns_survive():
    day1, _ = cat.merge([], [row("osm:node/1"), row("osm:node/2")], "2026-11-07")
    day1[0]["review_score"] = "9.1"                        # added later by enrichment
    day2, ch2 = cat.merge(day1, [row("osm:node/1", name="Renamed"), row("osm:node/3")], "2026-11-08")
    by = {r["pid"]: r for r in day2}
    assert [r["pid"] for r in ch2.new] == ["osm:node/3"]
    assert [r["pid"] for r in ch2.gone] == ["osm:node/2"] and by["osm:node/2"]["gone_since"] == "2026-11-08"
    assert by["osm:node/1"]["name"] == "Renamed" and by["osm:node/1"]["review_score"] == "9.1"
    assert by["osm:node/1"]["first_seen"] == "2026-11-07"
    day3, ch3 = cat.merge(day2, [row("osm:node/1"), row("osm:node/2"), row("osm:node/3")], "2026-11-09")
    assert [r["pid"] for r in ch3.returned] == ["osm:node/2"] and not ch3.gone
    assert {r["pid"]: r for r in day3}["osm:node/2"]["gone_since"] == ""


def test_unchanged_day_has_no_changes():
    day1, _ = cat.merge([], [row("osm:node/1")], "2026-11-07")
    day2, ch = cat.merge(day1, [row("osm:node/1")], "2026-11-08")
    assert not ch.any() and day2[0] == day1[0]
