"""Bambuddy's status words, mapped onto one vocabulary (#89).

Shared by the progress read and the archive linking (#306), which must agree on what
counts as a finished or running job.
"""

from __future__ import annotations

from typing import Literal

#: Normalised across both routes. ``unknown`` is a real state: Bambuddy's status
#: vocabularies differ per object and a new value must render as "still going" rather
#: than silently as "done", which would stop the polling on a print that is still live.
Stage = Literal["running", "queued", "done", "failed", "cancelled", "unknown"]

#: Bambuddy's own words for a finished state, per object. Anything outside these is
#: treated as still in flight.
_DONE = {"completed", "complete", "done", "finished", "success", "succeeded", "printed"}
_FAILED = {"failed", "error", "errored"}
_CANCELLED = {"cancelled", "canceled", "aborted"}
_QUEUED = {"queued", "pending", "waiting", "scheduled"}


def stage_of(status: str | None) -> Stage:
    """Map one of Bambuddy's status strings onto the shared vocabulary."""
    if not status:
        return "unknown"
    value = status.strip().lower()
    if value in _DONE:
        return "done"
    if value in _FAILED:
        return "failed"
    if value in _CANCELLED:
        return "cancelled"
    if value in _QUEUED:
        return "queued"
    if value in {"running", "printing", "in_progress", "slicing", "dispatching"}:
        return "running"
    return "unknown"
