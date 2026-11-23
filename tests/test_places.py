"""Type of place around watched properties, from OpenStreetMap extracts (old town, vineyards, thermal, major city,
plus mountain, sea, lake...). The reader runs for real on a small OpenStreetMap file."""
import csv
from datetime import date

import pytest

from hotel_scraper import config, places

PC = config.load()["places"]
LAT, LON = 45.6500, 13.7700


def osm_file(path):
    hist = "".join(f'<node id="{100 + i}" lat="{LAT + i * 0.0005}" lon="{LON}"><tag k="historic" v="monument"/>'
                   f'<tag k="wikidata" v="Q{i}"/></node>' for i in range(9))
    path.write_text(f"""<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6" generator="test">
  <node id="1" lat="45.65" lon="13.78"><tag k="place" v="city"/><tag k="population" v="200 000"/></node>
  <node id="2" lat="45.80" lon="13.70"><tag k="place" v="city"/><tag k="population" v="300000"/></node>
  {hist}
  <node id="3" lat="{LAT}" lon="{LON + 0.02}"><tag k="amenity" v="public_bath"/><tag k="name" v="Terme Excelsior"/></node>
  <node id="4" lat="{LAT}" lon="{LON - 0.02}"><tag k="amenity" v="public_bath"/><tag k="name" v="Hallenbad"/></node>
  <node id="5" lat="{LAT}" lon="{LON + 0.001}"><tag k="amenity" v="cafe"/></node>
  <node id="10" lat="{LAT + 0.005}" lon="{LON - 0.005}"/><node id="11" lat="{LAT + 0.006}" lon="{LON - 0.005}"/>
  <node id="12" lat="{LAT + 0.006}" lon="{LON - 0.004}"/>
  <way id="100"><nd ref="10"/><nd ref="11"/><nd ref="12"/><nd ref="10"/><tag k="landuse" v="vineyard"/></way>
  <node id="20" lat="{LAT}" lon="{LON + 0.01}"/><node id="21" lat="{LAT + 0.01}" lon="{LON + 0.01}"/>
  <node id="22" lat="{LAT + 0.02}" lon="{LON + 0.01}"/>
  <way id="101"><nd ref="20"/><nd ref="21"/><nd ref="22"/><tag k="natural" v="coastline"/></way>
  <node id="30" lat="{LAT - 0.003}" lon="{LON}"/><node id="31" lat="{LAT - 0.003}" lon="{LON + 0.001}"/>
  <node id="32" lat="{LAT - 0.002}" lon="{LON + 0.001}"/>
  <way id="102"><nd ref="30"/><nd ref="31"/><nd ref="32"/><nd ref="30"/><tag k="historic" v="castle"/><tag k="wikidata" v="Q99"/></way>
  <node id="40" lat="46.30" lon="13.60"/><node id="41" lat="46.30" lon="13.62"/><node id="42" lat="46.32" lon="13.62"/>
  <way id="200"><nd ref="40"/><nd ref="41"/><nd ref="42"/></way><way id="201"><nd ref="42"/><nd ref="40"/></way>
  <relation id="300"><member type="way" ref="200" role="outer"/><member type="way" ref="201" role="outer"/>
    <tag k="type" v="multipolygon"/><tag k="natural" v="water"/><tag k="water" v="lake"/><tag k="name" v="Lago"/></relation>
</osm>""", encoding="utf-8")
    return path


def test_reader_on_an_openstreetmap_file(tmp_path):
    pytest.importorskip("osmium")
    pts, cities = places.read_features(str(osm_file(tmp_path / "t.osm")), (45.0, 13.0, 47.0, 14.5), step=1)
    assert len(pts["historic"]) == 10                     # 9 monuments + the castle outline (a closed way)
    assert len(pts["thermal"]) == 1 and len(pts["vineyard"]) == 1 and len(pts["sea"]) == 3
    assert len(pts["lake"]) >= 3                          # the lake relation's shore, assembled from its ways
    assert sorted(c[2] for c in cities) == [200000, 300000]


def test_measure_and_tags(tmp_path):
    pytest.importorskip("osmium")
    pts, cities = places.read_features(str(osm_file(tmp_path / "t.osm")), (45.0, 13.0, 47.0, 14.5), step=1)
    grids = {k: places.Grid(v) for k, v in pts.items()}
    r = {"elevation_m": "5", **places.measure(LAT, LON, grids, cities, PC)}
    assert r["historic_n"] == 10 and r["major_city_km"] == "" and r["city_km"] != "" and r["lake_km"] == ""
    assert places.tags_for(r, PC) == "sea,old_town,city,vineyards,thermal"


def test_mountain_from_elevation_or_ski_and_countryside():
    base = {k: "" for k in places.FIELDS}
    assert places.tags_for({**base, "elevation_m": "1200", "historic_n": 0}, PC) == "mountain"
    assert places.tags_for({**base, "elevation_m": "300", "ski_km": "2.1", "historic_n": 0}, PC) == "mountain,ski"
    assert places.tags_for({**base, "elevation_m": "200", "vineyard_km": "0.4", "historic_n": 0}, PC) == "vineyards,countryside"
    assert places.tags_for({**base, "major_city_km": "3", "historic_n": 2}, PC) == "major_city,city"
    assert places.tags_for({**base, "unesco_km": "0.6", "historic_n": 1}, PC) == "old_town"


