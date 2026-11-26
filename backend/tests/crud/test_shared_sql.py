"""The SQL that Python and Go's ``scadbuddy-core`` both run (#7667 §5.1, #6949 K1): each
statement is one file, and each language compares the text it runs with it (Go:
``tools/go/core/internal/inventory/store`` ``TestSharedStatementsMatch``). Go's actor, which
Python's history reads with the strict ``Actor`` model, is checked here against that model."""

from __future__ import annotations

import json
import typing
from pathlib import Path

import pytest

from scadbuddy.crud import idempotency
from scadbuddy.entities import registry
from scadbuddy.orders.models import Actor

PACKAGE = Path(registry.__file__).resolve().parents[1]
GO_ACTORS = PACKAGE.parents[1] / "tools/go/core/internal/inventory/store/testdata/actors.json"


def _normal(text: str) -> str:
    return " ".join(text.split())


@pytest.mark.parametrize(
    ("path", "ours"),
    [
        ("entities/shared/ensure_entity.sql", registry.ENSURE_ENTITY_SQL),
        ("crud/shared/crud_requests_select.sql", idempotency.SELECT_SQL),
        ("crud/shared/crud_requests_insert.sql", idempotency.INSERT_SQL),
    ],
)
def test_shared_statements_match(path: str, ours: str) -> None:
    assert _normal(ours) == _normal((PACKAGE / path).read_text())


def test_go_writes_the_actor_python_reads() -> None:
    facts = json.loads(GO_ACTORS.read_text())
    assert facts["fields"] == list(Actor.model_fields)
    assert facts["kinds"] == list(typing.get_args(Actor.model_fields["kind"].annotation))
    for written in facts["accepted"].values():
        assert Actor.model_validate_json(written).model_dump_json() == written
