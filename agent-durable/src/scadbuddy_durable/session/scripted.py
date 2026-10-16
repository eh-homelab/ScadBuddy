"""A scripted model, for tests only: the worker runs it in place of Claude when
``SCADBUDDY_DURABLE_SCRIPTED=1`` (``worker.segment_runner``).

The agent service's end-to-end test (``agent/test/durable.e2e.test.ts``) starts this
worker as a process and drives a durable turn from the chat socket; the model must
not be Anthropic's there. It answers every message with the message, and calls no tool,
except for a message ``fetch <url>``: it calls the turn's ``http_request`` (#2320), as a
durable tool the agent serves, and answers with what came back.
"""

from __future__ import annotations

from temporalio.claude_agent_sdk.testing import Final, HistoryItem, ScriptedClaude, ToolCall

FETCH = "fetch "
HTTP_REQUEST = "scadbuddy_http__http_request"


def echo(prompt: str, history: list[HistoryItem]) -> ToolCall | Final:
    if prompt.startswith(FETCH):
        if history:
            last = history[-1]
            return Final(f"fetched ({'error' if last.is_error else 'ok'}): {last.content}")
        url = prompt[len(FETCH) :].split()[0]
        return ToolCall(HTTP_REQUEST, {"url": url, "method": "GET"})
    return Final(f"you said: {prompt}")


def scripted_runner() -> ScriptedClaude:
    return ScriptedClaude(echo)
