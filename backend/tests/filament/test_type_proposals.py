"""Proposed spool types for the spools that need one (#6701, spec §8.1: "else SB proposes
a new type for a person to confirm")."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from psycopg import errors

from scadbuddy.filament import type_proposals
from scadbuddy.filament.bambuddy_pull import pull
from scadbuddy.filament.catalog import CatalogExistsError
from scadbuddy.filament.component import Filament
from scadbuddy.filament.models import (
    SpoolCreate,
    SpoolPatch,
    SpoolTypeCreate,
    TypeProposal,
    TypeProposalConfirm,
)
from tests.conftest import PgPool
from tests.filament.test_bambuddy_pull import NOW, _filament, _ids, _pla, bb

pytestmark = pytest.mark.requires_postgres

ACTOR = "person:ada"


def _spool(f: Filament, **over: Any) -> UUID:
    body: dict[str, Any] = {
        "material_id": _ids(f, "materials")["PLA"],
        "rgba": "#FF0000",
        "label_weight_g": 1000,
    } | over
    return f.spools.create(SpoolCreate.model_validate(body), actor="test").id


def _pick(group: TypeProposal, **over: Any) -> dict[str, Any]:
    keys = ("brand_id", "material_id", "subtype_id", "rgba", "label_weight_g", "material_number")
    return (
        {k: getattr(group, k) for k in keys}
        | {
            "code": group.code,
            "spool_ids": group.spool_ids,
        }
        | over
    )


def _confirm(f: Filament, *picks: dict[str, Any]) -> Any:
    body = TypeProposalConfirm.model_validate({"groups": list(picks)})
    return type_proposals.confirm(f, body, actor=ACTOR)


def test_spools_group_by_brand_material_subtype_colour_weight_and_number(
    pg_pool: PgPool,
) -> None:
    f = _filament(pg_pool)
    pull(
        f,
        [
            bb(id=1, tray_uuid=None, tag_uid=None),
            bb(id=2, tray_uuid=None, tag_uid=None),
            bb(id=3, tray_uuid=None, tag_uid=None, label_weight=250),
            bb(id=4, tray_uuid=None, tag_uid=None, material_number="GFA00"),
            bb(id=5, tray_uuid=None, tag_uid=None, rgba="FF000080", color_name=None),
        ],
        now=NOW,
    )
    found = type_proposals.proposals(f)
    by_count = sorted((g.count, g.code) for g in found.groups)
    # The two with no number fold into the one group of their key that has one (R-6729-1).
    assert by_count == [
        (1, "BAMBU-LAB-PLA-BASIC-FF000080"),
        (1, "BAMBU-LAB-PLA-BASIC-RED"),
        (3, "GFA00"),
    ]
    first = found.groups[0]
    assert first.count == 3 and len(first.spool_ids) == 3 and len(first.numbers) == 3
    assert first.numbers == sorted(first.numbers)
    assert first.material_number == "GFA00"
    assert (first.brand, first.material, first.subtype) == ("Bambu Lab", "PLA", "Basic")
    assert (first.rgba, first.label_weight_g, first.colour_name) == ("#FF0000FF", 1000, "Red")
    assert first.type_id is None
    assert found.ungrouped == []


def test_a_spool_missing_colour_or_label_weight_is_listed_with_what_is_missing(
    pg_pool: PgPool,
) -> None:
    f = _filament(pg_pool)
    no_colour = _spool(f, rgba=None)
    neither = _spool(f, rgba=None, label_weight_g=None)
    _spool(f)
    found = type_proposals.proposals(f)
    assert [g.count for g in found.groups] == [1]
    missing = {u.spool_id: u.missing for u in found.ungrouped}
    assert missing == {no_colour: ["rgba"], neither: ["rgba", "label_weight_g"]}


def test_typed_and_archived_spools_are_not_proposed(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    archived = _spool(f)
    f.spools.archive(archived, True, actor="test")
    kind = f.types.create(
        SpoolTypeCreate(
            code="PLA-RED", material_id=_pla(pg_pool), rgba="#FF0000", label_weight_g=1000
        )
    )
    _spool(f, type_id=kind.id)
    assert type_proposals.proposals(f).groups == []


def test_a_proposal_names_a_type_that_already_matches(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    _spool(f)
    kind = f.types.create(
        SpoolTypeCreate(code="MINE", material_id=_pla(pg_pool), rgba="#FF0000", label_weight_g=1000)
    )
    (group,) = type_proposals.proposals(f).groups
    assert (group.type_id, group.code) == (kind.id, "MINE")


def test_a_code_another_type_holds_gets_the_label_weight(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    f.types.create(
        SpoolTypeCreate(
            code="PLA-FF0000", material_id=_pla(pg_pool), rgba="#00FF00", label_weight_g=1000
        )
    )
    _spool(f)
    (group,) = type_proposals.proposals(f).groups
    assert group.code == "PLA-FF0000-1000G"


def _shared_number(f: Filament) -> list[TypeProposal]:
    """A red and a blue spool whose Bambuddy readings carry one ``material_number``."""
    pull(
        f,
        [
            bb(id=1, tray_uuid=None, tag_uid=None, material_number="X1"),
            bb(
                id=2,
                tray_uuid=None,
                tag_uid=None,
                material_number="X1",
                rgba="0000FFFF",
                color_name="Blue",
            ),
        ],
        now=NOW,
    )
    groups = type_proposals.proposals(f).groups
    assert [(g.code, g.rgba, g.type_id) for g in groups] == [
        ("X1", "#FF0000FF", None),
        ("X1-1000G", "#0000FFFF", None),
    ]
    return groups


def _typed_as(f: Filament, group: TypeProposal) -> tuple[str, str]:
    (spool_id,) = group.spool_ids
    spool = f.spools.get(spool_id)
    assert spool is not None and spool.type_id is not None
    kind = f.types.get(spool.type_id)
    assert kind is not None
    return kind.code, kind.rgba


def test_groups_that_share_a_material_number_each_get_their_own_type(pg_pool: PgPool) -> None:
    """R-6701-8: a code match is used only when the type is of the group's key."""
    f = _filament(pg_pool)
    red, blue = _shared_number(f)
    done = _confirm(f, _pick(red), _pick(blue)).groups
    assert [(r.code, r.made) for r in done] == [("X1", True), ("X1-1000G", True)]
    assert _typed_as(f, red) == ("X1", "#FF0000FF")
    assert _typed_as(f, blue) == ("X1-1000G", "#0000FFFF")


