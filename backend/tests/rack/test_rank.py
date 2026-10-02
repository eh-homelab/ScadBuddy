"""``rank_rack`` and its helpers (#836, spec 2026-10-01 §3, §4, §5, §9)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from scadbuddy.bambuddy.filaments import SpoolOption
from scadbuddy.bambuddy.models import NozzleRackSlot
from scadbuddy.rack import rank
from scadbuddy.rack.rank import (
    NOZZLE_MATERIALS,
    Usage,
    abrasive_type,
    candidates_for,
    eligible,
    manual_for,
    rack_groups,
    rack_positions,
    rack_warnings,
    rank_rack,
)
from tests.rack.helpers import group, mounted, requirement, serial, slot


def positions(picks: dict[int, rank.Pick]) -> dict[int, int]:
    return {group_id: pick.position for group_id, pick in picks.items()}


# --- eligibility ------------------------------------------------------------------


def test_a_rack_point_two_matches_a_group_point_two_zero() -> None:
    assert eligible(slot(2, "HS00", "0.2"), "0.20", "Standard")
    assert not eligible(slot(2, "HS00", "0.2"), "0.40", "Standard")


def test_high_flow_matches_only_hh_and_standard_only_the_rest() -> None:
    assert eligible(slot(2, "HH01"), "0.40", "High Flow")
    assert not eligible(slot(2, "HS01"), "0.40", "High Flow")
    assert eligible(slot(2, "HS01"), "0.40", " standard ")
    assert not eligible(slot(2, "HH01"), "0.40", "Standard")


def test_a_missing_code_or_flow_name_matches_either() -> None:
    assert eligible(slot(2, ""), "0.40", "High Flow")
    assert eligible(slot(2, ""), "0.40", "Standard")
    assert eligible(slot(2, "HH01"), "0.40", "")


def test_an_unparsable_diameter_is_never_eligible() -> None:
    assert not eligible(slot(2, "HS01", "abc"), "0.40", "Standard")
    assert not eligible(slot(2, "HS01", ""), "0.40", "Standard")
    assert not eligible(slot(2), "abc", "Standard")


def test_the_mounted_hotend_fills_the_one_missing_position_only() -> None:
    """Review Focus 3: recovered as Bambuddy's ``_rack_by_position`` does."""
    one_missing = [mounted(), *(slot(p) for p in (2, 3, 4, 5, 6))]
    assert rack_positions(one_missing)[1].id == 0
    two_missing = [mounted(), *(slot(p) for p in (3, 4, 5, 6))]
    found = rack_positions(two_missing)
    assert sorted(found) == [3, 4, 5, 6]
    assert all(entry.id != 0 for entry in found.values())


# --- materials --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filament_type", "abrasive"),
    [
        ("PLA-CF", True),
        ("PA6-CF", True),
        ("ABS-GF", True),
        ("petg cf", True),
        ("PLA", False),
        ("PLA-AERO", False),
        ("", False),
        (None, False),
    ],
)
def test_cf_and_gf_tokens_are_abrasive(filament_type: str | None, abrasive: bool) -> None:
    assert abrasive_type(filament_type) is abrasive


def test_the_material_table_ships_empty_so_every_code_is_unknown() -> None:
    assert NOZZLE_MATERIALS == {}
    picks = rank_rack(
        [group(abrasive=True, materials=("PLA-CF",))],
        [slot(2, "HH01"), slot(3, "HS01")],
        "least_used",
        {},
        {},
    )
    assert picks[0].unsafe_material is True


def test_an_unknown_code_is_not_hardened(hardened_code: str) -> None:
    assert rank.hardened(hardened_code)
    assert not rank.hardened("HZ42")


def test_material_ranks_above_color(hardened_code: str) -> None:
    rack = [slot(2, "HS01", color="FF6A13FF"), slot(3, hardened_code)]
    abrasive = group(color="#FF6A13", abrasive=True, materials=("PLA-CF",))
    plain = group(color="#FF6A13")
    assert positions(rank_rack([abrasive], rack, "least_used", {}, {})) == {0: 3}
    # On a plate with no abrasive filament the brass nozzle wins even without the color.
    rack = [slot(2, hardened_code, color="FF6A13FF"), slot(3, "HS01")]
    assert positions(rank_rack([plain], rack, "least_used", {}, {})) == {0: 3}


