"""#6758: the prime tower judged before the slice, by the settings the print chose."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import cast

import pytest
import trimesh

from scadbuddy.bambuddy import tower_check as tower_check_module
from scadbuddy.bambuddy.prime_tower import PrimeTowerChoice
from scadbuddy.bambuddy.print_run import PrintRunRequest, check_tower
from scadbuddy.bambuddy.print_source import PrintSource
from scadbuddy.bambuddy.tower_check import chosen_tower, judge_towers, tower_check
from scadbuddy.render.bambu3mf import PROJECT_SETTINGS_NAME, PlateParts, rezipped, write_plates_3mf
from scadbuddy.render.plate import DEFAULT_PLATE, TowerSettings, plate_for
from scadbuddy.render.split import ColourPart

H2C = plate_for("H2C")
NO_SPARSE = PrimeTowerChoice(enabled=True, no_sparse_layers=True)


def _author_file(tmp_path: Path, tower: tuple[str, str]) -> bytes:
    """Knitted Cat's shape (#3770): an author's two-colour H2C file, its 30 mm object at
    x 160..190, y 145..175, and its tower where the author put it."""
    body = trimesh.creation.box(extents=(30, 30, 20))
    eye = trimesh.creation.box(extents=(4, 4, 4))
    plates = [
        PlateParts(
            (ColourPart(1, "Body", "#D3CFB5", body), ColourPart(2, "Eye", "#27272C", eye)), (1, 2)
        )
    ]
    out = tmp_path / "cat.3mf"
    write_plates_3mf(plates, ["#D3CFB5", "#27272C"], out, thumbnails=None, plate=H2C)
    payload = out.read_bytes()
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        settings = json.loads(archive.read(PROJECT_SETTINGS_NAME))
    settings.update(
        print_settings_id="0.20mm Standard @BBL H2C",
        wipe_tower_x=[tower[0]],
        wipe_tower_y=[tower[1]],
    )
    return rezipped(payload, {PROJECT_SETTINGS_NAME: json.dumps(settings).encode()})


def test_no_sparse_layers_chosen_in_the_dialog_finds_the_keep_out_and_a_free_spot(
    tmp_path: Path,
) -> None:
    """The file's own settings pass the send (#6691), so the send keeps the tower 10 mm
    from the cat. "No sparse layers" chosen in the dialog needs about 100 mm."""
    payload = _author_file(tmp_path, ("200", "150"))
    by_file = tower_check(payload, H2C, choice=None, overrides={}, template={}, plates=None)
    assert by_file is not None and by_file.conflicts == []

    check = tower_check(payload, H2C, choice=NO_SPARSE, overrides={}, template={}, plates=None)

    assert check is not None
    [conflict] = check.conflicts
    assert (conflict.plate_id, conflict.reason) == (1, "keep-out")
    assert (conflict.x, conflict.y) == (200.0, 150.0)
    assert conflict.move_x is not None and conflict.move_y is not None
    assert check.fix is not None
    assert set(check.fix) == {"wipe_tower_x", "wipe_tower_y"}
    assert [len(values) for values in check.fix.values()] == [1, 1]
    # The fix, sent as process overrides, leaves nothing to refuse.
    fixed = tower_check(
        payload, H2C, choice=NO_SPARSE, overrides=dict(check.fix), template={}, plates=None
    )
    assert fixed is not None and fixed.conflicts == [] and fixed.fix is None


def test_a_tower_on_the_objects_is_said_to_stand_on_them(tmp_path: Path) -> None:
    payload = _author_file(tmp_path, ("200", "150"))
    on = {"wipe_tower_x": "165", "wipe_tower_y": "150"}

    check = tower_check(payload, H2C, choice=None, overrides=on, template={}, plates=None)

    assert check is not None
    [conflict] = check.conflicts
    assert conflict.reason == "on-objects" and (conflict.x, conflict.y) == (165.0, 150.0)


def test_the_templates_tower_place_is_judged_under_the_overrides(tmp_path: Path) -> None:
    """#6796: a template's `print_settings` that place the tower are what the slice
    uses, so the check judges the tower there. The request's overrides win over them."""
    payload = _author_file(tmp_path, ("200", "150"))
    on = {"wipe_tower_x": "165", "wipe_tower_y": "150"}

    check = tower_check(payload, H2C, choice=None, overrides={}, template=on, plates=None)

    assert check is not None
    [conflict] = check.conflicts
    assert conflict.reason == "on-objects" and (conflict.x, conflict.y) == (165.0, 150.0)
    away = {"wipe_tower_x": "200", "wipe_tower_y": "150"}
    moved = tower_check(payload, H2C, choice=None, overrides=away, template=on, plates=None)
    assert moved is not None and moved.conflicts == []


