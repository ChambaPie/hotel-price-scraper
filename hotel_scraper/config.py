from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "config" / "settings.yaml"


def load(path: Path | str = DEFAULT_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)