def test_color_ranks_above_use() -> None:
    rack = [slot(2, color="FF6A13FF"), slot(3)]
    usage = {serial(17): Usage(prints=40, print_seconds=360_000)}
    picks = rank_rack([group(color="#FF6A13")], rack, "least_used", usage, {})
    assert positions(picks) == {0: 2}
    assert picks[0].reason == "already loaded with this color"


def test_a_hash_rgb_group_matches_an_rgba_slot_and_empty_never_matches() -> None:
    rack = [slot(2), slot(3, color="00B1B7FF")]
    assert positions(rank_rack([group(color="#00B1B7")], rack, "least_used", {}, {})) == {0: 3}
    # No group color never matches a hotend with no color, whether the rack reports
    # it empty or as a zero alpha: the lower position, with a real color, still wins.
    for empty in ("", "00000000"):
        rack = [slot(2, color="00B1B7FF"), slot(3, color=empty)]
        picks = rank_rack([group(color=None)], rack, "least_used", {}, {})
        assert positions(picks) == {0: 2}
        assert picks[0].reason == "lowest free position"


def test_an_empty_hotends_zero_alpha_color_never_matches_black() -> None:
    """Review Focus 1: the rack reports ``00000000`` for a hotend with no filament."""
    rack = [slot(2, color="00000000"), slot(3, color="000000FF")]
    picks = rank_rack([group(color="#000000")], rack, "least_used", {}, {})
    assert positions(picks) == {0: 3}
    assert picks[0].reason == "already loaded with this color"


# --- algorithms -------------------------------------------------------------------


def test_least_used_orders_by_print_seconds_then_prints() -> None:
    rack = [slot(2), slot(3), slot(4)]
    usage = {
        serial(17): Usage(prints=3, print_seconds=500),
        serial(18): Usage(prints=1, print_seconds=500),
        serial(19): Usage(prints=1, print_seconds=900),
    }
    picks = rank_rack([group()], rack, "least_used", usage, {})
    assert positions(picks) == {0: 3}
    assert picks[0].reason == "least used"


def test_an_unused_hotend_ranks_before_a_used_one() -> None:
    """Spec §4: the ``coalesce`` gives a never-used serial zeros, never a missing entry."""
    rack = [slot(2), slot(3)]
    usage = {serial(17): Usage(prints=1, print_seconds=60), serial(18): Usage()}
    assert positions(rank_rack([group()], rack, "least_used", usage, {})) == {0: 3}


def test_oldest_and_newest_first_order_on_first_seen_with_the_unseen_last_and_first() -> None:
    rack = [slot(2), slot(3), slot(4)]
    usage = {
        serial(17): Usage(first_seen_at=datetime(2026, 9, 1, tzinfo=UTC)),
        serial(18): Usage(first_seen_at=datetime(2026, 8, 1, tzinfo=UTC)),
        serial(19): Usage(first_seen_at=None),
    }
    oldest = candidates_for(group(), rack, "oldest_first", usage)
    newest = candidates_for(group(), rack, "newest_first", usage)
    assert [c.position for c in oldest] == [3, 2, 4]
    assert [c.position for c in newest] == [4, 2, 3]


def test_the_position_breaks_every_other_tie() -> None:
    picks = rank_rack([group()], [slot(5), slot(3), slot(4)], "least_used", {}, {})
    assert positions(picks) == {0: 3}
    assert picks[0].reason == "lowest free position"


def test_let_bambuddy_pick_sends_no_choice() -> None:
    assert rank_rack([group()], [slot(2), slot(3)], "bambuddy", {}, {}) == {}


def test_let_bambuddy_pick_warns_whenever_bambuddy_could_take_a_non_hardened_nozzle(
    hardened_code: str,
) -> None:
    cf = group(abrasive=True, materials=("PLA-CF",))

    def kinds(*rack: NozzleRackSlot) -> list[str]:
        return [w.kind for w in rack_warnings([cf], list(rack), "bambuddy", {}, {})]

    assert kinds(slot(2), slot(3)) == ["rack-unsafe-material"]
    assert kinds(slot(2, hardened_code), slot(3)) == ["rack-unsafe-material"]
    assert kinds(slot(2, hardened_code), slot(3, hardened_code)) == []


