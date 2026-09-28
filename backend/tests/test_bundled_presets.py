"""The presets the bundled models ship (`models/<name>/presets.json`) are valid ones:
the store reads every entry, and every value is one a render of that model accepts."""

from __future__ import annotations

from pathlib import Path

import pytest

from scadbuddy.core.config import Config, load_config
from scadbuddy.core.paths import TEMPLATE_PRESETS_NAME, DataPaths
from scadbuddy.library.presets import PresetStore
from scadbuddy.render.runner import build_defines, export_schema

MODELS = Path(__file__).resolve().parents[2] / "models"
TEMPLATES = sorted(path.parent.name for path in MODELS.glob("*/model.scad"))
SHIPPING = sorted(path.parent.name for path in MODELS.glob(f"*/{TEMPLATE_PRESETS_NAME}"))


def _store(tmp_path: Path, slug: str) -> PresetStore:
    """A store whose `models/` holds only ``slug``'s presets file."""
    paths = DataPaths(tmp_path)
    paths.model_dir(slug).mkdir(parents=True)
    source = MODELS / slug / TEMPLATE_PRESETS_NAME
    (paths.model_dir(slug) / TEMPLATE_PRESETS_NAME).write_bytes(source.read_bytes())
    return PresetStore(paths)


def test_some_bundled_models_ship_presets() -> None:
    assert SHIPPING


@pytest.mark.parametrize("slug", SHIPPING)
def test_every_shipped_preset_is_read(tmp_path: Path, slug: str) -> None:
    presets = _store(tmp_path, slug).template_presets(slug)
    # A file the store cannot read is logged and listed as no presets at all.
    assert presets, f"{slug}/{TEMPLATE_PRESETS_NAME} did not load"
    names = [preset.name.casefold() for preset in presets]
    assert len(names) == len(set(names)), f"{slug} ships two presets of the same name"


@pytest.mark.requires_openscad
@pytest.mark.parametrize("slug", SHIPPING)
async def test_every_shipped_preset_renders(tmp_path: Path, slug: str) -> None:
    config = Config(openscad=load_config().openscad, data_dir=tmp_path)
    schema = await export_schema(MODELS / slug / "model.scad", config=config)
    for preset in _store(tmp_path, slug).template_presets(slug):
        # Raises on an unknown parameter, a value of the wrong type, one outside its
        # customizer range, or a dropdown value that is not one of its options (#432).
        build_defines(schema, preset.params)


@pytest.mark.requires_openscad
@pytest.mark.parametrize("slug", TEMPLATES)
async def test_every_bundled_default_is_in_its_customizer_range(tmp_path: Path, slug: str) -> None:
    """A render of the defaults sends them all: none may fall outside its own range (#432)."""
    config = Config(openscad=load_config().openscad, data_dir=tmp_path)
    schema = await export_schema(MODELS / slug / "model.scad", config=config)
    defaults = {p.name: p.initial for p in schema.parameters if p.initial is not None}
    build_defines(schema, defaults)
