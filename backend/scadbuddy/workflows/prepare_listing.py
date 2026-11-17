"""One run per listing that renders each of its colour combinations (#6237, #6299 review).

``PrepareListing`` runs on the ``library`` queue (the API's worker), id
``prepare-listing:<listing id>``. ``POST …/storefront/listings/{id}/prepare`` starts it
(`orders/storefront/prepare.py` `start_run`); a second POST while it runs joins it.

Prepared renders run at the default priority, as a visitor's do: a visitor's render of the
same combination has the same render key, so it joins the prepared execution, and it must
not wait behind the batch. So the run does not lower their priority. It throttles them
instead: at most `PREPARE_IN_FLIGHT` prepared renders are open at once.

The run asks for the combinations by index, through ``prepare_listing_submit``: the
activity reads the listing again, builds the combination at that index and submits it with
``reuse_finished``. Its answer:

- ``open``: the combination's job is in the queue or running (this submit's, or one that
  was open already). The run waits until it settles.
- ``settled``: the job is done already; the run goes to the next index at once.
- ``failed``: the submit failed (not a full queue), or the step failed each attempt. The
  run adds its index to the memo ``failed_to_start`` and goes on. `GET …/prepare` counts
  the indices whose newest job is not done, so a later render of one clears it (#6299
  review).
- ``busy``: the render queue is full. The run waits and submits the same index again.
- ``end``: no combination has that index, or the listing is not published any more.

``prepare_listing_settled`` answers which of the open jobs are still open; the run reads it
with a backoff from `POLL_FIRST` to `POLL_MAX`. The run ends when every combination was
submitted and settled. It continues as new every `CONTINUE_EVERY` submits, or when its
history passes `HISTORY_LIMIT` events, with the next index and the open jobs.

On the versioned ``library`` worker it is AUTO_UPGRADE (`VersionedPrepareListing`), so a
change to the commands ``run`` issues goes behind ``workflow.patched``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Literal

from pydantic import BaseModel
from temporalio import workflow
from temporalio.common import RetryPolicy, VersioningBehavior
from temporalio.exceptions import ActivityError

PREPARE_LISTING_WORKFLOW = "PrepareListing"
SUBMIT_ACTIVITY = "prepare_listing_submit"
SETTLED_ACTIVITY = "prepare_listing_settled"
#: The workflow id's prefix: one run per listing.
WORKFLOW_PREFIX = "prepare-listing:"
#: The memo key of the indices whose submit failed (not a full queue), which
#: `GET …/prepare` reads. A run from before `FAILED_INDICES_PATCH` holds a count there.
FAILED_MEMO = "failed_to_start"
#: The memo holds the failed indices, not their count (#6299 review).
FAILED_INDICES_PATCH = "prepare-failed-indices"
#: The most prepared renders one run has open at once (#6299 review): a visitor's render
#: and the shop's own find the render queue almost as it would be with no run.
PREPARE_IN_FLIGHT = 2
#: Submits per run before it continues as new, so its history stays small.
CONTINUE_EVERY = 50
#: Past this many events a run continues as new, whatever the submits.
HISTORY_LIMIT = 2000
#: The first wait before the open jobs are read again, and the longest.
POLL_FIRST = timedelta(seconds=2)
POLL_MAX = timedelta(seconds=30)
#: One submit reads the listing and the price book and waits for the render's accept.
SUBMIT_TIMEOUT = timedelta(minutes=2)
#: One read of the open jobs' rows.
SETTLED_TIMEOUT = timedelta(seconds=30)
#: A submit retries a few times; one that still fails counts as ``failed``.
SUBMIT_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=5,
)
#: A read of the open jobs retries with no limit: it changes nothing.
SETTLED_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=0,
)

Outcome = Literal["open", "settled", "failed", "busy", "end"]


class OpenJob(BaseModel):
    """A prepared combination's job that is in the queue or running."""

    slug: str
    render_key: str


class PrepareListingStart(BaseModel):
    """What a run starts from: the listing, the next combination's index, the submits that
    failed so far (their count, and their indices), and the jobs still open (a run
    continued as new carries them on)."""

    listing_id: str
    next_index: int = 0
    failed_to_start: int = 0
    failed_indices: list[int] = []
    open_jobs: list[OpenJob] = []


