"""#6758: the prime tower judged before the slice, by the settings the print chose."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import cast

import trimesh

from scadbuddy.bambuddy.prime_tower import PrimeTowerChoice
from scadbuddy.bambuddy.print_run import PrintRunRequest, check_tower
from scadbuddy.bambuddy.print_source import PrintSource
from scadbuddy.bambuddy.tower_check import chosen_tower, tower_check
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
