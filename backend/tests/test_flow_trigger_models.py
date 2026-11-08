"""An event trigger's subject and `match` (#4252, orders spec §7.1)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from scadbuddy.flow_triggers.models import EventTiming, Subject, matches


def test_a_slug_reads_as_a_template_subject() -> None:
    timing = EventTiming.model_validate({"kind": "model.changed", "slug": "box"})
    assert timing.subject == Subject(kind="template", id="box")
    assert "slug" not in timing.model_dump()


def test_a_stored_slug_of_none_reads_as_no_subject() -> None:
    timing = EventTiming.model_validate({"kind": "render.finished", "slug": None})
    assert timing.subject is None
    # A null slug names nothing, so it never clashes with a subject.
    both = EventTiming.model_validate(
        {"kind": "order.changed", "slug": None, "subject": {"kind": "order", "id": "o1"}}
    )
    assert both.subject == Subject(kind="order", id="o1")


@pytest.mark.parametrize(
    "event",
    [
        {"kind": "order.changed", "subject": {"kind": "template", "id": "box"}},
        {"kind": "order.changed", "subject": {"kind": "order_item", "id": "i1"}},
        {"kind": "order.changed", "slug": "box"},
        {"kind": "render.finished", "subject": {"kind": "order", "id": "o1"}},
        {"kind": "model.changed", "subject": {"kind": "template", "id": "Not A Slug"}},
        {"kind": "order.changed", "subject": {"kind": "order", "id": "a/b"}},
        {"kind": "model.changed", "slug": "box", "subject": {"kind": "template", "id": "box"}},
    ],
)
def test_a_subject_the_kind_has_no_such_thing_of_is_refused(event: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        EventTiming.model_validate(event)


def test_an_item_event_takes_its_order_or_the_item_as_its_subject() -> None:
    for kind in ("order", "order_item"):
        timing = EventTiming.model_validate(
            {"kind": "order_item.changed", "subject": {"kind": kind, "id": "x1"}}
        )
        assert timing.subject == Subject(kind=kind, id="x1")


@pytest.mark.parametrize(
    "event",
    [
        {"kind": "order.changed", "match": {"colour": ["red"]}},
        {"kind": "order.changed", "match": {"status": ["acepted"]}},
        {"kind": "order.changed", "match": {"status": ["printed"]}},
        {"kind": "order.changed", "match": {"status": []}},
        {"kind": "order.changed", "match": {}},
        {"kind": "render.finished", "match": {"status": ["done"]}},
        {"kind": "order_item.changed", "match": {"status": ["accepted"]}},
    ],
)
def test_a_match_on_a_field_or_value_the_event_cannot_have_is_refused(
    event: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        EventTiming.model_validate(event)


def test_a_match_admits_only_the_values_it_lists() -> None:
    timing = EventTiming.model_validate(
        {"kind": "order_item.changed", "match": {"status": ["printed", "failed"]}}
    )
    assert matches(timing, {"status": "printed"})
    assert matches(timing, {"status": "failed"})
    assert not matches(timing, {"status": "queued"})
    assert not matches(timing, {})


def test_no_match_admits_every_event_of_the_kind() -> None:
    timing = EventTiming.model_validate({"kind": "order.changed"})
    assert matches(timing, {"status": "draft"})
