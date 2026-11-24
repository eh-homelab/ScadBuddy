"""Starts an attempt's media import (#7233, spec 2026-11-05 §4.1).

`TemporalMediaStarter` starts one ``AttemptMedia`` workflow per attempt
(`workflows/attempt_media.py`), as `storefront/staged_files.py` `TemporalWatcher` starts
its watchers: a run still going is joined, and a run that ended is started again. A
start with ``retries`` is a signal-with-start, so a run it joins steps again too. A
start is one Temporal call and never waits on Bambuddy. `start_media` logs a failed start
and never raises it, so a route that starts the import still answers with Temporal down.
"""

from __future__ import annotations

import logging
from typing import Protocol

from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from scadbuddy.workflows.attempt_media import (
    ATTEMPT_MEDIA_WORKFLOW,
    RETRIES_SIGNAL,
    AttemptMediaInput,
    workflow_id,
)

logger = logging.getLogger(__name__)


class MediaStarter(Protocol):
    """Starts an attempt's import (`TemporalMediaStarter`; a test's fake)."""

    async def start(self, fulfilment_id: str, *, retries: bool) -> None: ...


class TemporalMediaStarter:
    def __init__(self, client: Client, task_queue: str) -> None:
        self.client = client
        self.task_queue = task_queue

    async def start(self, fulfilment_id: str, *, retries: bool) -> None:
        await self.client.start_workflow(
            ATTEMPT_MEDIA_WORKFLOW,
            AttemptMediaInput(fulfilment_id=fulfilment_id, retries=retries),
            id=workflow_id(fulfilment_id),
            task_queue=self.task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            # A run already going keeps its input: the signal asks it for retries.
            start_signal=RETRIES_SIGNAL if retries else None,
        )


async def start_media(starter: MediaStarter, fulfilment_id: str, *, retries: bool) -> None:
    """Start the attempt's import; a failure is logged, never raised."""
    try:
        await starter.start(fulfilment_id, retries=retries)
    except Exception:
        logger.warning(
            "could not start the media import of attempt %s", fulfilment_id, exc_info=True
        )
