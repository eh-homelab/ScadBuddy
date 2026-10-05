"""The plugin's SegmentRunner: one ClaudeAgentSdkRunner per segment (plan deviation 3)."""

from __future__ import annotations

import re
import warnings
from collections.abc import Callable
from typing import Any

from claude_agent_sdk import SessionStore
from temporalio import activity
from temporalio.claude_agent_sdk import ClaudeAgentSdkRunner
from temporalio.claude_agent_sdk._activity import SegmentRunner
from temporalio.claude_agent_sdk._models import SegmentInput, SegmentOutput
from temporalio.exceptions import ApplicationError

from scadbuddy_durable.credentials import CredentialSource, NoUsableCredential, credential_env
from scadbuddy_durable.segments import Segments

CWD = "/srv/agent"

_SESSION_ID = re.compile(r"session-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def session_of(workflow_id: str) -> str:
    """The `ai_sessions.id` a workflow id names; anything but `session-<uuid>` is refused."""
    if not _SESSION_ID.fullmatch(workflow_id):
        raise ApplicationError(f"workflow id {workflow_id!r} is not a session", non_retryable=True)
    return workflow_id.removeprefix("session-")


def extra_options(plugin_dir: str, prompt_append: str) -> dict[str, Any]:
    return {
        "plugins": [{"type": "local", "path": plugin_dir}],
        "system_prompt": {"type": "preset", "preset": "claude_code", "append": prompt_append},
    }


class SessionRunner:
    """The plugin's SegmentRunner (deviation 3): one ClaudeAgentSdkRunner per segment,
    with that session's credential env and remaining budget."""

    def __init__(
        self,
        credentials: CredentialSource,
        segments: Segments,
        store: SessionStore,
        *,
        plugin_dir: str,
        prompt_append: str,
        cwd: str = CWD,
        runner_factory: Callable[..., SegmentRunner] = ClaudeAgentSdkRunner,
    ) -> None:
        self._credentials, self._segments, self._store = credentials, segments, store
        self._extra = extra_options(plugin_dir, prompt_append)
        self._cwd, self._factory = cwd, runner_factory

    async def run(self, inp: SegmentInput, attempt: int) -> SegmentOutput:
        session_id = session_of(activity.info().workflow_id or "")
        limits = await self._segments.limits(session_id)
        remaining = limits.budget_usd - limits.cost_usd
        if remaining <= 0:
            return SegmentOutput(
                session_id=inp.session_id, is_error=True, error="the session's budget is spent"
            )
        try:
            credential = await self._credentials.first_usable()
        except NoUsableCredential as err:
            raise ApplicationError(str(err), non_retryable=True) from None
        with warnings.catch_warnings():
            # The runner warns when none of its ENV_AUTH names is set; a gateway's
            # ANTHROPIC_AUTH_TOKEN is not among them and survives resumes too.
            warnings.simplefilter("ignore")
            runner = self._factory(
                session_store=self._store,
                cwd=self._cwd,
                env=credential_env(credential),
                extra_options=self._extra,
                max_budget_usd=remaining,
            )
        out = await runner.run(inp, attempt)
        await self._segments.record(session_id, inp.segment_index, attempt, out.session_id, out.cost_usd)
        return out
