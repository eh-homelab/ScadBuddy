"""The plugin's live-output events as the assistant panel's protocol (v1), plan rulings 7-10.

Pure: the projector (projector.py) feeds each `follow_agent` event in stream order and
appends what comes back. The shapes mirror agent/src/sessions/protocol.ts `ServerEvent`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from scadbuddy_durable.models import EXPIRED_BY

PROTOCOL_VERSION = 1
APPROVAL_SUMMARY_MAX = 500  # agent/src/approvals/service.ts
STOPPED = "the turn was stopped"  # ruling 9

_WAITING = ("waiting_approval", "waiting_input")
_SETTLED = ("idle", "done", "failed")


REDACTED = "[redacted]"


def redact(text: str, secrets: Iterable[str]) -> str:
    """agent/src/secrets.ts `redact`: every occurrence of each secret of 4 or more characters."""
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, REDACTED)
    return text


def _scrub(value: Any, secrets: Sequence[str]) -> Any:
    """Every string in an event, redacted (as sdkEvents.ts `scrubForLog` scrubs classic's)."""
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, Mapping):
        return {k: _scrub(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, secrets) for v in value]
    return value


def durable_approval_id(session_id: str, tool_use_id: str) -> str:
    """Ruling 7: the id `approval.required` carries, which the approval routes recognise."""
    return f"durable:{session_id}:{tool_use_id}"


def _cap(text: str, limit: int) -> str:
    """service.ts `cap`."""
    return f"{text[: limit - 1]}…" if len(text) > limit else text


def _summary(name: str, tool_input: Any) -> str:
    """As classic: `${tool} ${JSON.stringify(input)}`, capped. A plugin-capped input is already text."""
    text = tool_input if isinstance(tool_input, str) else json.dumps(tool_input, separators=(",", ":"))
    return _cap(f"{name} {text}", APPROVAL_SUMMARY_MAX)


@dataclass
class Batch:
    """What one live event becomes: panel events, the status they leave, whether the turn ended."""

    events: list[dict[str, Any]] = field(default_factory=list)
    status: str | None = None
    final: bool = False


@dataclass
class _Text:
    segment: int
    attempt: int
    offset: int
    text: str


