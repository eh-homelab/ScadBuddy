"""Each bundled analyzer against fixtures that do and do not trip it (#284)."""

from __future__ import annotations

import pytest

from scadbuddy.analyzers import builtin
from scadbuddy.analyzers.builtin import (
    ELIGIBILITY,
    LOW_FILAMENT,
    NON_MANIFOLD,
    OPEN_EDGES,
    OVERHANGS,
    PLATE_FIT,
    SILK_GLOSS,
    SILK_NOZZLE,
    SILK_PLATE,
)
from scadbuddy.analyzers.context import AnalysisRequest
from scadbuddy.analyzers.model import Analyzer, AnalyzerDiagnostic
from scadbuddy.analyzers.runner import run_checks
from scadbuddy.bambuddy.filaments import FilamentOptions, FilamentPlan, SlotNeed, SpoolOption
from scadbuddy.bambuddy.models import EligibilityReport, SlotChoice
from tests.analyzers.conftest import (
    basic_slot,
    context,
    cube,
    geometry_of,
    open_box,
    output,
    pipeline,
    silk_slot,
    tee,
    touching_cubes,
)


def _run(analyzer: Analyzer, **fields: object) -> list[AnalyzerDiagnostic]:
    diagnostics, skipped = run_checks(context(**fields), [analyzer])
    assert not skipped, skipped
    return diagnostics


# --- SB1001 / SB1002 ----------------------------------------------------------------


def test_a_closed_cube_has_no_mesh_diagnostics() -> None:
    geometry = geometry_of(cube())
    assert _run(NON_MANIFOLD, geometry=geometry) == []
    assert _run(OPEN_EDGES, geometry=geometry) == []


def test_edges_shared_by_four_faces_are_a_non_manifold_error_located_on_the_part() -> None:
    [found] = _run(NON_MANIFOLD, geometry=geometry_of(touching_cubes()))
    assert found.id == "SB1001"
    assert found.key == "SB1001:part-1"
    assert found.severity == "error"
    assert found.location is not None and found.location.part == 1
    assert found.location.edges and all(e.kind == "non_manifold" for e in found.location.edges)
    assert any(e.label == "non-manifold edges" and e.value for e in found.evidence)
    assert found.sources[0].quote.startswith("Non\u2011manifold edges are reported as Error")


def test_a_missing_face_is_open_edges_reported_as_info() -> None:
    [found] = _run(OPEN_EDGES, geometry=geometry_of(open_box()))
    assert found.severity == "info"
    assert "4 open edges" in found.message


def test_a_split_part_is_not_edge_checked_so_it_raises_nothing() -> None:
    geometry = geometry_of(open_box())
    geometry.parts[0].edges_checked = False
    assert _run(OPEN_EDGES, geometry=geometry) == []


# --- SB1003 -------------------------------------------------------------------------


def test_a_flat_ceiling_is_past_the_default_support_threshold_with_a_fix() -> None:
    [found] = _run(OVERHANGS, geometry=geometry_of(tee()))
    assert found.severity == "info"
    # The bar's whole underside, 30 x 10 mm: the stem is a separate closed box that
    # only touches it, so the face over the stem still points down.
    area = next(e for e in found.evidence if e.unit == "mm\u00b2")
    assert area.value == pytest.approx(300.0)
    [fix] = found.fixes
    [line] = fix.changes
    assert (line.target, line.setting, line.proposed) == (
        "derived_process_preset",
        "enable_support",
        "1",
    )
    assert line.base_known is False and not line.verified and line.to_verify
    assert {s.quote for s in found.sources} >= {'"support_threshold_angle": "30",'}


def test_a_cube_has_no_overhang() -> None:
    assert _run(OVERHANGS, geometry=geometry_of(cube())) == []


# --- SB2001-SB2003 ------------------------------------------------------------------


def test_silk_by_preset_name_proposes_the_guides_values_per_slot() -> None:
    [found] = _run(SILK_GLOSS, filaments=[silk_slot(2), basic_slot(1)])
    assert found.id == "SB2001" and found.severity == "info"
    [fix] = found.fixes
    lines = {(c.target, c.setting, c.slot_id): c.proposed for c in fix.changes}
    assert lines == {
        ("derived_process_preset", "outer_wall_speed", None): 50,
        ("filament_overrides", "nozzle_temperature", 2): 235,
        ("filament_overrides", "nozzle_temperature_initial_layer", 2): 235,
    }
    # Every value is covered by a source that says it supports that setting.
    for line in fix.changes:
        assert any(line.setting in source.supports for source in line.sources)
    assert len(fix.blockers) == 2


def test_silk_by_subtype_counts_and_a_basic_pla_does_not() -> None:
    silk = basic_slot().model_copy(update={"subtype": "Silk+", "preset_name": None})
    assert _run(SILK_GLOSS, filaments=[silk])
    assert _run(SILK_GLOSS, filaments=[basic_slot()]) == []


def test_a_pipeline_preset_named_silk_counts_without_a_spool() -> None:
    slot = silk_slot().model_copy(
        update={"material": None, "subtype": None, "spool_id": None, "origin": "pipeline"}
    )
    slot = slot.model_copy(update={"preset_name": "Bambu PLA Silk @BBL H2C"})
    assert _run(SILK_GLOSS, filaments=[slot])


def test_silk_petg_is_not_silk_pla() -> None:
    slot = silk_slot().model_copy(update={"material": "PETG"})
    assert _run(SILK_GLOSS, filaments=[slot]) == []


