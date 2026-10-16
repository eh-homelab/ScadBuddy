"""A `Config` whose openscad is `tests.conftest`'s fake: one blue box per 3MF, and a
`.param` with one parameter, `width`."""

from __future__ import annotations

from pathlib import Path

from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths
from tests.conftest import fake_3mf_openscad


def install_fake_openscad(tmp_path: Path, paths: DataPaths) -> Config:
    return Config(openscad=fake_3mf_openscad(tmp_path / "bin"), data_dir=paths.root)
