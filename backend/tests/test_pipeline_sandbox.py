"""Pipeline source in the workflow sandbox (spec 2026-09-27 §3.4 step 2, §3.6)."""

from __future__ import annotations

import inspect
import shutil
import uuid

import pytest
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from scadbuddy.workflows.sandbox import PipelineContractError, load_pipeline_module, pipeline_error
from tests.support.pipeline_probe import ExecProbe
from tests.support.temporal import TEST_TEMPORAL_DEV_SERVER, temporal_client

pytestmark = pytest.mark.requires_temporal


async def _probe(source: str) -> str:
    async with temporal_client() as client:
        queue = f"probe-{uuid.uuid4().hex[:8]}"
        async with Worker(client, task_queue=queue, workflows=[ExecProbe]):
            return await client.execute_workflow(
                ExecProbe.run, source, id=f"probe-{uuid.uuid4().hex}", task_queue=queue
            )


async def test_source_runs_and_returns() -> None:
    assert await _probe("async def run(ctx, inputs):\n    return 41 + 1\n") == "ok|42"


async def test_a_runtime_error_names_its_line() -> None:
    result = await _probe("async def run(ctx, inputs):\n    x = 1\n    return x / 0\n")
    assert (
        result
        == "builtins.ZeroDivisionError|pipeline/pipeline.py:3: ZeroDivisionError: division by zero"
    )


async def test_a_restricted_call_is_catchable_and_names_its_line() -> None:
    result = await _probe(
        "import datetime\n\nasync def run(ctx, inputs):\n    return datetime.datetime.now()\n"
    )
    kind, message = result.split("|", 1)
    assert kind.endswith("RestrictedWorkflowAccessError")
    assert message.startswith("pipeline/pipeline.py:4: RestrictedWorkflowAccessError: ")
    assert "datetime.datetime.now" in message


async def test_a_restricted_call_at_module_level_names_its_line() -> None:
    result = await _probe(
        "x = 1\nopen('/etc/hostname')\n\nasync def run(ctx, inputs):\n    return 1\n"
    )
    kind, message = result.split("|", 1)
    assert kind.endswith("RestrictedWorkflowAccessError")
    assert message.startswith("pipeline/pipeline.py:2: ")


async def test_a_syntax_error_names_its_line() -> None:
    result = await _probe("async def run(ctx, inputs)\n    return 1\n")
    assert result.split("|", 1)[1].startswith("pipeline/pipeline.py:1: SyntaxError: ")


def test_source_without_run_breaks_the_contract() -> None:
    with pytest.raises(PipelineContractError, match="defines no run"):
        load_pipeline_module("INPUTS_VERSION = 1\n", "pipeline/pipeline.py")


def test_an_error_with_no_pipeline_frame_names_only_the_file() -> None:
    assert pipeline_error(ValueError("x"), "pipeline/pipeline.py") == (
        "pipeline/pipeline.py: ValueError: x"
    )


async def test_the_testing_api_verify_pipeline_uses() -> None:
    """Task 10's `verify_pipeline` in 1.33.0: `start_local` takes an existing dev-server
    binary and a data converter, returns an environment whose client carries that
    converter and runs a workflow, and is shut down with `shutdown()`."""
    params = inspect.signature(WorkflowEnvironment.start_local).parameters
    assert {"dev_server_existing_path", "data_converter"} <= set(params)
    binary = TEST_TEMPORAL_DEV_SERVER or shutil.which("temporal")
    if binary is None:
        pytest.skip("no temporal CLI to start a local dev server with")
    env = await WorkflowEnvironment.start_local(
        dev_server_existing_path=binary, data_converter=pydantic_data_converter
    )
    try:
        assert env.client.data_converter is pydantic_data_converter
        queue = f"probe-{uuid.uuid4().hex[:8]}"
        async with Worker(env.client, task_queue=queue, workflows=[ExecProbe]):
            result = await env.client.execute_workflow(
                ExecProbe.run,
                "async def run(ctx, inputs):\n    return 1\n",
                id=f"probe-{uuid.uuid4().hex}",
                task_queue=queue,
            )
        assert result == "ok|1"
    finally:
        await env.shutdown()


def test_pipeline_error_at_keeps_the_location() -> None:
    from scadbuddy.workflows.sandbox import pipeline_error_at

    assert pipeline_error_at(ValueError("x"), "pipeline/pipeline.py", "pack goal nope") == (
        "pipeline/pipeline.py: pack goal nope"
    )
