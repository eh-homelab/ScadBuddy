"""#770: a template's default slicer settings, ``print_settings`` in its model.json."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from scadbuddy.core.paths import MODEL_META_NAME, DataPaths
from scadbuddy.library.catalogue import (
    PRINT_SETTING_KEYS,
    Catalogue,
    InvalidModelMetaError,
    meta_from_raw,
)
from scadbuddy.render.solids import WRAPPER_PREFIX

MODELS = Path(__file__).resolve().parents[2] / "models"


def test_print_settings_are_read_in_the_allowlists_order() -> None:
    meta = meta_from_raw(
        {"print_settings": {"enable_support": "0", "enable_prime_tower": "1"}}, "demo"
    )

    assert meta.print_settings == {"enable_prime_tower": "1", "enable_support": "0"}
    assert list(meta.print_settings) == ["enable_prime_tower", "enable_support"]


def test_no_print_settings_is_empty() -> None:
    assert meta_from_raw({}, "demo").print_settings == {}
    assert meta_from_raw({"print_settings": None}, "demo").print_settings == {}


def test_every_allowlisted_key_is_accepted() -> None:
    raw = {key: "1" for key in PRINT_SETTING_KEYS}

    assert meta_from_raw({"print_settings": raw}, "demo").print_settings == raw


def test_an_unknown_key_is_refused_by_name() -> None:
    with pytest.raises(ValidationError) as caught:
        meta_from_raw({"print_settings": {"layer_height": "0.2"}}, "demo")

    message = str(caught.value)
    assert "'layer_height' is not a print setting a template can set" in message
    assert "enable_prime_tower" in message


def test_a_value_that_is_not_a_string_is_refused() -> None:
    """Bambu's configs store every value as a string, so a template does too."""
    with pytest.raises(ValidationError):
        meta_from_raw({"print_settings": {"enable_support": 0}}, "demo")


def test_print_settings_that_are_not_an_object_are_refused() -> None:
    with pytest.raises(ValidationError):
        meta_from_raw({"print_settings": ["enable_support"]}, "demo")


def _catalogue(tmp_path: Path, meta: dict[str, object]) -> Catalogue:
    paths = DataPaths(tmp_path)
    paths.model_dir("demo").mkdir(parents=True)
    paths.model_source("demo").write_text("cube(1);\n", encoding="utf-8")
    paths.model_meta("demo").write_text(json.dumps(meta), encoding="utf-8")
    return Catalogue(paths, wrapper_prefix=WRAPPER_PREFIX)


def test_the_catalogue_answers_a_templates_print_settings(tmp_path: Path) -> None:
    catalogue = _catalogue(tmp_path, {"name": "Demo", "print_settings": {"brim_width": "5"}})

    assert catalogue.print_settings("demo") == {"brim_width": "5"}
    assert catalogue.record("demo").print_settings == {"brim_width": "5"}


def test_a_template_that_is_gone_has_none(tmp_path: Path) -> None:
    assert _catalogue(tmp_path, {"name": "Demo"}).print_settings("gone") == {}


def test_loading_a_template_with_an_unknown_key_says_which(tmp_path: Path) -> None:
    catalogue = _catalogue(tmp_path, {"name": "Demo", "print_settings": {"infill": "15%"}})

    with pytest.raises(InvalidModelMetaError, match="'infill' is not a print setting"):
        catalogue.print_settings("demo")
    with pytest.raises(InvalidModelMetaError, match="print_settings"):
        catalogue.record("demo")


def test_name_keychain_declares_its_print_defaults() -> None:
    raw = json.loads((MODELS / "name-keychain" / MODEL_META_NAME).read_text(encoding="utf-8"))

    assert meta_from_raw(raw, "name-keychain").print_settings == {
        "enable_prime_tower": "1",
        "wipe_tower_no_sparse_layers": "1",
        "enable_support": "0",
    }


@pytest.mark.parametrize("meta", sorted(MODELS.glob(f"*/{MODEL_META_NAME}")), ids=str)
def test_every_bundled_model_json_reads(meta: Path) -> None:
    meta_from_raw(json.loads(meta.read_text(encoding="utf-8")), meta.parent.name)
