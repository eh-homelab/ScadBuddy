import pytest
from pydantic import TypeAdapter

from scadbuddy.filament.colour import Rgba, is_translucent, normalise_rgba
from scadbuddy.filament.models import ColourCreate, SpoolPatch, SpoolTypeCreate


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


def test_rgba_type_normalises() -> None:
    assert TypeAdapter(Rgba).validate_python("#abcdef") == "#ABCDEFFF"


def test_models_use_rgba() -> None:
    colour = ColourCreate(name="Red", rgba="#ff0000", extra_colours=["00ff00"])
    assert colour.rgba == "#FF0000FF"
    assert colour.extra_colours == ["#00FF00FF"]


def test_patch_clears_nullable_and_rejects_null_required() -> None:
    patch = SpoolPatch.model_validate({"colour_name": None})
    assert patch.model_fields_set == {"colour_name"}
    with pytest.raises(ValueError):
        SpoolTypeCreate.model_validate({"code": "X"})
    with pytest.raises(ValueError):
        SpoolPatch.model_validate({"weight_locked": None})