def test_classify():
    assert places.classify({"historic": "castle", "wikidata": "Q1", "heritage": "1"}) == ["unesco", "historic"]
    assert places.classify({"historic": "wayside_cross"}) == []            # no Wikidata entry: not counted
    assert places.classify({"natural": "water", "water": "lake"}) == []    # unnamed pond
    assert places.classify({"amenity": "public_bath", "bath:type": "thermal"}) == ["thermal"]


def square(w, s, e, n):
    return {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}


INDEX = {"features": [
    {"properties": {"id": "europe", "urls": {"pbf": "u/europe"}}, "geometry": square(-10, 35, 30, 60)},
    {"properties": {"id": "italy", "parent": "europe", "urls": {"pbf": "u/italy"}}, "geometry": square(6, 36, 19, 47.1)},
    {"properties": {"id": "italy/nord-est", "parent": "italy", "urls": {"pbf": "u/ne"}}, "geometry": square(10, 44, 14, 47.1)},
    {"properties": {"id": "slovenia", "parent": "europe", "urls": {"pbf": "u/si"}}, "geometry": square(13.9, 45.4, 16.6, 46.9)},
]}


def test_smallest_extract_that_contains_each_property():
    ex = places.extracts_from_index(INDEX)
    assert {x["id"] for x in ex} == {"italy", "italy/nord-est", "slovenia"}     # never a continent
    rows = [{"pid": "trieste", "lat": "45.65", "lon": "13.77"}, {"pid": "rome", "lat": "41.9", "lon": "12.5"},
            {"pid": "bled", "lat": "46.37", "lon": "14.11"}, {"pid": "paris", "lat": "48.8", "lon": "2.3"}]
    g = places.choose_extracts(rows, ex)
    assert {k: [r["pid"] for r in v] for k, v in g.items()} == {"italy/nord-est": ["trieste"], "italy": ["rome"],
                                                                 "slovenia": ["bled"]}
    assert "italy/nord-est" not in places.choose_extracts(rows, ex, skip={"italy/nord-est"})


def test_run_one_download_per_extract_and_rolling(tmp_path):
    pytest.importorskip("osmium")
    cfg = config.load()
    for k in ("watchlist", "places", "landscape"):
        cfg["paths"][k] = str(tmp_path / f"{k}.csv")
    with open(cfg["paths"]["watchlist"], "w", newline="") as f:
        w = csv.DictWriter(f, ["pid", "lat", "lon", "gone_since"])
        w.writeheader()
        w.writerows([{"pid": "ta:1", "lat": str(LAT), "lon": str(LON), "gone_since": ""},
                     {"pid": "ta:2", "lat": "45.66", "lon": "13.78", "gone_since": ""}])
    with open(cfg["paths"]["landscape"], "w", newline="") as f:
        w = csv.DictWriter(f, ["pid", "elevation_m"])
        w.writeheader()
        w.writerows([{"pid": "ta:1", "elevation_m": "12"}, {"pid": "ta:2", "elevation_m": "40"}])
    src = osm_file(tmp_path / "src.osm")
    got = []

    def fetch(url, dest, log=None):
        got.append(url)
        dest.write_bytes(src.read_bytes())
        return dest.stat().st_size
    # the download is saved as .osm.pbf; pyosmium tells the format from the name, so read the .osm original
    kw = dict(index=INDEX, fetch=fetch, elev=lambda pts: [], log=lambda m: None, workdir=tmp_path,
              read=lambda path, bbox, step: places.read_features(str(src), bbox, step))
    res = places.run(cfg, 5, today=date(2026, 11, 23), **kw)
    assert got == ["u/ne"] and res["done"] == 2 and res["coverage"] == "2/2"
    rows = {r["pid"]: r for r in csv.DictReader(open(cfg["paths"]["places"]))}
    assert rows["ta:1"]["extract"] == "italy/nord-est" and "old_town" in rows["ta:1"]["tags"]
    assert rows["ta:1"]["elevation_m"] == "12"
    assert places.run(cfg, 5, today=date(2026, 11, 24), **kw)["due"] == 0 and got == ["u/ne"]


def test_fetch_retries_then_ipv4(monkeypatch):
    import socket
    calls = []

    def fake_urlopen(req, timeout=0):
        calls.append(socket.getaddrinfo is not real)
        if len(calls) == 1:
            raise OSError(101, "Network is unreachable")
        return "ok"
    real = socket.getaddrinfo
    monkeypatch.setattr(places, "urlopen", fake_urlopen)
    assert places.fetch_url("https://example.org/x", log=lambda m: None, sleep=lambda s: None) == "ok"
    assert calls == [False, True] and socket.getaddrinfo is real      # second try over IPv4 only, then restored


def test_unreachable_index_is_reported_not_fatal(tmp_path, monkeypatch):
    cfg = config.load()
    for k in ("watchlist", "places", "landscape"):
        cfg["paths"][k] = str(tmp_path / f"{k}.csv")
    with open(cfg["paths"]["watchlist"], "w", newline="") as f:
        w = csv.DictWriter(f, ["pid", "lat", "lon", "gone_since"])
        w.writeheader()
        w.writerow({"pid": "ta:1", "lat": "45.6", "lon": "13.7", "gone_since": ""})

    def down(*a, **k):
        raise RuntimeError("unreachable")
    monkeypatch.setattr(places, "fetch_url", down)
    res = places.run(cfg, 5, elev=lambda pts: [], today=date(2026, 11, 23), log=lambda m: None)
    assert res["failed_extracts"] == -1 and res["done"] == 0
