"""#7471: the answerer rule's form."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from scadbuddy.flows.answerers import AnswererRule

RULE = TypeAdapter(AnswererRule)


@pytest.mark.parametrize(
    "value", ["starter", "sessions", "token:6f1c3a52-6a43-4d2b-9a3e-0d2f4f0b8c11"]
)
def test_a_known_rule_is_kept(value: str) -> None:
    assert RULE.validate_python(value) == value


@pytest.mark.parametrize("value", ["", "everyone", "token:", "token:x y", "grant:1", "Starter"])
def test_an_unknown_rule_is_refused(value: str) -> None:
    with pytest.raises(ValidationError):
        RULE.validate_python(value)