# --- allocation -------------------------------------------------------------------


def test_no_eligible_position_gives_no_pick_and_says_so() -> None:
    picks = rank_rack([group(diameter="0.60")], [slot(2), slot(3)], "least_used", {}, {})
    assert picks == {}
    [warning] = rack_warnings([group(diameter="0.60")], [slot(2)], "least_used", picks, {})
    assert warning.kind == "rack-left-to-bambuddy"
    assert warning.slot_id is None
    assert warning.message == "Rack pick left to Bambuddy: no eligible position for group 0."


def test_an_unparsable_group_diameter_gives_no_pick_and_says_why() -> None:
    bad = group(diameter="abc")
    assert rank_rack([bad], [slot(2)], "least_used", {}, {}) == {}
    [warning] = rack_warnings([bad], [slot(2)], "least_used", {}, {})
    assert warning.message == "Rack pick left to Bambuddy: group 0: nozzle size unreadable."


def test_two_groups_never_share_a_position_and_the_constrained_one_goes_first() -> None:
    # Group 1 (High Flow) can use only position 2, whose code is missing; group 0
    # (Standard) can use 2 or 3. By group id alone, group 0 would take 2.
    rack = [slot(2, ""), slot(3, "HS01")]
    picks = rank_rack([group(0), group(1, volume="High Flow")], rack, "least_used", {}, {})
    assert positions(picks) == {0: 3, 1: 2}


def test_the_dynamic_recount_beats_a_static_sort() -> None:
    """After group 1 takes position 4, group 0's count drops to tie group 2's, and group
    0 goes first by id. A static sort on the initial counts (2: 3, 0: 4) would have run
    group 2 first and given it the red hotend at position 1."""
    rack = [slot(1, color="FF0000FF"), slot(2), slot(3), slot(4, "HH01")]
    groups = [
        group(0, volume="", color="#FF0000"),  # any flow: {1, 2, 3, 4}
        group(1, volume="High Flow"),  # {4}
        group(2, color="#FF0000"),  # Standard: {1, 2, 3}
    ]
    assert positions(rank_rack(groups, rack, "least_used", {}, {})) == {1: 4, 0: 1, 2: 2}


def test_three_groups_over_two_positions_starve_the_last_deterministically() -> None:
    """PERMANENT regression test of the allocation order (spec §3, §9; #1012). The
    heuristic is most-constrained-first, not an optimal assignment; if a change to the
    order changes these picks, change this test deliberately, never to make it pass."""
    rack = [slot(2), slot(4)]
    groups = [group(0), group(1), group(2)]
    first = rank_rack(groups, rack, "least_used", {}, {})
    assert positions(first) == {0: 2, 1: 4}
    assert positions(rank_rack(groups, rack, "least_used", {}, {})) == positions(first)
    [warning] = rack_warnings(groups, rack, "least_used", first, {})
    assert warning.message == "Rack pick left to Bambuddy: no eligible position for group 2."


# --- manual picks -----------------------------------------------------------------


def test_a_manual_pick_is_reserved_before_the_ranking() -> None:
    rack = [slot(2, color="FF6A13FF"), slot(3)]
    groups = [group(0, color="#FF6A13"), group(1, color="#FF6A13")]
    picks = rank_rack(groups, rack, "least_used", {}, {1: 2})
    assert positions(picks) == {0: 3, 1: 2}
    assert picks[1].manual and picks[1].reason == "chosen by hand"


def test_a_manual_pick_that_does_not_fit_the_sliced_group_is_ranked_instead() -> None:
    """#1016: the manual entry is re-checked against the sliced group, like any pick."""
    rack = [slot(2), slot(3, "HH01")]
    picks = rank_rack([group(0)], rack, "least_used", {}, {0: 3})
    assert positions(picks) == {0: 2} and not picks[0].manual
    [warning] = rack_warnings([group(0)], rack, "least_used", picks, {0: 3})
    assert warning.kind == "rack-manual-partial"
    assert warning.message == (
        "Rack position 3 does not fit group 0 (0.4 mm Standard), so ScadBuddy chose "
        "position 2 instead."
    )