def test_only_the_plates_printed_are_judged(tmp_path: Path) -> None:
    payload = _author_file(tmp_path, ("200", "150"))
    check = tower_check(payload, H2C, choice=NO_SPARSE, overrides={}, template={}, plates=[2])
    assert check is not None and check.conflicts == [] and check.fix is None


def test_no_tower_or_no_printer_has_nothing_to_judge(tmp_path: Path) -> None:
    payload = _author_file(tmp_path, ("165", "150"))
    off = PrimeTowerChoice(enabled=False)
    assert tower_check(payload, H2C, choice=off, overrides={}, template={}, plates=None) is None
    assert (
        tower_check(
            payload,
            H2C,
            choice=None,
            overrides={"enable_prime_tower": "0"},
            template={},
            plates=None,
        )
        is None
    )
    assert (
        tower_check(
            payload,
            H2C,
            choice=None,
            overrides={},
            template={"enable_prime_tower": "0"},
            plates=None,
        )
        is None
    )
    assert (
        tower_check(payload, DEFAULT_PLATE, choice=None, overrides={}, template={}, plates=None)
        is None
    )


def _without_settings(payload: bytes) -> bytes:
    """``payload`` with no project settings, as an author's file may come."""
    out = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(payload)) as source,
        zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for name in source.namelist():
            if name != PROJECT_SETTINGS_NAME:
                target.writestr(name, source.read(name))
    return out.getvalue()


def test_a_file_whose_settings_cannot_be_read_has_no_conflicts(tmp_path: Path) -> None:
    """No project settings, settings that are not JSON, or no zip at all: the check says
    nothing, and the slicer says why it refuses the file."""
    payload = _author_file(tmp_path, ("165", "150"))
    unreadable = [
        _without_settings(payload),
        rezipped(payload, {PROJECT_SETTINGS_NAME: b"not json"}),
        b"not a zip",
    ]
    for laid_out in unreadable:
        check = judge_towers(laid_out, H2C, None, {}, None)
        assert check.conflicts == [] and check.fix is None


def test_unreadable_settings_in_the_laid_out_file_have_nothing_to_judge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#6813: settings the laid-out file holds that cannot be read (not JSON, not an
    object, or gone) leave the check silent, as `judge_towers` is, not failing the dialog."""
    payload = _author_file(tmp_path, ("200", "150"))
    for laid in (
        rezipped(payload, {PROJECT_SETTINGS_NAME: b"not json"}),
        rezipped(payload, {PROJECT_SETTINGS_NAME: b"[1, 2]"}),
        _without_settings(payload),
    ):
        monkeypatch.setattr(tower_check_module, "_laid_out_for", lambda _p, _t, b=laid: b)
        tower_check_module.forget_laid_out()
        check = tower_check(payload, H2C, choice=NO_SPARSE, overrides={}, template={}, plates=None)
        assert check is None


def test_a_second_check_of_the_same_file_and_printer_does_not_lay_it_out_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#6796: the dialog checks on every setting change. The laid-out file is kept by
    the payload's digest and the printer, so only a new file or printer lays it out."""
    payload = _author_file(tmp_path, ("200", "150"))
    real = tower_check_module._laid_out_for
    calls: list[str | None] = []

    def counted(given: bytes, target: object) -> bytes:
        calls.append(target.plate.model)  # type: ignore[attr-defined]
        return real(given, target)  # type: ignore[arg-type]

    monkeypatch.setattr(tower_check_module, "_laid_out_for", counted)
    tower_check_module.forget_laid_out()

    first = tower_check(payload, H2C, choice=NO_SPARSE, overrides={}, template={}, plates=None)
    again = tower_check(payload, H2C, choice=NO_SPARSE, overrides={}, template={}, plates=None)
    assert first == again and len(calls) == 1
    tower_check(payload, plate_for("H2D"), choice=NO_SPARSE, overrides={}, template={}, plates=None)
    assert calls == [H2C.model, plate_for("H2D").model]
    other = _author_file(tmp_path, ("190", "150"))
    tower_check(other, H2C, choice=NO_SPARSE, overrides={}, template={}, plates=None)
    assert len(calls) == 3


