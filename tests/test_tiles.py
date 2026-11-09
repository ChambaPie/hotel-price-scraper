import pytest

from hotel_scraper import discover_osm as d
from hotel_scraper import tiles
from hotel_scraper.discover_osm import TileResult

A, B, C = (0, 0, 1, 1), (1, 0, 2, 1), (2, 0, 3, 1)


def test_choose_never_refreshed_first_then_oldest():
    assert tiles.choose([A, B, C], {}, 1) == [A, B, C]
    state = {tiles.key(A): {"done": "2026-11-08T03:00:00+00:00"}}
    assert tiles.choose([A, B, C], state, 1) == [B, C]
    state.update({tiles.key(B): {"done": "2026-11-01T03:00:00+00:00"}, tiles.key(C): {"done": "2026-11-05T03:00:00+00:00"}})
    assert tiles.choose([A, B, C], state, 2) == [B, C]


def test_record_and_covered():
    state = {}
    res = [TileResult(A, True, 10, 3, 1.5), TileResult(B, False, error="timeout")]
    tiles.record(state, res, "2026-11-08T03:00:00+00:00")
    assert list(state) == [tiles.key(A)]
    cov = tiles.covered_fn(res)
    assert cov({"lat": "0.5", "lon": "0.5"}) and not cov({"lat": "1.5", "lon": "0.5"}) and not cov({})


def test_overpass_remark_errors_are_failures():
    with pytest.raises(RuntimeError):
        d.check_overpass({"elements": [], "remark": "runtime error: Query timed out in \"query\" at line 3"})
    assert d.check_overpass({"elements": [1]}) == {"elements": [1]}


def test_discover_skips_failed_tiles_and_respects_deadline():
    from hotel_scraper import config
    cfg = config.load()
    seen = []

    def fetch(q):
        seen.append(q)
        if len(seen) == 2:
            raise RuntimeError("HTTP 504")
        return {"elements": []}
    clock = lambda: len(seen) * 100                              # each query "takes" 100 s
    props, res = d.discover(cfg, fetch, tile_list=[A, B, C, (3, 0, 4, 1)], sleep=lambda s: None, log=lambda m: None,
                            deadline=250, clock=clock)
    assert [r.ok for r in res] == [True, False, True]            # 4th tile not started: budget spent


def test_fix_country_vatican():
    assert d.fix_country("VA", 41.9028, 12.4964) == "IT"      # central Rome
    assert d.fix_country("VA", 41.9035, 12.4520) == "VA"      # inside Vatican City
    assert d.fix_country("SI", 46.05, 14.5) == "SI"


def test_first_time_tiles_are_not_news():
    from hotel_scraper import catalog as cat
    from hotel_scraper.refresh import reportable
    seen = tiles.covered_fn([TileResult(A, True)])
    ch = cat.Changes(new=[{"pid": "a", "lat": "0.5", "lon": "0.5"}, {"pid": "b", "lat": "1.5", "lon": "0.5"}],
                     gone=[{"pid": "c"}])
    rep = reportable(ch, seen)
    assert [r["pid"] for r in rep.new] == ["a"] and rep.gone == [{"pid": "c"}]