class SubmitAsk(BaseModel):
    listing_id: str
    index: int


class Submitted(BaseModel):
    """What `prepare_listing_submit` did with one combination; ``job`` is set when
    ``outcome`` is ``open``."""

    outcome: Outcome
    job: OpenJob | None = None


class PrepareListingResult(BaseModel):
    """How many combinations the run submitted, and how many submits failed."""

    submitted: int
    failed_to_start: int


def workflow_id(listing_id: str) -> str:
    return f"{WORKFLOW_PREFIX}{listing_id}"


@workflow.defn(name=PREPARE_LISTING_WORKFLOW)
class PrepareListing:
    @workflow.run
    async def run(self, start: PrepareListingStart) -> PrepareListingResult:
        index = start.next_index
        failed = start.failed_to_start
        failed_indices = list(start.failed_indices)
        open_jobs = list(start.open_jobs)
        submitted = 0
        ended = False
        wait = POLL_FIRST
        while True:
            while not ended and len(open_jobs) < PREPARE_IN_FLIGHT and submitted < CONTINUE_EVERY:
                try:
                    answer: Submitted = await workflow.execute_activity(
                        SUBMIT_ACTIVITY,
                        SubmitAsk(listing_id=start.listing_id, index=index),
                        result_type=Submitted,
                        start_to_close_timeout=SUBMIT_TIMEOUT,
                        retry_policy=SUBMIT_RETRY,
                    )
                except ActivityError:
                    # The step itself failed each attempt (the database is away, say):
                    # the combination counts as a failed submit, and the run goes on.
                    answer = Submitted(outcome="failed")
                if answer.outcome == "end":
                    ended = True
                    break
                if answer.outcome == "busy":
                    break
                asked = index
                index += 1
                submitted += 1
                wait = POLL_FIRST
                if answer.outcome == "failed":
                    failed += 1
                    if workflow.patched(FAILED_INDICES_PATCH):
                        # Each index once per run, so at most the cap of them.
                        failed_indices.append(asked)
                        workflow.upsert_memo({FAILED_MEMO: failed_indices})
                    else:
                        workflow.upsert_memo({FAILED_MEMO: failed})
                elif answer.outcome == "open" and answer.job is not None:
                    open_jobs.append(answer.job)
            if ended and not open_jobs:
                return PrepareListingResult(submitted=index, failed_to_start=failed)
            if (
                submitted >= CONTINUE_EVERY
                or workflow.info().get_current_history_length() > HISTORY_LIMIT
            ):
                carried = PrepareListingStart(
                    listing_id=start.listing_id,
                    next_index=index,
                    failed_to_start=failed,
                    failed_indices=failed_indices,
                    open_jobs=open_jobs,
                )
                if failed_indices and workflow.patched(FAILED_INDICES_PATCH):
                    # The next run shows the failed indices from its start.
                    workflow.continue_as_new(carried, memo={FAILED_MEMO: failed_indices})
                workflow.continue_as_new(carried)
            if open_jobs:
                still: list[OpenJob] = await workflow.execute_activity(
                    SETTLED_ACTIVITY,
                    open_jobs,
                    result_type=list[OpenJob],
                    start_to_close_timeout=SETTLED_TIMEOUT,
                    retry_policy=SETTLED_RETRY,
                )
                if len(still) < len(open_jobs):
                    open_jobs = still
                    wait = POLL_FIRST
                    continue
            await workflow.sleep(wait)
            wait = min(wait * 2, POLL_MAX)


@workflow.defn(name=PREPARE_LISTING_WORKFLOW, versioning_behavior=VersioningBehavior.AUTO_UPGRADE)
class VersionedPrepareListing(PrepareListing):
    """``PrepareListing`` on the versioned ``library`` worker (#4090). AUTO_UPGRADE: a run
    of many combinations lasts longer than a rollout, so it must move to each new build.
    Its own class because an unversioned worker cannot complete a workflow that declares a
    behavior."""

    @workflow.run
    async def run(self, start: PrepareListingStart) -> PrepareListingResult:
        return await super().run(start)
