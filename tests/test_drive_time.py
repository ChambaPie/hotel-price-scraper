from urllib.parse import urlsplit

import pytest

from hotel_scraper import config, drive_time as dtm
from hotel_scraper.discover_osm import Property

TODAY = "2026-11-07"


def prop(i, lat=46.0, lon=14.0):
    return Property(pid=f"osm:node/{i}", kind="hotel", name=f"H{i}", stars=4, stars_raw="4", lat=lat, lon=lon,
                    distance_km=100.0)


@pytest.fixture
def cfg(tmp_path):
    c = config.load()
    c["paths"]["drive_times"] = str(tmp_path / "drive_times.csv")
    c["universe"]["drive_time"].update(batch_size=3, max_requests_per_run=100, pause_seconds=0)
    return c


class FakeOSRM:
    """Answers table requests: hours = destination latitude - 40 (so lat 46 -> 6 h, lat 52 -> 12 h)."""
    def __init__(self, fail_after=None, too_big_above=None, unroutable_lat=None):
        self.calls, self.fail_after, self.too_big_above, self.unroutable_lat = [], fail_after, too_big_above, unroutable_lat

    def __call__(self, url):
        self.calls.append(url)
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise OSError("connection reset")
        coords = urlsplit(url).path.split("/")[-1].split(";")
        dests = [tuple(map(float, c.split(","))) for c in coords[1:]]
        if self.too_big_above and len(dests) > self.too_big_above:
            return {"code": "TooBig", "message": "Too many table coordinates"}
        durs = [None if lat == self.unroutable_lat else (lat - 40) * 3600 for lon, lat in dests]
        return {"code": "Ok", "durations": [[0, *durs]], "distances": [[0, *[None if d is None else d * 1000 / 36 for d in durs]]]}


def test_routes_in_batches_and_drops_over_limit(cfg):
    props = [prop(i) for i in range(5)] + [prop(9, lat=52.0)]
    fake = FakeOSRM()
    kept, st = dtm.update(props, cfg, TODAY, fetch=fake, sleep=lambda s: None)
    assert len(fake.calls) == 2 and "sources=0" in fake.calls[0]         # 6 properties, batches of 3
    assert [p.pid for p in kept] == [f"osm:node/{i}" for i in range(5)]  # 12 h one dropped
    assert kept[0].drive_hours == 6.0 and kept[0].road_km == 600.0
    assert st.routed == 6 and st.dropped == 1


def test_cache_is_reused_and_moved_properties_rerouted(cfg):
    dtm.update([prop(1), prop(2)], cfg, TODAY, fetch=FakeOSRM(), sleep=lambda s: None)
    fake = FakeOSRM()
    kept, st = dtm.update([prop(1), prop(2, lat=47.0)], cfg, TODAY, fetch=fake, sleep=lambda s: None)
    assert st.cached == 1 and st.routed == 1 and len(fake.calls) == 1
    assert {p.pid: p.drive_hours for p in kept} == {"osm:node/1": 6.0, "osm:node/2": 7.0}


def test_server_failure_keeps_properties_pending(cfg):
    kept, st = dtm.update([prop(i) for i in range(6)], cfg, TODAY, fetch=FakeOSRM(fail_after=1), sleep=lambda s: None)
    assert len(kept) == 6 and st.routed == 3 and st.pending == 3 and "connection reset" in st.error
    assert sum(1 for p in kept if p.drive_hours == "") == 3


def test_too_big_halves_batch(cfg):
    cfg["universe"]["drive_time"]["batch_size"] = 8
    fake = FakeOSRM(too_big_above=4)
    kept, st = dtm.update([prop(i) for i in range(8)], cfg, TODAY, fetch=fake, sleep=lambda s: None)
    assert st.routed == 8 and all(p.drive_hours == 6.0 for p in kept)


def test_unroutable_kept_or_dropped_by_setting(cfg):
    props = [prop(1), prop(2, lat=45.5)]
    kept, st = dtm.update(props, cfg, TODAY, fetch=FakeOSRM(unroutable_lat=45.5), sleep=lambda s: None)
    assert st.unroutable == 1 and len(kept) == 2
    cfg["universe"]["drive_time"]["keep_unroutable"] = False
    kept, _ = dtm.update([prop(1), prop(2, lat=45.5)], cfg, TODAY, fetch=FakeOSRM(), sleep=lambda s: None)
    assert [p.pid for p in kept] == ["osm:node/1"]                      # cached as unroutable -> dropped


def test_request_cap_leaves_rest_for_next_run(cfg):
    cfg["universe"]["drive_time"]["max_requests_per_run"] = 1
    kept, st = dtm.update([prop(i) for i in range(5)], cfg, TODAY, fetch=FakeOSRM(), sleep=lambda s: None)
    assert st.routed == 3 and st.pending == 2 and len(kept) == 5


def test_disabled_does_nothing(cfg):
    cfg["universe"]["drive_time"]["enabled"] = False
    fake = FakeOSRM()
    kept, _ = dtm.update([prop(1)], cfg, TODAY, fetch=fake, sleep=lambda s: None)
    assert kept[0].drive_hours == "" and fake.calls == []
