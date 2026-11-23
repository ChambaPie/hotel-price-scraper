"""Type of place around watched properties (old town, vineyards, thermal, major city, plus the landscape tags)."""
import csv
from datetime import date

from hotel_scraper import config, places

PC = config.load()["places"]
LAT, LON = 45.6500, 13.7700


def el(kind, lat, lon, eid, **tags):
    e = {"type": kind, "id": eid, "tags": tags}
    if kind == "node":
        e.update(lat=lat, lon=lon)
    else:
        e["center"] = {"lat": lat, "lon": lon}
    return e


def coast(eid, pts):
    return {"type": "way", "id": eid, "tags": {"natural": "coastline"}, "geometry": [{"lat": a, "lon": b} for a, b in pts]}


def elements():
    hist = [el("node", LAT + i * 0.0005, LON, 100 + i, historic="monument", wikidata=f"Q{i}") for i in range(9)]
    return hist + [
        coast(1, [(LAT, LON + 0.01), (LAT + 0.01, LON + 0.01), (LAT + 0.02, LON + 0.01)]),   # ~0.8 km east
        el("node", 45.65, 13.78, 2, place="city", population="200 000"),                     # Trieste-sized: city only
        el("node", 45.80, 13.70, 3, place="city", population="300000"),                       # big, ~17 km: too far
        el("way", LAT + 0.005, LON - 0.005, 4, landuse="vineyard"),
        el("node", LAT, LON + 0.02, 5, amenity="public_bath", name="Terme Excelsior"),        # thermal by name
        el("node", LAT, LON - 0.02, 6, amenity="public_bath", name="Hallenbad"),             # not thermal
        el("node", 45.6501, 13.7701, 100, historic="monument", wikidata="Q0"),                # duplicate: counted once
    ]


def test_features_and_tags():
    f = places.features(elements(), step=1)
    assert len(f["historic"]) == 9 and len(f["thermal"]) == 1 and len(f["sea"]) == 3
    r = {"elevation_m": "5", **places.measure(LAT, LON, f, PC)}
    assert r["historic_n"] == 9 and r["major_city_km"] == "" and r["city_km"] != ""
    assert places.tags_for(r, PC) == "sea,old_town,city,vineyards,thermal"


def test_mountain_from_elevation_or_ski_and_countryside():
    base = {k: "" for k in places.FIELDS}
    assert places.tags_for({**base, "elevation_m": "1200", "historic_n": 0}, PC) == "mountain"
    assert places.tags_for({**base, "elevation_m": "300", "ski_km": "2.1", "historic_n": 0}, PC) == "mountain,ski"
    assert places.tags_for({**base, "elevation_m": "200", "vineyard_km": "0.4", "historic_n": 0}, PC) == "vineyards,countryside"
    assert places.tags_for({**base, "elevation_m": "", "major_city_km": "3", "historic_n": 2}, PC) == "major_city,city"
    assert places.tags_for({**base, "unesco_km": "0.6", "historic_n": 1}, PC) == "old_town"


def test_query_asks_only_around_the_properties():
    q = places.query([(46.1, 13.2), (46.2, 13.3)], PC, 120)
    assert q.count("around:") == 22 and "(around:1500,46.10000,13.20000)" in q and "out geom;" in q


def test_batches_keep_neighbours_together():
    rows = [{"pid": str(i), "lat": str(la), "lon": str(lo)} for i, (la, lo) in
            enumerate([(46.1, 13.0), (48.0, 11.0), (46.2, 13.1), (48.1, 11.2)])]
    assert [[r["pid"] for r in b] for b in places.batches(rows, 2)] == [["0", "2"], ["1", "3"]]


def test_run_rolls_through_due_properties(tmp_path):
    cfg = config.load()
    for k in ("watchlist", "places", "landscape"):
        cfg["paths"][k] = str(tmp_path / f"{k}.csv")
    with open(cfg["paths"]["watchlist"], "w", newline="") as f:
        w = csv.DictWriter(f, ["pid", "lat", "lon", "gone_since"])
        w.writeheader()
        w.writerow({"pid": "ta:1", "lat": str(LAT), "lon": str(LON), "gone_since": ""})
    with open(cfg["paths"]["landscape"], "w", newline="") as f:
        w = csv.DictWriter(f, ["pid", "elevation_m"])
        w.writeheader()
        w.writerow({"pid": "ta:1", "elevation_m": "12"})
    asked = []

    def fetch(q):
        asked.append(q)
        return {"elements": elements()}
    res = places.run(cfg, 5, fetch=fetch, elev=lambda pts: [], today=date(2026, 11, 23), log=lambda m: None,
                     sleep=lambda s: None)
    assert res["done"] == 1 and res["coverage"] == "1/1" and len(asked) == 1
    row = next(csv.DictReader(open(cfg["paths"]["places"])))
    assert row["elevation_m"] == "12" and "old_town" in row["tags"]
    assert places.run(cfg, 5, fetch=fetch, today=date(2026, 11, 24), log=lambda m: None, sleep=lambda s: None)["due"] == 0
