import shutil
from pathlib import Path

import pytest
import yaml

from hotel_scraper import config

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).parent / "fixtures" / "overpass_sample.json"


@pytest.fixture
def tmp_cfg(tmp_path):
    """Real settings, but every path points into tmp_path. Returns (config_path, cfg)."""
    cfg = config.load()
    for name in ("selection", "include", "exclude"):
        dst = tmp_path / Path(cfg["paths"][name]).name
        shutil.copy(ROOT / cfg["paths"][name], dst)
        cfg["paths"][name] = str(dst)
    cfg["paths"].update(catalog=str(tmp_path / "catalog.csv"), watchlist=str(tmp_path / "watchlist.csv"),
                        osm_catalog=str(tmp_path / "osm_catalog.csv"), ta_state=str(tmp_path / "ta_state.json"),
                        drive_times=str(tmp_path / "drive_times.csv"), tile_state=str(tmp_path / "tile_state.json"),
                        out_dir=str(tmp_path / "out"))
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return path, cfg
