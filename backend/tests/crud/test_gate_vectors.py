"""The gate's validator and response hash on the API (#5299 step 10,
`agent_data/gate_validate.py`, `agent_data/gate_canonical.py`): copies of agent-durable's,
which `.github/scripts/lint-codec-copies.sh` keeps byte-identical, run against the shared
vectors the agent's `src/gate/validate.ts` and `canonical.ts` wrote (copies in
`tests/fixtures/`, kept equal by the same script)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.agent_data.gate import gate_entry
from scadbuddy.agent_data.gate_canonical import canonical_json, response_hash
from scadbuddy.agent_data.gate_validate import (
    ANSWER_MAX,
    RESPONSE_MAX,
    GateEntry,
    Principal,
    Question,
    RespondRefused,
    RespondRequest,
    validate_respond,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
DOC: dict[str, Any] = json.loads((FIXTURES / "pending-input-vectors.json").read_text())
HASHES: dict[str, Any] = json.loads((FIXTURES / "response-hash-vectors.json").read_text())


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
        in_run=bool(e.get("in_run", False)),
    )


def test_the_file_carries_this_copys_bounds() -> None:
    assert DOC["response_max"] == RESPONSE_MAX
    assert DOC["answer_max"] == ANSWER_MAX


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


@pytest.mark.parametrize("vector", HASHES["vectors"], ids=lambda v: v["name"])
def test_response_hash_vector(vector: dict[str, Any]) -> None:
    assert canonical_json(vector["value"]) == vector["canonical"]
    assert response_hash(vector["value"]) == vector["sha256"]


def test_a_rows_record_reads_as_the_agents_gate_record() -> None:
    """``ai_pending_input.gate`` holds the agent's `GateRecord` (camelCase), the entry's state
    the row's: ``resolving`` once past its timer."""
    row = {"request_id": "durable:x:r:t", "kind": "answer", "expired": True}
    record = {
        "id": "ignored",
        "kind": "approval",
        "inputHash": None,
        "requestedBy": {"kind": "bearer", "id": "a", "label": "A"},
        "sessionOwner": {"kind": "browser", "id": "browser"},
        "sessionCreator": None,
        "questions": [{"question": "Q?", "multiSelect": True, "header": "h"}],
        "inRun": True,
    }
    assert gate_entry(row, record) == GateEntry(
        id="durable:x:r:t",
        kind="answer",
        state="resolving",
        input_hash=None,
        requested_by=Principal("bearer", "a"),
        session_owner=Principal("browser", "browser"),
        session_creator=None,
        questions=(Question("Q?", True),),
        options=None,
        in_run=True,
    )
