"""`python -m scadbuddy.workflows.verify_pipeline <template> --inputs cases.json`: run a
template's pipeline on a local Temporal dev server with the local store (spec §5.5), for
`verify.sh`. No Postgres: the job row is a list in memory."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import uuid
import zipfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import TimeoutError as TemporalTimeoutError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.assets import AssetStore
from scadbuddy.render.inputs import InputsError, normalize_inputs
from scadbuddy.render.job_models import Job
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.workflows.activities import RenderActivities, WorkerDeps
from scadbuddy.workflows.client import pydantic_data_converter
from scadbuddy.workflows.models import Projection
from scadbuddy.workflows.pipeline_activities import PipelineActivities
from scadbuddy.workflows.pipelines import RenderPiece, TemplatePipeline

TASK_QUEUE = "verify"


class _Refs:
    """`blob_refs` without Postgres: the store is thrown away after the run, so a ref
    has nothing to hold."""

    def add(self, key: str, holder_kind: str, holder_id: str) -> None:
        pass


def dev_server() -> str | None:
    """The pinned `temporal` CLI, as tests/support/temporal.py finds it: never a
    download at run time (the test image carries the binary)."""
    return os.environ.get("SCADBUDDY_TEST_TEMPORAL_DEV_SERVER") or shutil.which("temporal")


async def verify(template: Path, cases: list[dict[str, Any]], *, config: Config) -> list[str]:
    """Run every case through the template's pipeline; the failures, one line each."""
    binary = dev_server()
    if binary is None:
        raise SystemExit(
            "no temporal CLI: set SCADBUDDY_TEST_TEMPORAL_DEV_SERVER or put temporal on PATH"
        )
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="scadbuddy-verify-") as root:
        paths = DataPaths(Path(root))
        paths.ensure()
        slug = template.name
        # Dot-named entries are not the template: verify.sh's `.verify` renders, a
        # `.renders` cache, `.git`.
        shutil.copytree(template, paths.model_dir(slug), ignore=shutil.ignore_patterns(".*"))
        deps = WorkerDeps(
            config=replace(config, data_dir=paths.root),
            paths=paths,
            assets=AssetStore(paths.assets),
            blobs=LocalBlobStore(paths.blobs),
            refs=_Refs(),  # type: ignore[arg-type]
            projection=None,  # type: ignore[arg-type]
        )
        finals: dict[str, Projection] = {}

        @activity.defn(name="project")
        async def project(projection: Projection) -> None:
            if projection.state not in (None, "running"):
                finals[projection.job_id] = projection

        render = RenderActivities(deps)
        acts = [a for a in render.all() if a != render.project]
        acts += [project, *PipelineActivities(deps).all()]
        # The form Task 1 measured against 1.33.0 (`test_the_testing_api_verify_pipeline_uses`).
        env = await WorkflowEnvironment.start_local(
            dev_server_existing_path=binary, data_converter=pydantic_data_converter
        )
        try:
            async with Worker(
                env.client,
                task_queue=TASK_QUEUE,
                workflows=[TemplatePipeline, RenderPiece],
                activities=acts,
            ):
                # Production's bound on every pipeline (`RenderService`), so a `run` that
                # never yields is that case's failure, not a hang.
                bound = deps.config.pipeline_timeout
                for number, case in enumerate(cases, start=1):
                    try:
                        # Exactly as the API takes inputs: `v` stamped, params checked.
                        normalized = normalize_inputs(case, None)
                    except InputsError as refused:
                        failures.append(f"case {number}: {refused}")
                        continue
                    job = Job(
                        id=uuid.uuid4().hex,
                        slug=slug,
                        params=normalized.params,
                        inputs=normalized.data,
                        created_at=datetime.now(UTC),
                    )
                    try:
                        await env.client.execute_workflow(
                            TemplatePipeline.run,
                            job,
                            id=f"render-{job.id}",
                            task_queue=TASK_QUEUE,
                            execution_timeout=timedelta(seconds=bound),
                        )
                    except WorkflowFailureError as failed:
                        if isinstance(failed.cause, TemporalTimeoutError):
                            failures.append(f"case {number}: timed out after {bound:g} s")
                        else:
                            failures.append(f"case {number}: {failed.cause or failed}")
                        continue
                    final = finals.get(job.id)
                    if final is None or final.state != "done":
                        why = final.failure.error if final and final.failure else "no final state"
                        failures.append(f"case {number}: {why}")
                        continue
                    if not final.outputs:
                        failures.append(f"case {number}: no output")
                    for output in final.outputs:
                        try:
                            with zipfile.ZipFile(paths.root / output.result.model_3mf) as archive:
                                bad = archive.testzip()
                        except (OSError, zipfile.BadZipFile) as error:
                            failures.append(f"case {number}: {output.name}: {error}")
                            continue
                        if bad is not None:
                            failures.append(f"case {number}: {output.name}: {bad} is corrupt")
        finally:
            await env.shutdown()
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m scadbuddy.workflows.verify_pipeline")
    parser.add_argument("template", type=Path)
    parser.add_argument("--inputs", type=Path, required=True)
    args = parser.parse_args()
    cases = json.loads(args.inputs.read_text(encoding="utf-8"))
    template = args.template.resolve()
    failures = asyncio.run(verify(template, cases, config=Config(data_dir=Path("/unused"))))
    for line in failures:
        print(f"FAIL {line}")
    print(f"{len(cases) - len(failures)} of {len(cases)} pipeline cases passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
