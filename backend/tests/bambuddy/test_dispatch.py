"""Issue #87 — which printer a slice-and-queue run is aimed at.

Only :func:`target_of` is unit-tested here; the rest of ``dispatch`` is exercised
end-to-end in ``tests/api/test_print_filaments.py`` against recorded bodies. The
printer-omitted branch is the one worth its own test: it is the only path that hands
Bambuddy a ``target_model`` instead of a printer, and every API-level test supplies an
explicit printer, so nothing else reaches it.
"""

from __future__ import annotations

from scadbuddy.bambuddy.dispatch import target_of
from scadbuddy.bambuddy.models import Pipeline


def pipeline(**extra: object) -> Pipeline:
    base: dict[str, object] = {
        "id": 1,
        "name": "Textured PEI",
        "target_kind": "printer_class",
        "target_model_class": "H2C",
        "target_printer_id": None,
        "printer_preset": {"source": "cloud", "id": "GM041"},
        "process_preset": {"source": "cloud", "id": "GP001"},
        "filament_presets": [{"source": "cloud", "id": "GFA00"}],
    }
    base.update(extra)
    return Pipeline.model_validate(base)


def test_an_explicit_printer_wins_over_the_pipelines_own_target() -> None:
    assert target_of(pipeline(), 7) == (7, None)


def test_a_class_targeted_pipeline_falls_back_to_the_class() -> None:
    """Exactly one of the two is sent, and it is the class — so Bambuddy's scheduler
    picks the machine, matching on the overrides it was given."""
    assert target_of(pipeline(), None) == (None, "H2C")


def test_a_printer_targeted_pipeline_falls_back_to_that_printer() -> None:
    aimed = pipeline(target_kind="specific_printer", target_printer_id=3, target_model_class=None)
    assert target_of(aimed, None) == (3, None)


def test_a_printer_targeted_pipeline_with_no_printer_falls_through_to_its_class() -> None:
    """``target_kind`` is not the whole answer: Bambuddy leaves ``target_printer_id``
    null on a pipeline whose printer has been removed, and aiming at the class is the
    behaviour that still schedules. Naming neither is what an empty pipeline gets."""
    stale = pipeline(target_kind="specific_printer", target_printer_id=None)
    assert target_of(stale, None) == (None, "H2C")

    empty = pipeline(
        target_kind="specific_printer", target_printer_id=None, target_model_class=None
    )
    assert target_of(empty, None) == (None, None)
