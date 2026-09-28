from __future__ import annotations

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter

RENDER_TASK_QUEUE_DEFAULT = "render"
DEPLOYMENT_NAME = "scadbuddy-render"


async def connect(address: str, namespace: str) -> Client:
    return await Client.connect(
        address, namespace=namespace, data_converter=pydantic_data_converter
    )


__all__ = ["DEPLOYMENT_NAME", "RENDER_TASK_QUEUE_DEFAULT", "connect", "pydantic_data_converter"]
