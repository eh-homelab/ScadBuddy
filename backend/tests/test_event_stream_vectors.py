"""The event stream's wire vectors (#7768): the frames the change streams
(`agent_changes/hub.py`, `api/crud_agent_changes.py`, `crud_fleet.py`, `crud_camera.py`) and
the realtime socket (`api/realtime.py`) send for each payload, and the typed events' fields.
Go core's copy of both readers (`tools/go/core/internal/bus`) runs the same file
(`tests/fixtures/event-stream-vectors.json`), so the two stay byte-compatible.

Each case's outputs are what this build computes. To write them again after a deliberate
change, run this file with ``UPDATE_EVENT_STREAM_VECTORS=1`` and
``SCADBUDDY_TEST_DATABASE_URL`` set: a case's ``stored`` is its payload as the ``events``
log's ``jsonb`` gives it back (``payload::text``), which only Postgres can say.
"""

from __future__ import annotations

import json
import os
import types
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin

import pytest
from pydantic_core import PydanticUndefined

from scadbuddy.agent_changes.hub import NOTIFY_ONLY, Change, frame
from scadbuddy.api.crud_agent_changes import keep_agent
from scadbuddy.api.crud_camera import keep_camera
from scadbuddy.api.crud_fleet import keep_fleet
from scadbuddy.api.realtime import event_frame, topics_of, valid_topic
from scadbuddy.core.events import PG_CHANNEL, Event, decode_event, encode_event

FILE = Path(__file__).resolve().parent / "fixtures" / "event-stream-vectors.json"
UPDATE = os.environ.get("UPDATE_EVENT_STREAM_VECTORS") == "1"


def _type(annotation: Any) -> dict[str, Any]:
    """A field's type as the vectors name it: str, int, bool, datetime or list[str], with its
    choices (a ``Literal``) and whether it may be null."""
    out: dict[str, Any] = {"nullable": False}
    args = get_args(annotation)
    if get_origin(annotation) in (Union, types.UnionType) and type(None) in args:
        out["nullable"] = True
        (annotation,) = [a for a in args if a is not type(None)]
    if get_origin(annotation) is Literal:
        out["type"], out["choices"] = "str", list(get_args(annotation))
    elif get_origin(annotation) is list and get_args(annotation) == (str,):
        out["type"] = "list[str]"
    else:
        out["type"] = {str: "str", int: "int", bool: "bool", datetime: "datetime"}[annotation]
    return out


def kinds() -> dict[str, list[dict[str, Any]]]:
    """Every typed kind's fields, in the order pydantic writes them."""
    table: dict[str, list[dict[str, Any]]] = {}
    for model in get_args(get_args(Event)[0]):
        for kind in get_args(model.model_fields["kind"].annotation):
            fields = []
            for name, info in model.model_fields.items():
                field = {"name": name, **_type(info.annotation)}
                if name == "kind":
                    field = {"name": name, "type": "kind", "nullable": False}
                field["required"] = (
                    info.default is PydanticUndefined
                    and info.default_factory is None
                    and name != "kind"
                )
                if info.default is not PydanticUndefined and name != "kind":
                    field["default"] = info.default
                fields.append(field)
            table[kind] = fields
    return dict(sorted(table.items()))


def notify_only() -> list[str]:
    """The typed kinds the bus sends as NOTIFY only: their change frames carry no ``id``."""
    return sorted(
        k for model in NOTIFY_ONLY for k in get_args(model.model_fields["kind"].annotation)
    )


def _decode(text: str) -> Any:
    try:
        return decode_event(text)
    except ValueError:
        return None


def outputs(case: dict[str, Any]) -> dict[str, Any]:
    """What this build sends for one case: its change frame live and in replay, which
    callers' streams keep it, and its realtime topics and frame."""
    channel, payload = case.get("channel", PG_CHANNEL), case["payload"]
    event = _decode(payload) if channel == PG_CHANNEL else None
    logged = None if event is None or isinstance(event, NOTIFY_ONLY) else event.id
    change = Change(channel, payload, logged)
    out: dict[str, Any] = {
        "decodes": event is not None,
        "live": frame("change", {"c": channel, "p": payload}, logged).decode(),
        "keep": {
            "agent": keep_agent(change),
            "fleet": keep_fleet(change),
            "camera": keep_camera(change),
        },
    }
    if channel != PG_CHANNEL:
        return out
    stored = case.get("stored")
    replayed = None if stored is None else _decode(stored)
    out["replay"] = (
        None
        if replayed is None
        else frame("change", {"c": PG_CHANNEL, "p": encode_event(replayed)}, replayed.id).decode()
    )
    if event is not None:
        topics = topics_of(event)
        out["topics"] = topics
        out["ws"] = (
            json.dumps(event_frame(event, topics), separators=(",", ":"), ensure_ascii=False)
            if topics
            else None
        )
    return out


def _stored(payload: str) -> str | None:
    import psycopg

    url = os.environ["SCADBUDDY_TEST_DATABASE_URL"]
    with psycopg.connect(url) as conn:
        try:
            row = conn.execute("SELECT %s::jsonb::text", (payload,)).fetchone()
        except psycopg.Error:
            return None
    assert row is not None
    return str(row[0])


def _update(doc: dict[str, Any]) -> None:
    doc["kinds"] = kinds()
    doc["notify_only"] = notify_only()
    for case in doc["changes"]:
        if case.get("channel", PG_CHANNEL) == PG_CHANNEL:
            case["stored"] = _stored(case["payload"])
        case["want"] = outputs(case)
    for case in doc["topics"]:
        case["valid"] = valid_topic(case["topic"])
    for case in doc["reprs"]:
        case["repr"] = repr(json.loads(case["json"]))
    for case in doc["frames"]:
        case["bytes"] = frame(case["kind"], case["data"], case.get("id")).decode()
    FILE.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


DOC: dict[str, Any] = json.loads(FILE.read_text())
if UPDATE:
    _update(DOC)


def test_the_kinds_are_the_models() -> None:
    assert DOC["kinds"] == kinds()


def test_the_notify_only_kinds_are_the_hubs() -> None:
    assert DOC["notify_only"] == notify_only()


@pytest.mark.parametrize("case", DOC["changes"], ids=lambda c: c["name"])
def test_a_change_is_framed_as_the_vectors_say(case: dict[str, Any]) -> None:
    assert outputs(case) == case["want"]


@pytest.mark.parametrize("case", DOC["topics"], ids=lambda c: c["topic"][:40])
def test_a_topic_is_valid_as_the_vectors_say(case: dict[str, Any]) -> None:
    assert valid_topic(case["topic"]) is case["valid"]


@pytest.mark.parametrize("case", DOC["reprs"], ids=lambda c: c["json"][:40])
def test_a_refused_value_is_named_as_the_vectors_say(case: dict[str, Any]) -> None:
    assert repr(json.loads(case["json"])) == case["repr"]


@pytest.mark.parametrize("case", DOC["frames"], ids=lambda c: c["kind"])
def test_a_stream_frame_is_written_as_the_vectors_say(case: dict[str, Any]) -> None:
    assert frame(case["kind"], case["data"], case.get("id")).decode() == case["bytes"]
