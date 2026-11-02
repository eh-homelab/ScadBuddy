"""How far the off-box git remote is behind the model and flow history (#4974)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter
from pydantic import BaseModel, Field

from scadbuddy.git_remote.component import GitRemoteDep
from scadbuddy.git_remote.push import RepositoryLag

router = APIRouter(tags=["settings"])


class GitRemoteStatus(BaseModel):
    """The remote set in Settings (its URL is ``GET /settings``'s ``git_remote_url``) and
    each repository's lag. Never the token."""

    #: Whether a remote is set; without one nothing is pushed and ``repositories`` is
    #: empty.
    configured: bool
    repositories: list[RepositoryLag] = Field(default_factory=list)


@router.get(
    "/settings/git-remote",
    response_model=GitRemoteStatus,
    summary="How far the git remote is behind the model and flow history",
)
async def get_git_remote(push: GitRemoteDep) -> GitRemoteStatus:
    """Each repository's newest commit, the last one a push landed at the remote, and
    how many commits (and since when) it is behind. A commit is pushed once it lands,
    and the housekeeping sweep pushes what is still behind every few minutes."""
    configured = push.target() is not None
    repositories = await asyncio.to_thread(push.lag) if configured else []
    return GitRemoteStatus(configured=configured, repositories=repositories)