class Translator:
    """One session's live events, in order, as panel events.

    `open_approvals` are tool_use ids of this turn whose `approval.required` is already in
    the log with no result yet, and `resolved` those of them an `approval.resolved` in the
    log already settled (a projector taking over a session mid-turn reads both there).
    An open approval holds the status at `waiting_approval` until its result; `cancelled`
    resolves only the ones nobody resolved. `secrets` (the credentials a segment may have
    run with) are redacted from every event, as classic redacts its turn's.
    """

    def __init__(
        self,
        session_id: str,
        tiers: Mapping[str, str],
        open_approvals: Iterable[str] = (),
        resolved: Iterable[str] = (),
        *,
        secrets: Sequence[str] = (),
    ) -> None:
        self._sid = session_id
        self._tiers = tiers
        self._secrets = [s for s in secrets if s and len(s) >= 4]
        self._open: list[str] = list(dict.fromkeys(open_approvals))
        self._resolved: set[str] = set(resolved)
        self._texts: list[_Text] = []
        self._attempts: dict[int, int] = {}

    @property
    def buffered(self) -> bool:
        """Whether text is held back until the segment's next decision (ruling 10)."""
        return bool(self._texts)

    def mark_resolved(self, tool_use_ids: Iterable[str]) -> None:
        """Approvals someone else resolved (the route, after a person's decision)."""
        self._resolved.update(tool_use_ids)

    def feed(self, event: Mapping[str, Any], *, decided_by: str | None = None) -> Batch:
        """`decided_by` is the `decisions` Query's answer for a rejected `tool_result`."""
        kind = event.get("type")
        if kind == "text":
            self._text(event)
            return Batch()
        if kind == "retry":
            segment, attempt = int(event.get("segment", 0)), int(event.get("attempt", 1))
            self._attempts[segment] = max(self._attempts.get(segment, 1), attempt)
            self._texts = [t for t in self._texts if t.segment != segment or t.attempt >= attempt]
            return Batch()
        if kind == "tool_call":
            name = str(event.get("name", ""))
            call = self._event(
                "tool.call",
                id=event["id"],
                name=name,
                input=event.get("input", {}),
                risk=self._tiers.get(name, "outward"),
            )
            return Batch([*self._flush(), call])
        if kind == "approval_needed":
            call_id = str(event["id"])
            if call_id not in self._open:
                self._open.append(call_id)
            required = self._event(
                "approval.required",
                id=durable_approval_id(self._sid, call_id),
                tool=call_id,
                summary=_summary(str(event.get("name", "")), event.get("input", {})),
                risk="outward",
            )
            return self._with_status([*self._flush(), required], "waiting_approval")
        if kind == "tool_result":
            return self._result(event, decided_by)
        if kind in ("done", "error"):
            self._open, self._resolved = [], set()
        if kind == "done":
            return self._with_status([*self._flush(), self._event("session.result")], "idle", final=True)
        if kind == "error":
            failed = self._event("error", message=str(event.get("error", "")))
            return self._with_status([*self._flush(), failed], "idle", final=True)
        if kind == "cancelled":
            # The stopped segment's text was never committed to the conversation.
            self._texts = []
            resolved = [
                self._event(
                    "approval.resolved",
                    id=durable_approval_id(self._sid, call_id),
                    approved=False,
                    reason=STOPPED,
                )
                for call_id in self._open
                if call_id not in self._resolved
            ]
            self._open = []
            self._resolved = set()
            return self._with_status(resolved, "idle", final=True)
        return Batch()  # prompt, continued_as_new: the agent service logged the turn itself

    def _text(self, event: Mapping[str, Any]) -> None:
        segment, attempt = int(event.get("segment", 0)), int(event.get("attempt", 1))
        if attempt < self._attempts.get(segment, 1):
            return
        self._attempts[segment] = attempt
        self._texts = [t for t in self._texts if t.segment != segment or t.attempt >= attempt]
        self._texts.append(_Text(segment, attempt, int(event["offset"]), str(event.get("text", ""))))

    def _flush(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for t in self._texts:
            message_id = f"{self._sid}-{t.offset}"
            out.append(self._event("assistant.text.delta", messageId=message_id, delta=t.text))
            out.append(self._event("assistant.text.done", messageId=message_id))
        self._texts = []
        return out

    def _result(self, event: Mapping[str, Any], decided_by: str | None) -> Batch:
        call_id = str(event["id"])
        status = str(event.get("status", ""))
        out = self._flush()
        if status == "rejected" and decided_by == EXPIRED_BY:
            # Ruling 8: expiry has no route to log it, so it resolves here, without `by`.
            approval = durable_approval_id(self._sid, call_id)
            out.append(self._event("approval.resolved", id=approval, approved=False))
        out.append(self._event("tool.result", id=call_id, ok=status == "done", summary=status))
        self._resolved.discard(call_id)
        if call_id not in self._open:
            return Batch(out)
        self._open.remove(call_id)
        return Batch(out) if self._open else self._with_status(out, "running")

    def _with_status(self, events: list[dict[str, Any]], status: str, *, final: bool = False) -> Batch:
        return Batch([*events, self._event("session.status", status=status)], status, final)

    def _event(self, kind: str, **fields: Any) -> dict[str, Any]:
        if self._secrets:
            fields = _scrub(fields, self._secrets)
        return {"v": PROTOCOL_VERSION, "type": kind, "sessionId": self._sid, **fields}


def bus_kind_of(events: Sequence[Mapping[str, Any]]) -> tuple[str, str | None]:
    """busEvents.ts `busKindOf`: the one bus kind a batch is announced as, and its last status."""
    statuses = [str(e["status"]) for e in events if e.get("type") == "session.status"]
    status = statuses[-1] if statuses else None
    if any(e.get("type") == "session.started" for e in events):
        return "session.started", status
    if any(e.get("type") == "session.owner" for e in events):
        return "session.owner", status
    if status in _WAITING:
        return "session.waiting", status
    if status in _SETTLED:
        return "session.done", status
    return "session.message", status
