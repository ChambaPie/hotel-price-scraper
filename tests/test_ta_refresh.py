import csv
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import yaml

from hotel_scraper import selection, ta_refresh
from hotel_scraper.http import Response
from hotel_scraper.xotelo import Xotelo

TRIESTE_LIST = json.loads((Path(__file__).parent / "fixtures" / "xotelo_list_trieste.json").read_text())["result"]["list"]
FAR = {"name": "Far Away Hotel", "key": "g60763-d1", "accommodation_type": "Hotel",
       "review_summary": {"rating": 5, "count": 999}, "geo": {"latitude": 40.7, "longitude": -74.0}}


class FakeX:
    """Answers /list by location key; records calls; can fail a key."""
    def __init__(self, lists, fail=()):
        self.lists, self.fail, self.calls = lists, set(fail), []

    def get(self, url, headers=None):
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        key, off, lim = q["location_key"], int(q["offset"]), int(q["limit"])
        self.calls.append((key, off))
        if key in self.fail:
            return Response(503, {}, b"", 0.1, url, "HTTP 503")
        items = self.lists.get(key, [])
        body = {"error": None, "result": {"total_count": len(items), "limit": lim, "offset": off, "list": items[off:off + lim]}}
        return Response(200, {}, json.dumps(body).encode(), 0.1, url)


def setup(tmp_cfg, osm_rows=None, **tcfg):
    path, cfg = tmp_cfg
    cfg["tripadvisor"].update(seed_keys=["gSEED"], page_size=10, pause_seconds=[0, 0], **tcfg)
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    if osm_rows:
        with open(cfg["paths"]["osm_catalog"], "w", newline="") as f:
            w = csv.DictWriter(f, list(osm_rows[0])); w.writeheader(); w.writerows(osm_rows)
    return cfg


def go(cfg, fake, today="2026-11-08"):
    x = Xotelo(cfg["tripadvisor"]["base_url"], fake, pause_seconds=(0, 0), sleep=lambda s: None)
    return ta_refresh.run(cfg, x, today, log=lambda m: None, route=False)


def catalog(cfg):
    return {r["pid"]: r for r in csv.DictReader(open(cfg["paths"]["catalog"], encoding="utf-8"))}


OSM_SAVOIA = {"pid": "osm:way/36432036", "kind": "hotel", "name": "Savoia Excelsior Palace", "stars": "5",
              "lat": "45.64942", "lon": "13.76580", "gone_since": "", "drive_hours": "0.1", "city": "Trieste", "country": "IT"}


def test_seed_harvests_city_keys_then_lists_cities(tmp_cfg):
    cfg = setup(tmp_cfg, osm_rows=[OSM_SAVOIA])
    fake = FakeX({"gSEED": TRIESTE_LIST[:5] + [FAR], "g187813": TRIESTE_LIST})
    res = go(cfg, fake)
    state = json.loads(open(cfg["paths"]["ta_state"]).read())
    assert list(state["cities"]) == ["g187813"]                     # far-away city not harvested
    rows = catalog(cfg)
    types = {r["ta_type"] for r in rows.values()}
    assert "Condominium" not in types and "Hostel" not in types   # skipped types
    sav = rows["ta:g187813-d203235"]
    assert sav["kind"] == "hotel" and sav["stars"] == "5" and sav["stars_source"] == "osm" and sav["review_score"] == "4.7"
    assert any(r["breakfast_label"] == "yes" for r in rows.values())
    assert res["star_matches"] == 1 and not res["news"].any()       # first listing is not news
    assert res["coverage"].startswith("## Coverage: 1 of 1 official")
    w = {r["pid"]: r["watch_reason"] for r in csv.DictReader(open(cfg["paths"]["watchlist"]))}
    assert w["ta:g187813-d203235"] == "rule 1"                       # official 5 stars
    assert all(float(rows[p]["review_score"] or 0) >= 4.5 or rows[p]["stars"] for p in w)


def test_second_run_reports_real_changes_only_for_complete_lists(tmp_cfg):
    cfg = setup(tmp_cfg)
    go(cfg, FakeX({"gSEED": TRIESTE_LIST[:3], "g187813": TRIESTE_LIST}))
    smaller = [i for i in TRIESTE_LIST if i["key"] != "g187813-d203235"]
    res = go(cfg, FakeX({"gSEED": TRIESTE_LIST[:3], "g187813": smaller}), today="2026-11-09")
    assert [r["pid"] for r in res["news"].gone] == ["ta:g187813-d203235"]
    assert catalog(cfg)["ta:g187813-d203235"]["gone_since"] == "2026-11-09"


