"""DurableSession's gate handlers, as the agent service sends them (agent/src/gate/names.ts)."""

from __future__ import annotations

PENDING_INPUT_QUERY = "pending_input"
RESPOND_UPDATE = "respond"
CANCEL_INPUT_UPDATE = "cancel_input"
INTERRUPT_SIGNAL = "interrupt"
# The session was marked done: an idle session returns from its run loop, a busy one
# after its turn (#1056).
END_SIGNAL = "end"
# A validator refusal reaches the client as an ApplicationFailure of this type plus
# ":<code>" (validate.py RefusalCode), which the route maps to its status.
GATE_REFUSED = "GateRefused"


def refused_failure_type(code: str) -> str:
    return f"{GATE_REFUSED}:{code}"
