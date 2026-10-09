"""The shared validator vectors (spec 2026-10-01 §6.6, §8), as the agent's suite runs them.

Every vector runs here, ``durable_only`` ones included: being resolved is a state only
the durable entry has.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scadbuddy_durable.gate import names
from scadbuddy_durable.gate.ids import durable_request_id, parse_durable_request_id
from scadbuddy_durable.gate.validate import (
    ANSWER_MAX,
    RESPONSE_MAX,
    GateEntry,
    Principal,
    Question,
    RespondRefused,
    RespondRequest,
    validate_respond,
)

DOC: dict[str, Any] = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "agent"
        / "test"
        / "fixtures"
        / "pending-input-vectors.json"
    ).read_text()
)


def _expand(value: Any) -> Any:
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        if isinstance(value.get("$repeat"), str):
            times = value["times"] if isinstance(value["times"], int) else DOC[value["times"]]
            return value["$repeat"] * (times + 1)
        return {k: _expand(v) for k, v in value.items()}
    return value


def _who(v: dict[str, str] | None) -> Principal | None:
    return None if v is None else Principal(kind=v["kind"], id=v["id"])


def _entry(e: dict[str, Any] | None) -> GateEntry | None:
    if e is None:
        return None
    return GateEntry(
        id=e["id"],
        kind=e["kind"],
        state=e["state"],
        input_hash=e["input_hash"],
        requested_by=_who(e["requested_by"]),
        session_owner=_who(e["session_owner"]),
        session_creator=_who(e["session_creator"]),
        questions=(
            tuple(Question(q["question"], q["multi_select"]) for q in e["questions"])
            if "questions" in e
            else None
        ),
        options=tuple(e["options"]) if "options" in e else None,
    )


def test_the_file_carries_this_ports_bounds_and_names() -> None:
    assert DOC["response_max"] == RESPONSE_MAX
    assert DOC["answer_max"] == ANSWER_MAX
    assert DOC["names"] == {
        "pending_input": names.PENDING_INPUT_QUERY,
        "respond": names.RESPOND_UPDATE,
        "cancel_input": names.CANCEL_INPUT_UPDATE,
        "interrupt": names.INTERRUPT_SIGNAL,
        "refused_type": names.GATE_REFUSED,
    }


@pytest.mark.parametrize("vector", DOC["vectors"], ids=[v["name"] for v in DOC["vectors"]])
def test_vector(vector: dict[str, Any]) -> None:
    r = vector["request"]
    who = _who(r["responder"])
    assert who is not None
    request = RespondRequest(
        request_id=r["request_id"], response=_expand(r["response"]), responder=who, role=r["role"]
    )
    expect = vector["expect"]
    if "refused" in expect:
        with pytest.raises(RespondRefused) as refused:
            validate_respond(_entry(vector["entry"]), request)
        assert refused.value.code == expect["refused"]
        return
    valid = validate_respond(_entry(vector["entry"]), request)
    if "decision" in expect:
        assert valid.decision == expect["decision"]
    if "answers" in expect:
        assert list(valid.answers) == expect["answers"]


def test_request_ids_round_trip_and_refuse_anything_else() -> None:
    session = "00000000-0000-4000-8000-0000000000AB"
    rid = durable_request_id(session, "run-1", "toolu:a:b")
    assert rid == "durable:00000000-0000-4000-8000-0000000000ab:run-1:toolu:a:b"
    assert parse_durable_request_id(rid) == (session.lower(), "run-1", "toolu:a:b")
    for bad in (
        "",
        "approval:x",
        "durable:not-a-uuid:r:t",
        f"durable:{session}:r",
        f"durable:{session}::t",
    ):
        assert parse_durable_request_id(bad) is None
