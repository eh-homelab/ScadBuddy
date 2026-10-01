"""Template inputs (spec 2026-09-27 §4.3, §10)."""

from __future__ import annotations

from collections.abc import Callable
from typing import assert_type

import pytest

from scadbuddy.api.jobs import _job_status
from scadbuddy.render.inputs import MAX_INPUTS_BYTES, InputsError, legacy_inputs, normalize_inputs
from scadbuddy.render.job_models import Job, now
from scadbuddy.render.schema import ParamValue


def test_bare_params_are_read_as_version_zero_inputs() -> None:
    assert normalize_inputs(None, {"width": 12}).data == {"params": {"width": 12}, "v": 0}


def test_nothing_at_all_is_empty_params() -> None:
    assert normalize_inputs(None, None).data == {"params": {}, "v": 0}


def test_ui_keys_are_kept_beside_params() -> None:
    raw = {"params": {"width": 12}, "house": {"storeys": 2}, "v": 3}
    assert normalize_inputs(raw, None).data == raw


def test_inputs_without_params_get_empty_params() -> None:
    assert normalize_inputs({"tab": "lid"}, None).data == {"tab": "lid", "params": {}, "v": 0}


def test_params_that_agree_with_inputs_are_accepted() -> None:
    assert normalize_inputs({"params": {"width": 1}}, {"width": 1}).params == {"width": 1}


def test_params_that_disagree_with_inputs_are_refused() -> None:
    with pytest.raises(InputsError, match="disagree"):
        normalize_inputs({"params": {"width": 1}}, {"width": 2})


def test_params_that_differ_only_in_type_disagree() -> None:
    with pytest.raises(InputsError, match="disagree"):
        normalize_inputs({"params": {"flag": True}}, {"flag": 1})


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize(
    "shape",
    [
        lambda n: {"params": {"width": n}},
        lambda n: {"params": {}, "ui": {"zoom": [1, n]}},
    ],
    ids=["param", "nested"],
)
def test_non_finite_numbers_are_refused(
    number: float, shape: Callable[[float], dict[str, object]]
) -> None:
    with pytest.raises(InputsError, match="no NaN or Infinity"):
        normalize_inputs(shape(number), None)


def test_a_job_from_before_inputs_reports_version_zero_inputs() -> None:
    job = Job(id="j", slug="s", params={"width": 3}, created_at=now())
    assert _job_status(job, None).inputs == {"params": {"width": 3}, "v": 0}


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"params": [1, 2]}, "inputs.params must be an object"),
        ({"params": {"width": [1]}}, "inputs.params.width must be a number, string or boolean"),
        ({"params": {"width": None}}, "inputs.params.width must be a number, string or boolean"),
        ({"params": {}, "v": -1}, "inputs.v must be a non-negative integer"),
        ({"params": {}, "v": True}, "inputs.v must be a non-negative integer"),
    ],
)
def test_malformed_inputs_are_refused(raw: dict[str, object], message: str) -> None:
    with pytest.raises(InputsError, match=message):
        normalize_inputs(raw, None)


def test_oversized_inputs_are_refused() -> None:
    with pytest.raises(InputsError, match=f"at most {MAX_INPUTS_BYTES}"):
        normalize_inputs({"params": {}, "blob": "x" * MAX_INPUTS_BYTES}, None)


def test_legacy_inputs() -> None:
    assert legacy_inputs({"width": 3}) == {"params": {"width": 3}, "v": 0}


@pytest.mark.parametrize("value", [float("inf"), float("nan")])
def test_bare_params_that_are_not_json_are_refused(value: float) -> None:
    with pytest.raises(InputsError, match="no NaN or Infinity"):
        normalize_inputs(None, {"width": value})


def test_bare_params_past_the_size_cap_are_refused() -> None:
    with pytest.raises(InputsError, match=f"at most {MAX_INPUTS_BYTES}"):
        normalize_inputs(None, {"label": "x" * MAX_INPUTS_BYTES})


def test_normalised_params_keep_their_type() -> None:
    """#706 gate: the route hands `params` to checks typed `Mapping[str, ParamValue]`;
    mypy checks this test, so `Any` here fails the type check."""
    normalized = normalize_inputs({"params": {"width": 1}, "ui": {}}, None)
    assert_type(normalized.params, dict[str, ParamValue])
    assert normalized.data["params"] is normalized.params
