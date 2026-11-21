from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import TypeAdapter

from scadbuddy.filament.colour import Rgba, is_translucent, normalise_rgba, same_opacity
from scadbuddy.filament.models import (
    BrandCreate,
    BrandPatch,
    Colour,
    ColourCreate,
    EffectiveValues,
    LocationCreate,
    SpoolCreate,
    SpoolEventCreate,
    SpoolPatch,
    SpoolTypePatch,
    TagCreate,
)


def test_six_digits_get_full_alpha() -> None:
    assert normalise_rgba("ff8800") == "#FF8800FF"
    assert normalise_rgba("#ff8800") == "#FF8800FF"


def test_eight_digits_keep_alpha() -> None:
    assert normalise_rgba("#ff880080") == "#FF880080"
    assert normalise_rgba("FF880080") == "#FF880080"


@pytest.mark.parametrize("bad", ["#FFF", "", "#GGGGGG", "#FF88001", "#FF8800FF00", "red"])
def test_bad_colour_raises(bad: str) -> None:
    with pytest.raises(ValueError):
        normalise_rgba(bad)


def test_translucent() -> None:
    assert is_translucent("#FFFFFF80")
    assert not is_translucent("#FFFFFFFF")


#: The vectors `frontend/src/lib/rgba.test.ts` "sameOpacity" checks too (#6262 F11):
#: the dialog's `seedPlan` and this helper must agree. Change both lists together.
SAME_OPACITY = [
    ("#FFFFFF", "#FFFFFF80", False),
    ("#FFFFFF", None, True),
    (None, None, True),
    ("#FFFFFFFF", "#FFFFFF", True),
    ("#FFFFFF80", "#00000010", True),
    (None, "#FFFFFF80", False),
    ("#FFFFFF80", None, False),
    ("#ffffff80", "#FFFFFFFE", True),
]


@pytest.mark.parametrize(("left", "right", "same"), SAME_OPACITY)
def test_same_opacity(left: str | None, right: str | None, same: bool) -> None:
    """Spec §5.1, plan Decision 12: ``#RRGGBB`` and ``None`` read as opaque; false when
    exactly one side is translucent."""
    assert same_opacity(left, right) is same
    assert same_opacity(right, left) is same


def test_rgba_type_normalises() -> None:
    assert TypeAdapter(Rgba).validate_python("#abcdef") == "#ABCDEFFF"


def test_models_use_rgba() -> None:
    colour = ColourCreate(name="Red", rgba="#ff0000", extra_colours=["00ff00"])
    assert colour.rgba == "#FF0000FF"
    assert colour.extra_colours == ["#00FF00FF"]


def test_patch_clears_nullable() -> None:
    patch = SpoolPatch.model_validate({"colour_name": None})
    assert patch.model_fields_set == {"colour_name"}
    assert patch.colour_name is None


def test_patch_refuses_null_on_not_null_field() -> None:
    with pytest.raises(ValueError):
        SpoolPatch.model_validate({"weight_locked": None})
    with pytest.raises(ValueError):
        BrandPatch.model_validate({"name": None})
    with pytest.raises(ValueError):
        SpoolTypePatch.model_validate({"rgba": None})
    with pytest.raises(ValueError):
        SpoolTypePatch.model_validate({"label_weight_g": None})
    with pytest.raises(ValueError):
        BrandPatch.model_validate({"archived": None})


def test_unknown_field_refused() -> None:
    with pytest.raises(ValueError):
        BrandCreate.model_validate({"name": "A", "bogus": 1})
    with pytest.raises(ValueError):
        SpoolPatch.model_validate({"bogus": 1})


def test_spool_create_needs_type_or_material() -> None:
    with pytest.raises(ValueError):
        SpoolCreate.model_validate({})
    SpoolCreate.model_validate({"type_id": uuid4()})
    SpoolCreate.model_validate({"material_id": uuid4()})


def test_tag_create_needs_a_name() -> None:
    with pytest.raises(ValueError):
        TagCreate.model_validate({"kind": "other"})
    TagCreate.model_validate({"kind": "other", "tag_uid": "AB"})


def test_event_ref_id_needs_ref_kind() -> None:
    base = {"spool_id": uuid4(), "kind": "weighed"}
    with pytest.raises(ValueError):
        SpoolEventCreate.model_validate({**base, "ref_id": "x"})
    SpoolEventCreate.model_validate({**base, "ref_id": "x", "ref_kind": "run"})


def test_patch_normalises_rgba() -> None:
    assert SpoolPatch.model_validate({"rgba": "#abcdef"}).rgba == "#ABCDEFFF"
    assert SpoolTypePatch.model_validate({"extra_colours": ["abc123"]}).extra_colours == [
        "#ABC123FF"
    ]


def test_out_of_range_refused() -> None:
    with pytest.raises(ValueError):
        SpoolPatch.model_validate({"low_stock_pct": 101})
    with pytest.raises(ValueError):
        SpoolPatch.model_validate({"label_weight_g": 0})
    with pytest.raises(ValueError):
        BrandCreate.model_validate({"name": "A", "sort_order": 2**40})
    with pytest.raises(ValueError):
        LocationCreate.model_validate({"name": "A", "kind_id": uuid4(), "ams_id": 2**40})


def test_name_stripped_and_not_blank() -> None:
    assert BrandCreate(name="  Bambu ").name == "Bambu"
    with pytest.raises(ValueError):
        BrandCreate(name="   ")


def test_output_models_normalise_rgba() -> None:
    now = datetime.now(UTC)
    colour = Colour(
        id=uuid4(),
        created_at=now,
        updated_at=now,
        name="R",
        sort_order=0,
        archived_at=None,
        manufacturer=None,
        rgba="#ff0000",
        material_id=None,
        extra_colours=["00ff00"],
        effect_id=None,
    )
    assert colour.rgba == "#FF0000FF"
    assert colour.extra_colours == ["#00FF00FF"]
    assert EffectiveValues(rgba="123456").rgba == "#123456FF"