@pytest.mark.parametrize(("nozzle", "hits"), [("0.4", 0), ("0.2", 0), ("0.6", 1), ("0.8", 1)])
def test_silk_on_a_large_nozzle_is_a_warning(nozzle: str, hits: int) -> None:
    found = _run(SILK_NOZZLE, filaments=[silk_slot()], pipeline=pipeline(nozzle_diameter=nozzle))
    assert len(found) == hits
    assert all(row.severity == "warning" for row in found)


def test_silk_on_supertack_from_the_request_or_the_pipeline() -> None:
    assert _run(
        SILK_PLATE,
        filaments=[silk_slot()],
        request=AnalysisRequest(bed_type="Supertack Plate"),
    )
    assert _run(SILK_PLATE, filaments=[silk_slot()], pipeline=pipeline(bed_type="Supertack Plate"))
    assert _run(SILK_PLATE, filaments=[silk_slot()], pipeline=pipeline()) == []


# --- SB3002 -------------------------------------------------------------------------


def _options(remaining: float | None, used: float | None) -> FilamentOptions:
    return FilamentOptions(
        library_file_id=41,
        printer_id=1,
        slots=[SlotNeed(slot_id=1, material="PLA", used_grams=used)],
        spools=[SpoolOption(spool_id=2, material="PLA", remaining_g=remaining)],
    )


PLAN = FilamentPlan(slots=[SlotChoice(slot_id=1, spool_id=2)])


def test_less_left_than_the_copies_need_is_a_warning_with_the_figures() -> None:
    [found] = _run(
        LOW_FILAMENT,
        filament_options=_options(remaining=30, used=20),
        request=AnalysisRequest(filament_plan=PLAN, copies=2),
    )
    assert found.key == "SB3002:slot-1"
    assert found.severity == "warning"
    figures = {e.label: e.value for e in found.evidence}
    assert figures == {"remaining": 30, "needed per copy": 20, "copies": 2}


def test_unknown_grams_decline_to_judge() -> None:
    assert (
        _run(
            LOW_FILAMENT,
            filament_options=_options(remaining=1, used=None),
            request=AnalysisRequest(filament_plan=PLAN),
        )
        == []
    )


# --- SB4001 -------------------------------------------------------------------------


def test_a_model_taller_than_the_printer_is_a_plate_error() -> None:
    [found] = _run(PLATE_FIT, output=output(size=(10, 10, 400)))
    assert found.severity == "error"
    assert "Z 400.0 mm > 325.0 mm" in found.message


def test_a_small_model_fits() -> None:
    assert _run(PLATE_FIT, output=output()) == []


def test_room_for_the_prime_tower_is_checked_for_multi_colour() -> None:
    size = (300, 300, 10)
    assert _run(PLATE_FIT, output=output(size=size, colours=["#FF0000"])) == []
    [found] = _run(PLATE_FIT, output=output(size=size, colours=["#FF0000", "#00FF00"]))
    assert any(e.label == "placement" for e in found.evidence)


# --- SB5001 -------------------------------------------------------------------------


def test_each_eligibility_issue_is_reported_verbatim() -> None:
    report = EligibilityReport.model_validate(
        {
            "ok": False,
            "issues": [
                {
                    "kind": "filament_type_mismatch",
                    "slot_index": 1,
                    "expected": "PLA",
                    "actual": "PETG",
                },
                {"kind": "printer_offline"},
            ],
        }
    )
    found = _run(ELIGIBILITY, eligibility=report, pipeline=pipeline())
    assert [row.severity for row in found] == ["error", "error"]
    assert "filament_type_mismatch for slot 1 (expected 'PLA', actual 'PETG')" in found[0].message


def test_a_class_where_one_printer_fails_is_a_warning() -> None:
    report = EligibilityReport.model_validate(
        {
            "ok": True,
            "target_kind": "printer_class",
            "printer_reports": [
                {
                    "printer_id": 2,
                    "printer_name": "B",
                    "ok": False,
                    "issues": [{"kind": "printer_offline"}],
                }
            ],
        }
    )
    [found] = _run(ELIGIBILITY, eligibility=report, pipeline=pipeline())
    assert found.severity == "warning" and "on B" in found.message


# --- the runner ---------------------------------------------------------------------


def test_an_analyzer_missing_an_input_is_skipped_saying_why() -> None:
    ctx = context()
    ctx.unavailable["geometry"] = "this configuration has not been rendered yet"
    diagnostics, skipped = run_checks(ctx, [OVERHANGS])
    assert diagnostics == []
    [row] = skipped
    assert row.id == "SB1003"
    assert row.missing[0].reason == "this configuration has not been rendered yet"


def test_a_crashing_analyzer_is_itself_a_diagnostic() -> None:
    def boom(ctx: object, analyzer: Analyzer) -> list[AnalyzerDiagnostic]:
        raise RuntimeError("bad")

    broken = Analyzer(
        id="SB9999",
        name="broken",
        title="Broken",
        severity="info",
        category="geometry",
        description="",
        sources=OVERHANGS.sources,
        check=boom,
    )
    diagnostics, _ = run_checks(context(), [broken])
    [found] = diagnostics
    assert (found.id, found.key, found.severity) == ("SB0001", "SB0001:SB9999", "warning")
    assert "RuntimeError" in found.message


def test_every_bundled_analyzer_has_a_unique_id() -> None:
    ids = [analyzer.id for analyzer in builtin.BUILTIN]
    assert len(ids) == len(set(ids))
