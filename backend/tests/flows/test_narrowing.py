"""What counts as a narrowing of a parked call (#2482): the same call, made smaller."""

from typing import Any

import pytest

from scadbuddy.flows.narrowing import narrowing_problem

SOURCE = {"output_id": "o1"}
REQUEST = {"filament_plan": {"slots": []}, "choices": {"quality": "std"}, "copies": 4}
ARRANGE = {
    "objects": [{"output_id": "a", "count": 3}, {"output_id": "b"}, {"output_id": "c", "count": 2}],
    "goal": "fewest_plates",
}


def print_call(**request: Any) -> dict[str, Any]:
    return {"source": SOURCE, "request": {**REQUEST, **request}}


@pytest.mark.parametrize(
    "proposed",
    [
        print_call(),
        print_call(copies=1),
        print_call(copies=4, plate_id=None),
    ],
)
def test_a_print_with_fewer_copies_is_a_narrowing(proposed: dict[str, Any]) -> None:
    assert narrowing_problem("queue_print", print_call(), proposed) is None


def test_all_plates_may_only_be_turned_off() -> None:
    original = print_call(all_plates=True)
    assert narrowing_problem("queue_print", original, print_call(all_plates=False)) is None
    assert narrowing_problem("queue_print", print_call(), original) is not None


def test_chosen_plates_may_only_be_fewer() -> None:
    """#3652: some of the call's plates, never others, none or a list it had not."""
    original = print_call(plate_ids=[1, 3, 4])
    assert narrowing_problem("queue_print", original, print_call(plate_ids=[3])) is None
    assert narrowing_problem("queue_print", original, print_call(plate_ids=[2])) is not None
    assert narrowing_problem("queue_print", original, print_call(plate_ids=[])) is not None
    assert narrowing_problem("queue_print", print_call(), print_call(plate_ids=[1])) is not None


@pytest.mark.parametrize(
    ("original", "proposed"),
    [
        (print_call(), print_call(copies=5)),
        (print_call(), print_call(copies=0)),
        (print_call(), print_call(copies=True)),
        (print_call(), print_call(copies=None)),
        (print_call(), print_call(printer_id=3)),
        (print_call(printer_id=4), print_call(printer_id=3)),
        (print_call(), print_call(choices={"quality": "fine"})),
        (print_call(), print_call(plate_id=2)),
        (print_call(), {"source": {"output_id": "o2"}, "request": {**REQUEST}}),
        ({"source": SOURCE, "request": {"choices": {}}}, print_call(copies=1)),
    ],
)
def test_a_print_that_widens_or_substitutes_is_not(
    original: dict[str, Any], proposed: dict[str, Any]
) -> None:
    assert narrowing_problem("queue_print", original, proposed) is not None


@pytest.mark.parametrize(
    "objects",
    [
        [{"output_id": "a", "count": 1}],
        [{"output_id": "b"}, {"output_id": "c", "count": 2}],
        [{"output_id": "a", "count": 3}, {"output_id": "c", "count": 0}],
    ],
)
def test_an_arrange_of_fewer_objects_is_a_narrowing(objects: list[Any]) -> None:
    proposed = {"request": {**ARRANGE, "objects": objects}}
    assert narrowing_problem("arrange", {"request": ARRANGE}, proposed) is None


@pytest.mark.parametrize(
    "request_",
    [
        {**ARRANGE, "objects": [{"output_id": "a", "count": 4}]},
        {**ARRANGE, "objects": [{"output_id": "b", "count": 1}]},
        {**ARRANGE, "objects": [{"output_id": "a"}]},
        {**ARRANGE, "objects": [{"output_id": "z"}]},
        {**ARRANGE, "objects": [{"output_id": "c", "count": 2}, {"output_id": "a", "count": 3}]},
        {**ARRANGE, "objects": []},
        {**ARRANGE, "goal": "keep_together"},
        {**ARRANGE, "printer_id": 2},
    ],
)
def test_an_arrange_that_widens_or_substitutes_is_not(request_: dict[str, Any]) -> None:
    assert narrowing_problem("arrange", {"request": ARRANGE}, {"request": request_}) is not None


def test_a_tool_call_is_never_narrowed() -> None:
    args = {"output_id": "o1"}
    assert narrowing_problem("tool", args, args) is not None
