"""The off-box git push (#4974) as a component (`core/components.py`): both repositories
pushed after each of their commits, and by the housekeeping sweep."""

from __future__ import annotations

from typing import Annotated

from scadbuddy.api.components import component_dep
from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.flows.component import FLOWS
from scadbuddy.git_remote.push import RemotePush, RemoteTarget
from scadbuddy.git_remote.store import PushRecords

GIT_REMOTE: Key[RemotePush] = Key("git_remote")


def _build(core: Core, components: Components) -> RemotePush:
    def target() -> RemoteTarget | None:
        # The running settings, read at each push: the three fields are live.
        settings = core.settings
        if not settings.git_remote_url:
            return None
        return RemoteTarget(
            url=settings.git_remote_url,
            username=settings.git_remote_username,
            token=settings.git_remote_token,
        )

    push = RemotePush(
        {"models": core.history, "flows": components.get(FLOWS).definitions.history},
        PushRecords(core.projection.pool),
        target,
    )
    push.watch()
    return push


COMPONENT = Component(GIT_REMOTE, build=_build)

GitRemoteDep = Annotated[RemotePush, component_dep(GIT_REMOTE)]
