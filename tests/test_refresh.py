import csv
import json

from hotel_scraper import refresh
from conftest import FIXTURE


def run(cfg_path, *extra):
    return refresh.main(["--config", str(cfg_path), "--fixture", str(FIXTURE), *extra])


def rows(cfg):
    return list(csv.DictReader(open(cfg["paths"]["osm_catalog"], encoding="utf-8")))


def test_builds_osm_catalog_and_tile_state(tmp_cfg):
    path, cfg = tmp_cfg
    assert run(path, "--max-tiles", "2", "--today", "2026-11-07") == 0
    kinds = {r["name"]: r["kind"] for r in rows(cfg)}
    assert kinds["B&B Villa Merano"] == "bed_and_breakfast" and kinds["Rosa Alpina"] == "hotel"
    assert len(json.loads(open(cfg["paths"]["tile_state"]).read())) == 2
    assert open(f"{cfg['paths']['out_dir']}/osm_summary.md").read().startswith("## OpenStreetMap catalog:")


def test_partial_run_only_touches_its_tiles(tmp_cfg, tmp_path):
    path, cfg = tmp_cfg
    assert run(path, "--today", "2026-11-07") == 0
    assert len(json.loads(open(cfg["paths"]["tile_state"]).read())) == 76
    smaller = json.loads(FIXTURE.read_text())
    smaller["elements"] = [e for e in smaller["elements"] if e["id"] != 303]   # Rosa Alpina missing from the source
    fx = tmp_path / "day2.json"
    fx.write_text(json.dumps(smaller))
    assert refresh.main(["--config", str(path), "--fixture", str(fx), "--max-tiles", "1", "--today", "2026-11-08"]) == 0
    by = {r["name"]: r for r in rows(cfg)}
    assert by["Rosa Alpina"]["gone_since"] == ""


def test_full_rerun_marks_missing_as_gone(tmp_cfg, tmp_path):
    import yaml
    path, cfg = tmp_cfg
    cfg["overpass"]["tiles_per_run"] = 1000
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    assert run(path, "--today", "2026-11-07") == 0
    smaller = json.loads(FIXTURE.read_text())
    smaller["elements"] = [e for e in smaller["elements"] if e["id"] != 303]
    fx = tmp_path / "day2.json"
    fx.write_text(json.dumps(smaller))
    assert refresh.main(["--config", str(path), "--fixture", str(fx), "--today", "2026-11-08"]) == 0
    assert {r["name"]: r for r in rows(cfg)}["Rosa Alpina"]["gone_since"] == "2026-11-08"


def test_failed_tiles_never_mark_properties_gone(tmp_cfg, monkeypatch):
    path, cfg = tmp_cfg
    assert run(path, "--today", "2026-11-07") == 0
    real = refresh.discover_osm.discover

    def flaky(cfg_, fetch, **kw):
        def failing(q):
            raise RuntimeError("Overpass remark: runtime error: Query timed out")
        return real(cfg_, failing, **kw)
    monkeypatch.setattr(refresh.discover_osm, "discover", flaky)
    assert run(path, "--today", "2026-11-08") == 1
    assert all(r["gone_since"] == "" for r in rows(cfg))
