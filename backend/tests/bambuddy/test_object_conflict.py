"""#6732: Bambu Studio's "Object conflicts were detected" refusal of a plate, read as one
structured ``object-conflict`` warning that names the plate (#3770 P4).

The texts are the CLI's ``cli_errors`` for ``CLI_OBJECT_COLLISION_IN_LAYER_PRINT`` (-64)
and ``CLI_OBJECT_COLLISION_IN_SEQ_PRINT`` (-63), ``src/BambuStudio.cpp`` at v02.08.02.61
(https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/BambuStudio.cpp#L163-L164).
"""

from __future__ import annotations

from scadbuddy.bambuddy.prime_tower import object_conflict_warning
from scadbuddy.bambuddy.runs import PrintRunError
from scadbuddy.workflows.problems import object_conflict

LAYER = (
    "Object conflicts were detected. Please verify the slicing of all plates in Bambu Studio"
    " before uploading."
)
BY_OBJECT = (
    "Object conflicts were detected when using print-by-object mode. Please verify the slicing"
    " of all plates in Bambu Studio before uploading."
)


def test_the_slicers_object_conflict_is_one_warning_for_the_plate() -> None:
    warning = object_conflict_warning(f"{LAYER} (return code -64)", 2)

    assert warning is not None
    assert (warning.kind, warning.plate_id, warning.slot_id) == ("object-conflict", 2, None)
    assert warning.message.startswith("Plate 2: ")


def test_a_by_object_conflict_is_one_too() -> None:
    warning = object_conflict_warning(f"Bambuddy failed to slice the plate: {BY_OBJECT}", 1)

    assert warning is not None and warning.kind == "object-conflict"
    assert "by object" in warning.message


def test_another_failure_is_no_conflict() -> None:
    assert object_conflict_warning("Found G-code outside of the printable area.", 1) is None
    assert object_conflict_warning(None, 1) is None
    assert object_conflict_warning("", 1) is None


def test_a_failed_slices_problem_keeps_its_words_and_carries_the_warning() -> None:
    problem = PrintRunError(
        status=502,
        title="Bad Gateway",
        detail=f"Bambuddy failed to slice the plate: {LAYER}",
        extensions={"slice_job_id": 9},
    )

    found = object_conflict(problem, 4)

    assert found is not None
    assert (found.status, found.title, found.detail) == (502, "Bad Gateway", problem.detail)
    assert found.extensions["slice_job_id"] == 9
    assert found.extensions["warnings"] == [
        object_conflict_warning(problem.detail, 4).model_dump(mode="json")  # type: ignore[union-attr]
    ]


def test_a_problem_with_no_conflict_is_left_alone() -> None:
    problem = PrintRunError(status=502, title="Bad Gateway", detail="the plate is empty")
    assert object_conflict(problem, 1) is None
