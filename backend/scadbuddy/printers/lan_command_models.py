"""What crosses ``LanCommand``'s history (#7276, plan 2026-11-25 Task 2).

A command's payload is built by the caller (`printers/lan_commands.py`); the activity puts
a fresh ``sequence_id`` into it (R-7276-10). Nothing here holds a secret: the access code
is opened from the printer's row inside the activity.

The timeouts are pure, so the workflow computes them (P20): a command's start-to-close
is the MQTT connect timeout plus its reply waits plus `LAN_COMMAND_MARGIN`, a read's
waits being its tries and the pauses between them; the caller waits for a run up to the
sum of its commands' timeouts.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from scadbuddy.printers.lan import MQTT_TIMEOUT
from scadbuddy.workflows.print_models import (
    LAN_COMMAND_MARGIN,
    LAN_READ_PAUSE,
    LAN_READ_TRIES,
)


class LanCommandInput(BaseModel):
    printer_id: str
    #: The payload's ``command``, which a reply must echo (`lan_commands.matches`).
    name: str
    #: A builder's payload; its ``print.sequence_id`` is replaced as it is sent.
    payload: dict[str, Any]
    #: How long one try waits for the reply, in seconds.
    wait_s: float
    #: A ``read`` is tried `LAN_READ_TRIES` times and fails with no reply; a ``write`` is
    #: sent once and answers ``confirmed: false`` with none (R-7276-9).
    kind: Literal["read", "write"]


class LanCommandReply(BaseModel):
    #: The printer answered (and did not refuse).
    confirmed: bool
    #: The matching ``print`` object, as the printer sent it.
    reply: dict[str, Any] | None = None
    received_at: datetime | None = None


class LanCommandRun(BaseModel):
    """The commands of one request, run in order; the first failure stops the rest."""

    commands: list[LanCommandInput] = Field(min_length=1, max_length=3)


class LanCommandOutcome(BaseModel):
    replies: list[LanCommandReply]


def tries(command: LanCommandInput) -> int:
    return LAN_READ_TRIES if command.kind == "read" else 1


def command_timeout(command: LanCommandInput) -> timedelta:
    """``lan_command``'s start-to-close for ``command`` (P20)."""
    n = tries(command)
    seconds = n * (MQTT_TIMEOUT + command.wait_s) + (n - 1) * LAN_READ_PAUSE
    return timedelta(seconds=seconds + LAN_COMMAND_MARGIN)


def run_timeout(run: LanCommandRun) -> timedelta:
    """How long a caller waits for ``run``: the sum of its commands' timeouts (P20)."""
    return sum((command_timeout(c) for c in run.commands), timedelta())
