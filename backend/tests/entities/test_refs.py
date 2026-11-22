"""Entity references, `kind:ref` (#6950, spec 6.1)."""

from __future__ import annotations

import pytest

from scadbuddy.entities.refs import BadEntityRef, EntityRef


def test_parse_splits_at_the_first_colon() -> None:
    assert EntityRef.parse("spool:abc") == EntityRef("spool", "abc")
    assert EntityRef.parse("library_file:a:b").ref == "a:b"


@pytest.mark.parametrize("text", ["spool", ":x", "Spool:x", "spool:", ""])
def test_parse_refuses_a_malformed_reference(text: str) -> None:
    with pytest.raises(BadEntityRef):
        EntityRef.parse(text)


def test_str_is_kind_colon_ref() -> None:
    assert str(EntityRef("order", "o1")) == "order:o1"