def test_the_laid_out_files_kept_are_bounded(tmp_path: Path) -> None:
    """The cache keeps at most `LAID_OUT_KEPT` files and `LAID_OUT_BYTES` in all."""
    tower_check_module.forget_laid_out()
    for n in range(tower_check_module.LAID_OUT_KEPT + 3):
        tower_check_module._keep_laid_out((str(n), H2C, False), b"x")
    assert len(tower_check_module._LAID_OUT) == tower_check_module.LAID_OUT_KEPT
    big = b"x" * (tower_check_module.LAID_OUT_BYTES // 2 + 1)
    tower_check_module._keep_laid_out(("a", H2C, False), big)
    tower_check_module._keep_laid_out(("b", H2C, False), big)
    assert list(tower_check_module._LAID_OUT) == [("b", H2C, False)]
    tower_check_module._keep_laid_out(("c", H2C, False), big + big)
    assert ("c", H2C, False) not in tower_check_module._LAID_OUT
    tower_check_module.forget_laid_out()


def test_an_explicit_override_wins_over_the_choice_and_the_template() -> None:
    stated = TowerSettings(filaments=3)
    assert chosen_tower(stated, NO_SPARSE, {}, {}).no_sparse_layers
    assert not chosen_tower(
        stated, NO_SPARSE, {"wipe_tower_no_sparse_layers": "0"}, {}
    ).no_sparse_layers
    assert chosen_tower(stated, None, {}, {"wipe_tower_no_sparse_layers": "1"}).no_sparse_layers
    walls = chosen_tower(stated, None, {"prime_tower_rib_wall": "0", "prime_tower_width": "50"}, {})
    assert (walls.rib_wall, walls.width, walls.filaments) == (False, 50.0, 3)


class _Source:
    """The little of a `PrintSource` that `check_tower` reads."""

    def __init__(self, payload: bytes, *, sliced: bool = False) -> None:
        self.payload = payload
        self.is_sliced = sliced
        self.print_settings: dict[str, str] = {}

    async def fetched_3mf(self, client: object) -> bytes:
        return self.payload


def _request(**fields: object) -> PrintRunRequest:
    return PrintRunRequest.model_validate(
        {
            "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]},
            "choices": {"nozzles": [{"size": "0.4"}], "tier": "standard"},
            **fields,
        }
    )


async def test_the_check_judges_the_tower_the_request_chooses(tmp_path: Path) -> None:
    """The dialog's check (`PrintCheck.prime_tower`) and the agent read this: the
    request's `prime_tower` and `process_overrides`, on the printer's plate."""
    source = cast(PrintSource, _Source(_author_file(tmp_path, ("200", "150"))))
    chosen = _request(prime_tower={"enabled": True, "no_sparse_layers": True})

    check = await check_tower(None, source, chosen, "H2C")

    assert check is not None and [c.reason for c in check.conflicts] == ["keep-out"]
    assert check.fix is not None
    moved = _request(
        prime_tower={"enabled": True, "no_sparse_layers": True}, process_overrides=check.fix
    )
    again = await check_tower(None, source, moved, "H2C")
    assert again is not None and again.conflicts == []
    assert await check_tower(None, source, chosen, None) is None
    sliced = cast(PrintSource, _Source(b"", sliced=True))
    assert await check_tower(None, sliced, chosen, "H2C") is None