def test_a_shared_material_number_confirmed_one_group_at_a_time(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    red, _ = _shared_number(f)
    (first,) = _confirm(f, _pick(red)).groups
    assert (first.code, first.made) == ("X1", True)
    # The reload proposes a new type for blue, not the red type that holds the code.
    (blue,) = type_proposals.proposals(f).groups
    assert (blue.code, blue.type_id, blue.rgba) == ("X1-1000G", None, "#0000FFFF")
    (second,) = _confirm(f, _pick(blue)).groups
    assert (second.code, second.made) == ("X1-1000G", True)
    assert _typed_as(f, red) == ("X1", "#FF0000FF")
    assert _typed_as(f, blue) == ("X1-1000G", "#0000FFFF")


def test_a_code_match_of_the_key_is_used(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    red, _ = _shared_number(f)
    kind = f.types.create(
        SpoolTypeCreate(
            code="x1",
            brand_id=red.brand_id,
            material_id=red.material_id,
            subtype_id=red.subtype_id,
            rgba="#FF0000",
            label_weight_g=1000,
        )
    )
    groups = type_proposals.proposals(f).groups
    assert [(g.code, g.type_id) for g in groups] == [("x1", kind.id), ("X1-1000G", None)]
    done = _confirm(f, *(_pick(g) for g in groups)).groups
    assert [(r.type_id == kind.id, r.made) for r in done] == [(True, False), (False, True)]


def test_confirm_makes_the_type_and_types_the_group(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    pull(
        f,
        [bb(id=1, tray_uuid=None, tag_uid=None), bb(id=2, tray_uuid=None, tag_uid=None)],
        now=NOW,
    )
    (group,) = type_proposals.proposals(f).groups
    done = _confirm(f, _pick(group, code="My red"))
    (result,) = done.groups
    assert result.made is True
    assert result.code == "My red"
    assert sorted(result.typed) == sorted(group.spool_ids)
    assert result.skipped == []
    kind = f.types.get(result.type_id)
    assert kind is not None
    assert (kind.code, kind.rgba, kind.label_weight_g, kind.colour_name) == (
        "My red",
        "#FF0000FF",
        1000,
        "Red",
    )
    assert kind.cost_per_kg is None
    for spool_id in group.spool_ids:
        spool = f.spools.get(spool_id)
        assert spool is not None
        assert spool.type_id == kind.id
        # Each override equal to the type's value is dropped; the spool reads the type's.
        assert (spool.material_id, spool.brand_id, spool.subtype_id) == (None, None, None)
        assert (spool.rgba, spool.label_weight_g, spool.colour_name) == (None, None, None)
        assert spool.effective.rgba == "#FF0000FF"
        edited = [e for e in f.spools.events(spool_id) if e.kind == "edited"]
        assert len(edited) == 1
        assert edited[0].actor == ACTOR
        changes = edited[0].data["changes"]
        assert changes["type_id"] == [None, str(kind.id)]
        assert changes["rgba"] == ["#FF0000FF", None]
    assert type_proposals.proposals(f).groups == []


def test_confirm_keeps_an_override_that_differs_from_the_type(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    hot = _spool(f, nozzle_temp_min=220)
    cool = _spool(f, nozzle_temp_min=200)
    (group,) = type_proposals.proposals(f).groups
    (result,) = _confirm(f, _pick(group)).groups
    kind = f.types.get(result.type_id)
    assert kind is not None
    # The spools do not share it, so the type does not take it, and each keeps its own.
    assert kind.nozzle_temp_min is None
    for spool_id, temp in ((hot, 220), (cool, 200)):
        spool = f.spools.get(spool_id)
        assert spool is not None
        assert spool.nozzle_temp_min == temp


def test_confirm_takes_the_values_every_spool_shares(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    ids = [_spool(f, nozzle_temp_max=230, density_g_cm3="1.24") for _ in range(2)]
    (group,) = type_proposals.proposals(f).groups
    (result,) = _confirm(f, _pick(group)).groups
    kind = f.types.get(result.type_id)
    assert kind is not None
    assert (kind.nozzle_temp_max, kind.density_g_cm3) == (230, Decimal("1.24"))
    for spool_id in ids:
        spool = f.spools.get(spool_id)
        assert spool is not None
        assert (spool.nozzle_temp_max, spool.density_g_cm3) == (None, None)
        assert spool.effective.nozzle_temp_max == 230


def test_confirm_uses_a_type_that_matches_now_instead_of_a_duplicate(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    one = _spool(f)
    (group,) = type_proposals.proposals(f).groups
    kind = f.types.create(
        SpoolTypeCreate(
            code="SOMEONE-ELSE", material_id=_pla(pg_pool), rgba="#FF0000", label_weight_g=1000
        )
    )
    (result,) = _confirm(f, _pick(group)).groups
    assert (result.made, result.type_id, result.code, result.typed) == (
        False,
        kind.id,
        "SOMEONE-ELSE",
        [one],
    )
    assert [t.code for t in f.types.list()] == ["SOMEONE-ELSE"]


def test_confirm_skips_a_spool_that_gained_a_type_or_changed(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    typed, archived, changed, kept = (_spool(f) for _ in range(4))
    (group,) = type_proposals.proposals(f).groups
    other = f.types.create(
        SpoolTypeCreate(
            code="OTHER", material_id=_pla(pg_pool), rgba="#0000FF", label_weight_g=1000
        )
    )
    f.spools.update(typed, SpoolPatch(type_id=other.id), actor="test")
    f.spools.archive(archived, True, actor="test")
    f.spools.update(changed, SpoolPatch(rgba="#00FF00"), actor="test")
    (result,) = _confirm(f, _pick(group)).groups
    assert result.made is True
    assert result.typed == [kept]
    assert {(s.spool_id, s.reason) for s in result.skipped} == {
        (typed, "typed"),
        (archived, "archived"),
        (changed, "changed"),
    }
    spool = f.spools.get(typed)
    assert spool is not None and spool.type_id == other.id


def test_a_group_with_no_spool_left_makes_no_type(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    only = _spool(f)
    (group,) = type_proposals.proposals(f).groups
    f.spools.archive(only, True, actor="test")
    (result,) = _confirm(f, _pick(group)).groups
    assert (result.made, result.type_id, result.typed) == (False, None, [])
    assert f.types.list() == []


def test_a_clashing_code_writes_nothing(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    red = _spool(f)
    _spool(f, rgba="#00FF00")
    f.types.create(
        SpoolTypeCreate(
            code="TAKEN", material_id=_pla(pg_pool), rgba="#0000FF", label_weight_g=1000
        )
    )
    groups = type_proposals.proposals(f).groups
    picks = [_pick(groups[0], code="FINE"), _pick(groups[1], code="taken")]
    with pytest.raises(CatalogExistsError):
        _confirm(f, *picks)
    assert [t.code for t in f.types.list()] == ["TAKEN"]
    spool = f.spools.get(red)
    assert spool is not None and spool.type_id is None


def test_a_group_with_no_number_folds_into_the_one_numbered_group_of_its_key(
    pg_pool: PgPool,
) -> None:
    """R-6729-1: a hand-added spool and a Bambuddy one of one key share one type."""
    f = _filament(pg_pool)
    pull(f, [bb(id=1, tray_uuid=None, tag_uid=None, material_number="GFA00")], now=NOW)
    (linked,) = type_proposals.proposals(f).groups
    hand = _spool(
        f,
        brand_id=linked.brand_id,
        subtype_id=linked.subtype_id,
        rgba="#FF0000FF",
        colour_name="Red",
    )
    (group,) = type_proposals.proposals(f).groups
    assert (group.code, group.material_number, group.count) == ("GFA00", "GFA00", 2)
    assert hand in group.spool_ids
    (result,) = _confirm(f, _pick(group)).groups
    assert (result.code, result.made, result.new, result.skipped) == ("GFA00", True, True, [])
    assert sorted(result.typed) == sorted(group.spool_ids)


def _two_numbers_and_none(f: Filament) -> list[TypeProposal]:
    pull(
        f,
        [
            bb(id=1, tray_uuid=None, tag_uid=None, material_number="N1"),
            bb(id=2, tray_uuid=None, tag_uid=None, material_number="N2"),
            bb(id=3, tray_uuid=None, tag_uid=None),
        ],
        now=NOW,
    )
    return type_proposals.proposals(f).groups


def test_a_group_with_no_number_stays_apart_when_two_numbered_groups_match(
    pg_pool: PgPool,
) -> None:
    f = _filament(pg_pool)
    groups = _two_numbers_and_none(f)
    assert sorted((g.material_number or "", g.count) for g in groups) == [
        ("", 1),
        ("N1", 1),
        ("N2", 1),
    ]


def test_confirm_says_when_a_group_joined_a_type_another_group_just_made(
    pg_pool: PgPool,
) -> None:
    """R-6729-1: the second and third groups land on the first one's new type."""
    f = _filament(pg_pool)
    groups = sorted(_two_numbers_and_none(f), key=lambda g: g.material_number or "~")
    picks = [_pick(g, code=f"MINE-{at}") for at, g in enumerate(groups)]
    done = _confirm(f, *picks).groups
    assert [(r.code, r.made, r.new) for r in done] == [
        ("MINE-0", True, True),
        ("MINE-0", False, True),
        ("MINE-0", False, True),
    ]
    assert len({r.type_id for r in done}) == 1


def test_a_type_that_existed_is_not_new(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    _spool(f)
    f.types.create(
        SpoolTypeCreate(code="MINE", material_id=_pla(pg_pool), rgba="#FF0000", label_weight_g=1000)
    )
    (group,) = type_proposals.proposals(f).groups
    (result,) = _confirm(f, _pick(group)).groups
    assert (result.code, result.made, result.new) == ("MINE", False, False)


def test_a_material_number_over_128_characters_is_proposed_with_no_number(
    pg_pool: PgPool,
) -> None:
    """R-6729-3: a cut code could collide, so a long one is not proposed at all."""
    f = _filament(pg_pool)
    pull(f, [bb(id=1, tray_uuid=None, tag_uid=None, material_number="X" * 129)], now=NOW)
    (group,) = type_proposals.proposals(f).groups
    assert (group.material_number, group.code) == (None, "BAMBU-LAB-PLA-BASIC-RED")
    (result,) = _confirm(f, _pick(group)).groups
    assert (result.made, result.skipped) == (True, [])


def test_another_unique_violation_is_not_a_code_clash(
    pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-6729-2: only the code's unique index is the 409."""
    f = _filament(pg_pool)
    _spool(f)
    (group,) = type_proposals.proposals(f).groups
    create_on = f.types.create_on

    def clash(conn: Any, body: Any) -> Any:
        conn.execute("INSERT INTO filament_materials (name) VALUES ('pla')")
        return create_on(conn, body)

    monkeypatch.setattr(f.types, "create_on", clash)
    with pytest.raises(errors.UniqueViolation):
        _confirm(f, _pick(group))
    assert f.types.list() == []


def test_a_spool_with_no_number_in_a_numbered_pick_is_changed_when_two_groups_match(
    pg_pool: PgPool,
) -> None:
    """R-6729-5: the confirm takes a spool with no number into a numbered pick only under
    the fold's rule, exactly one numbered group of its other key fields."""
    f = _filament(pg_pool)
    groups = _two_numbers_and_none(f)
    n1 = next(g for g in groups if g.material_number == "N1")
    (bare,) = next(g for g in groups if g.material_number is None).spool_ids
    (result,) = _confirm(f, _pick(n1, spool_ids=[*n1.spool_ids, bare])).groups
    assert result.typed == n1.spool_ids
    assert [(s.spool_id, s.reason) for s in result.skipped] == [(bare, "changed")]
