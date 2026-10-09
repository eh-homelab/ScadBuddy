"""A durable entry's request id (agent/src/gate/ids.ts).

``durable:<session id>:<workflow run id>:<tool_use_id>``: the run id makes a Reset's
replayed park a new entry, never a collision with the pre-Reset outcome. The
tool_use_id is the rest, colons included.
"""

from __future__ import annotations

import re

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def durable_request_id(session_id: str, run_id: str, tool_use_id: str) -> str:
    return f"durable:{session_id.lower()}:{run_id}:{tool_use_id}"


def parse_durable_request_id(request_id: str) -> tuple[str, str, str] | None:
    """``(session id, run id, tool_use_id)``, or None for anything else."""
    prefix, sep, rest = request_id.partition(":")
    if prefix != "durable" or not sep:
        return None
    parts = rest.split(":", 2)
    if len(parts) != 3 or not all(parts) or not _UUID.match(parts[0]):
        return None
    return parts[0].lower(), parts[1], parts[2]
