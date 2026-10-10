"""A scripted model, for tests only: the worker runs it in place of Claude when
``SCADBUDDY_DURABLE_SCRIPTED=1`` (``worker.segment_runner``).

The agent service's end-to-end test (``agent/test/durable.e2e.test.ts``) starts this
worker as a process and drives a durable turn from the chat socket; the model must
not be Anthropic's there. It answers every message with the message, and calls no tool.
"""

from __future__ import annotations

from temporalio.claude_agent_sdk.testing import Final, HistoryItem, ScriptedClaude, ToolCall


def echo(prompt: str, history: list[HistoryItem]) -> ToolCall | Final:
    return Final(f"you said: {prompt}")


def scripted_runner() -> ScriptedClaude:
    return ScriptedClaude(echo)
