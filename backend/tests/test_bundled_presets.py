"""The presets the bundled models define (`models/<name>/model.json`'s `presets`) are
valid ones: the store reads every entry, each has an explicit id, and every value is one
a render of that model accepts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scadbuddy.core.config import Config, load_config
from scadbuddy.core.paths import LEGACY_PRESETS_NAME, MODEL_META_NAME, DataPaths
from scadbuddy.library.presets import PresetStore
from scadbuddy.render.runner import build_defines, export_schema

MODELS = Path(__file__).resolve().parents[2] / "models"
TEMPLATES = sorted(path.parent.name for path in MODELS.glob("*/model.scad"))


def _defined(slug: str) -> list[dict[str, object]]:
    presets = json.loads((MODELS / slug / MODEL_META_NAME).read_text(encoding="utf-8")).get(
        "presets"
    )
    return presets if isinstance(presets, list) else []


SHIPPING = sorted(
    path.parent.name for path in MODELS.glob(f"*/{MODEL_META_NAME}") if _defined(path.parent.name)
)


def _store(tmp_path: Path, slug: str) -> PresetStore:
    """A store whose `models/` holds only ``slug``'s model.json."""
    paths = DataPaths(tmp_path)
    paths.model_dir(slug).mkdir(parents=True)
    source = MODELS / slug / MODEL_META_NAME
    (paths.model_dir(slug) / MODEL_META_NAME).write_bytes(source.read_bytes())
    return PresetStore(paths)


def test_some_bundled_models_ship_presets() -> None:
    assert SHIPPING


def test_no_bundled_model_uses_the_legacy_presets_file() -> None:
    assert not list(MODELS.glob(f"*/{LEGACY_PRESETS_NAME}"))


@pytest.mark.parametrize("slug", SHIPPING)
def test_every_shipped_preset_is_read(tmp_path: Path, slug: str) -> None:
    presets = _store(tmp_path, slug).template_presets(slug)
    # A list the store cannot read is logged and listed as no presets at all.
    assert len(presets) == len(_defined(slug)), f"{slug}'s presets did not load"


@pytest.mark.parametrize("slug", SHIPPING)
def test_every_shipped_preset_has_an_id(slug: str) -> None:
    # Written down, so renaming a bundled preset never changes which one it is.
    assert all(preset.get("id") for preset in _defined(slug)), slug


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
