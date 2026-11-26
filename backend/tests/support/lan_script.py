"""A printer scripted by replacing `LanPrinter.request` (P8): what it answers each command,
and every payload that reached it. The API tests of the K-profile and AMS slot routes
share it (#7276)."""

from __future__ import annotations

from typing import Any

import pytest

from scadbuddy.printers.lan import LanPrinter


class Scripted:
    """What the printer answers, and what reached it."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []
        self.replies: dict[str, dict[str, Any] | None] = {}

    def reply(self, name: str, **fields: Any) -> None:
        self.replies[name] = {"result": "success", **fields}

    def refuse(self, name: str, reason: str) -> None:
        self.replies[name] = {"result": "fail", "reason": reason}

    def silent(self, name: str) -> None:
        self.replies[name] = None

    def sent(self, command: str) -> list[dict[str, Any]]:
        """The ``print`` objects published under ``command``, in order."""
        return [p for p in self.published if p["command"] == command]


def script_printer(monkeypatch: pytest.MonkeyPatch) -> Scripted:
    """Every `LanPrinter.request` answers from the returned script: success unless it
    says otherwise."""
    script = Scripted()

    async def request(
        self: LanPrinter,
        command: dict[str, Any],
        *,
        name: str,
        seq: str,
        wait: float,
        until: float | None = None,
    ) -> dict[str, Any] | None:
        script.published.append(command["print"])
        answer = script.replies.get(name, {"result": "success"})
        if answer is None:
            return None
        return {"command": name, "sequence_id": seq, **answer}

    monkeypatch.setattr(LanPrinter, "request", request)
    return script
