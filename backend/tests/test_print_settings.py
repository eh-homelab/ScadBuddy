"""#770: a template's default slicer settings, ``print_settings`` in its model.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from scadbuddy.bambuddy.print_run import PrintRunRequest
from scadbuddy.core.paths import MODEL_META_NAME, DataPaths
from scadbuddy.library.catalogue import (
    Catalogue,
    PrintSequence,
    meta_from_raw,
)
from scadbuddy.render.solids import WRAPPER_PREFIX
from tests.support.media import MemoryMediaStore

MODELS = Path(__file__).resolve().parents[2] / "models"


def test_print_settings_keep_the_files_order() -> None:
    """#2998: no allowlist decides the order any more."""
    meta = meta_from_raw(
        {"print_settings": {"enable_support": "0", "enable_prime_tower": "1"}}, "demo"
    )

    assert list(meta.print_settings) == ["enable_support", "enable_prime_tower"]


def test_no_print_settings_is_empty() -> None:
    assert meta_from_raw({}, "demo").print_settings == {}
    assert meta_from_raw({"print_settings": None}, "demo").print_settings == {}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        # Keys outside the old allowlist (#2998): every Bambu Studio key is taken.
        ("layer_height", "0.2"),
        ("skirt_loops", "0"),
        ("support_interface_filament", "2"),
        ("a_key_bambu_studio_may_not_know", "anything"),
        # Values the allowlist refused: the slicer judges them now.
        ("enable_prime_tower", "true"),
        ("support_threshold_angle", "91"),
        ("brim_width", "1e2"),
        ("print_sequence", "by_object"),
    ],
)
def test_any_key_and_value_is_taken(key: str, value: str) -> None:
    assert meta_from_raw({"print_settings": {key: value}}, "demo").print_settings == {key: value}


@pytest.mark.parametrize(
    ("value", "stored"),
    [
        (0, "0"),
        (True, "1"),
        (False, "0"),
        (0.2, "0.2"),
        (5.0, "5"),
        ([1, 2], "1,2"),
        (["15%", "20%"], "15%,20%"),
    ],
)
def test_a_value_that_is_not_a_string_is_stored_as_bambu_does(value: object, stored: str) -> None:
    """Bambu's configs store every value as a string: a number, a boolean or a list is
    taken and written so (#2998), never refused."""
    meta = meta_from_raw({"print_settings": {"skirt_loops": value}}, "demo")

    assert meta.print_settings == {"skirt_loops": stored}


def test_a_value_that_is_no_setting_at_all_is_refused() -> None:
    with pytest.raises(ValidationError):
        meta_from_raw({"print_settings": {"enable_support": {"on": 1}}}, "demo")


def test_print_settings_that_are_not_an_object_are_refused() -> None:
    with pytest.raises(ValidationError):
        meta_from_raw({"print_settings": ["enable_support"]}, "demo")


def _catalogue(tmp_path: Path, meta: dict[str, object]) -> Catalogue:
    paths = DataPaths(tmp_path)
    paths.model_dir("demo").mkdir(parents=True)
    paths.model_source("demo").write_text("cube(1);\n", encoding="utf-8")
    paths.model_meta("demo").write_text(json.dumps(meta), encoding="utf-8")
    return Catalogue(paths, media_store=MemoryMediaStore(), wrapper_prefix=WRAPPER_PREFIX)


def test_the_catalogue_answers_a_templates_print_settings(tmp_path: Path) -> None:
    catalogue = _catalogue(tmp_path, {"name": "Demo", "print_settings": {"brim_width": "5"}})

    assert catalogue.print_settings("demo") == {"brim_width": "5"}
    assert catalogue.record("demo").print_settings == {"brim_width": "5"}


def test_a_template_that_is_gone_has_none(tmp_path: Path) -> None:
    assert _catalogue(tmp_path, {"name": "Demo"}).print_settings("gone") == {}


def test_loading_a_template_with_any_key_reads_it(tmp_path: Path) -> None:
    catalogue = _catalogue(tmp_path, {"name": "Demo", "print_settings": {"infill": "15%"}})

    assert catalogue.print_settings("demo") == {"infill": "15%"}


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


def test_a_prints_own_sequence_is_bambus_print_sequence() -> None:
    """#907: the per-print ``print_sequence`` shorthand takes Bambu's two values."""
    annotation: object = PrintRunRequest.model_fields["print_sequence"].annotation
    assert annotation == PrintSequence | None
    assert get_args(PrintSequence) == ("by layer", "by object")
