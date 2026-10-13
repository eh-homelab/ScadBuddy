"""The flow approval's input hash (#2318) is the agent's `inputHash`: every vector the
agent computed (agent/test/approvalHashVectors.test.ts, copied into tests/fixtures)
comes out the same here, from the same JSON parsed by Python."""

import json
from pathlib import Path

import pytest

from scadbuddy.core.secrets import Kek
from scadbuddy.flows.approval_hash import approval_hash_key, canonical_json, input_hash

#: agent/test/fixtures/approval-hash-vectors.json
VECTORS = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "approval-hash-vectors.json").read_text()
)


def test_the_key_is_the_agents() -> None:
    key = approval_hash_key(Kek(id="k1", key=bytes.fromhex(VECTORS["kek_hex"])))
    assert key.hex() == VECTORS["hash_key_hex"]


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: c["input_json"][:40])
def test_each_vector_hashes_as_the_agent_does(case: dict[str, str]) -> None:
    key = bytes.fromhex(VECTORS["hash_key_hex"])
    parsed = json.loads(case["input_json"])
    assert canonical_json({"tool": case["tool"], "input": parsed}) == case["canonical"]
    assert input_hash(key, case["tool"], parsed) == case["hash"]


def test_another_input_hashes_differently() -> None:
    key = bytes.fromhex(VECTORS["hash_key_hex"])
    assert input_hash(key, "render", {"n": 1}) != input_hash(key, "render", {"n": 2})
    assert input_hash(key, "render", {"n": 1}) != input_hash(key, "write", {"n": 1})
