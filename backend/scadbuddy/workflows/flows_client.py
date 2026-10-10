"""The Temporal client of everything that touches a flow run (plan
2026-10-09-durable-phase-6-flows.md, Rulings 9 and 10).

A flow's payloads are sealed per run by the subject payload codec, so the flows client
is its own client: the render, print and library clients keep `pydantic_data_converter`.
"""

from collections.abc import Callable, Sequence
from typing import Any

from temporal_agent_harness.plugin import AgentHarnessPlugin
from temporalio.client import Client
from temporalio.contrib.opentelemetry import TracingInterceptor

from scadbuddy.workflows.payload_codec import PayloadKeys, flows_converter


def harness_plugins(tools: Sequence[Callable[..., Any]]) -> list[AgentHarnessPlugin]:
    """The `projects` worker's plugins. No large-payload offload: the harness's default
    store is one host's disk, which the API and the worker do not share (Ruling 9)."""
    return [AgentHarnessPlugin(tools=list(tools), large_payload_offload=None)]


async def connect_flows(
    address: str, namespace: str, keys: PayloadKeys, *, lazy: bool = False
) -> Client:
    return await Client.connect(
        address,
        namespace=namespace,
        data_converter=flows_converter(keys),
        lazy=lazy,
        interceptors=[TracingInterceptor()],
    )