def test_failed_city_listing_marks_nothing_gone(tmp_cfg):
    cfg = setup(tmp_cfg)
    go(cfg, FakeX({"gSEED": TRIESTE_LIST[:3], "g187813": TRIESTE_LIST}))
    res = go(cfg, FakeX({"gSEED": TRIESTE_LIST[:3], "g187813": TRIESTE_LIST}, fail={"g187813"}), today="2026-11-09")
    assert not res["changes"].gone and all(r["gone_since"] == "" for r in catalog(cfg).values())


def test_request_budget_is_respected(tmp_cfg):
    cfg = setup(tmp_cfg, max_requests_per_run=2)
    fake = FakeX({"gSEED": TRIESTE_LIST[:3], "g187813": TRIESTE_LIST})
    res = go(cfg, fake)
    assert len(fake.calls) == 2 and res["requests"] == 2


def test_country_list_keys_are_resolved_from_the_url():
    item = dict(TRIESTE_LIST[0], key="g187768-d203235")       # as returned by a country-level list
    assert ta_refresh.ids(item) == ("g187813", "g187813-d203235")
    pp = ta_refresh.to_property(item, "g187768", (45.6495, 13.7768), 800, {"hotel": ["Hotel"]})
    assert pp.pid == "ta:g187813-d203235" and pp.list_key == "g187768"


def test_seed_items_enter_the_catalog_and_city_listing_wins(tmp_cfg):
    cfg = setup(tmp_cfg)
    seed_items = [dict(i, key="gSEED-" + i["key"].split("-")[1]) for i in TRIESTE_LIST[:5]]
    go(cfg, FakeX({"gSEED": seed_items}, fail={"g187813"}))    # city listing fails: seed rows still land
    rows = catalog(cfg)
    assert "ta:g187813-d203235" in rows and rows["ta:g187813-d203235"]["list_key"] == "gSEED"
    go(cfg, FakeX({"gSEED": seed_items, "g187813": TRIESTE_LIST}), today="2026-11-09")
    assert catalog(cfg)["ta:g187813-d203235"]["list_key"] == "g187813"


def test_selection_quotas_and_ranking():
    sel = {"auto_include": {"enabled": True, "max_watched": 3, "rules": [
        {"property_type": "hotel", "min_stars": 4, "max": 1},
        {"property_type": "hotel", "min_review_score": 4.5, "min_reviews": 20, "max": 5}]}}
    rows = [
        {"pid": "ta:g1-d0", "kind": "hotel", "stars": "5", "review_score": "4.2", "review_count": "800"},
        {"pid": "ta:g1-d1", "kind": "hotel", "stars": "4", "review_score": "4.6", "review_count": "300"},
        {"pid": "ta:g1-d2", "kind": "hotel", "stars": "", "review_score": "5.0", "review_count": "21"},
        {"pid": "ta:g1-d3", "kind": "hotel", "stars": "", "review_score": "4.8", "review_count": "2000"},
        {"pid": "ta:g1-d4", "kind": "hotel", "stars": "", "review_score": "4.9", "review_count": "5"},
    ]
    for r in rows:
        r.update(drive_hours="3", gone_since="")
    got = selection.resolve(rows, sel, include=["ta:g1-d4"], exclude=[])
    assert [(r["pid"], r["watch_reason"]) for r in got] == [
        ("ta:g1-d4", "include"),        # include always, even with 5 reviews
        ("ta:g1-d1", "rule 1"),          # best official 4-5* (quota 1)
        ("ta:g1-d3", "rule 2"),          # 4.8 from 2,000 reviews beats 5.0 from 21
        ("ta:g1-d2", "rule 2")]          # overall cap 3 applies to the ruled ones
    assert selection.PID_RE.match("ta:g187813-d203235") and not selection.PID_RE.match("ta:g1")


def test_unpriceable_properties_make_room():
    sel = {"auto_include": {"enabled": True, "rules": [{"property_type": "hotel", "min_review_score": 4.5, "max": 2}]}}
    rows = [{"pid": f"ta:g1-d{i}", "kind": "hotel", "review_score": "4.8", "review_count": str(1000 - i), "gone_since": ""}
            for i in range(3)]
    got = selection.resolve(rows, sel, [], [], skip={"ta:g1-d0"})
    assert [r["pid"] for r in got] == ["ta:g1-d1", "ta:g1-d2"]