def test_a_manual_non_hardened_pick_for_an_abrasive_group_is_sent_and_warned() -> None:
    cf = group(abrasive=True, materials=("PLA-CF",))
    picks = rank_rack([cf], [slot(2), slot(3)], "least_used", {}, {0: 3})
    assert positions(picks) == {0: 3} and picks[0].unsafe_material
    [warning] = rack_warnings([cf], [slot(2), slot(3)], "least_used", picks, {0: 3})
    assert warning.kind == "rack-unsafe-material"
    assert warning.message == (
        "Position 3, chosen by hand, is not known to be hardened, and PLA-CF is abrasive."
    )


def test_a_manual_pick_under_let_bambuddy_pick_is_still_sent() -> None:
    assert positions(rank_rack([group()], [slot(2), slot(3)], "bambuddy", {}, {0: 3})) == {0: 3}


def test_with_two_groups_the_manual_pick_goes_to_the_lower_id() -> None:
    manual, notes = manual_for([group(3), group(1)], 4, "least_used")
    assert manual == {1: 4}
    [note] = notes
    assert note.kind == "rack-manual-partial"
    assert note.message == (
        "The slice put the rack side in 2 nozzle groups: group 1 got position 4, and "
        "ScadBuddy ranked group 3."
    )


def test_a_manual_pick_on_a_plate_with_no_rack_group_is_unused() -> None:
    manual, notes = manual_for([], 4, "least_used")
    assert manual == {}
    assert [(n.kind, n.message) for n in notes] == [
        (
            "rack-left-to-bambuddy",
            "Rack pick left to Bambuddy: manual rack pick unused: this plate does not "
            "print from the rack.",
        )
    ]


def test_no_manual_pick_means_nothing_to_say() -> None:
    assert manual_for([group()], None, "least_used") == ({}, [])


# --- grouping and abrasive --------------------------------------------------------


def _spool(material: str, subtype: str | None = None) -> SpoolOption:
    return SpoolOption(spool_id=1, material=material, subtype=subtype)


def test_two_filaments_sharing_a_group_id_are_one_group() -> None:
    groups = rack_groups(
        [requirement(1, color="#FF6A13"), requirement(2, color="#00B1B7")],
        {1: _spool("PLA"), 2: _spool("PLA")},
    )
    assert [(g.group_id, g.color) for g in groups] == [(0, "#FF6A13")]


def test_a_group_of_pla_and_pla_cf_is_abrasive() -> None:
    [found] = rack_groups(
        [requirement(1), requirement(2, filament_type="PLA-CF")],
        {1: _spool("PLA"), 2: _spool("PLA")},
    )
    assert found.abrasive and found.materials == ("PLA", "PLA-CF")


def test_an_unused_cf_filament_does_not_make_its_group_abrasive() -> None:
    [found] = rack_groups(
        [requirement(1), requirement(2, filament_type="PLA-CF", used=False)],
        {1: _spool("PLA"), 2: _spool("PLA")},
    )
    assert not found.abrasive


@pytest.mark.parametrize("subtype", ["Glow", "glow", "GLOW"])
def test_a_pla_group_on_a_glow_spool_is_abrasive(subtype: str) -> None:
    [found] = rack_groups([requirement(1)], {1: _spool("PLA", subtype)})
    assert found.abrasive and not found.glow_unchecked


def test_a_filament_with_no_spool_is_judged_by_type_and_says_glow_was_unchecked() -> None:
    [found] = rack_groups([requirement(1)], {})
    assert not found.abrasive and found.glow_unchecked


def test_off_rack_and_ungrouped_filaments_are_not_rack_groups() -> None:
    assert rack_groups([requirement(1, on_rack=False), requirement(2, group_id=None)], {}) == []


def test_no_candidate_carries_a_serial() -> None:
    picks = rank_rack([group()], [slot(2), slot(3)], "least_used", {}, {})
    assert serial(17) not in repr(picks)
    assert all(not hasattr(c, "serial") for c in picks[0].candidates)
