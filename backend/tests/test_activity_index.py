"""Activity (spec 2026-10-09): the record, its event and topic."""

from scadbuddy.api.realtime import topics_of, valid_topic
from scadbuddy.core.events import ActivityEvent, AgentOperationEvent, decode_event, encode_event


def test_activity_event_round_trips_and_routes() -> None:
    event = ActivityEvent(activity_id="render:abc", principal="browser")
    assert decode_event(encode_event(event)) == event
    assert topics_of(event) == ["activity", "activity:render:abc"]


def test_activity_topics_are_valid() -> None:
    assert valid_topic("activity")
    assert valid_topic("activity:render:abc123")
    assert valid_topic("activity:agent_operation:3f2c9a10-1b2c-4d5e-8f90-123456789abc")
    assert not valid_topic("activity:nope:x")
    assert not valid_topic("activity:render:")


def test_agent_operation_event_decodes_and_goes_nowhere() -> None:
    payload = (
        '{"id":"e1","at":"2026-10-09T00:00:00Z","kind":"ai_operation.changed","operation_id":"x"}'
    )
    event = decode_event(payload)
    assert isinstance(event, AgentOperationEvent)
    assert topics_of(event) == []
