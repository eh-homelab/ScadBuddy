"""`GET /api/v1/inventory/operations/{operation_id}` (#6949 K1, R-K1-24): an inventory write
that answered 202, followed to its answer. scadbuddy-core keeps the record
(`inventory_operations`, R-K1-22) and reads it (`GET /operations/one?id=`), so the API reads
it there: `running`, `succeeded` with the write's answer as `result`, or `failed` with its
problem as `error`. A row running for over 60 s is checked with Temporal on the read.

No ``from __future__ import annotations``: FastAPI reads the dependency annotations at
runtime (``api/components.py``).
"""

from typing import Annotated

from fastapi import APIRouter, Path

from scadbuddy.api.inventory import codes
from scadbuddy.inventory.component import InventoryDep
from scadbuddy.operations.store import Operation

router = APIRouter(tags=["inventory"])

#: The activity id: an `Idempotency-Key` scoped to who sent it (`kind:principal:key`), or
#: `req:<uuid>`. `inventory_operations.id`'s bound.
OperationId = Annotated[str, Path(min_length=1, max_length=300, pattern=r"^[^\x00]*$")]


@router.get(
    "/inventory/operations/{operation_id}",
    response_model=Operation,
    summary="An inventory operation",
    description=(
        "The inventory write a 202 named (`OperationAccepted.id`): `running` until it ends, "
        "then `succeeded` with the route's answer as `result`, or `failed` with the problem "
        "the route would have answered as `error`. Its row on the Activities tab is "
        "`inventory_operation:<id>`."
    ),
    responses=codes(
        (404, "`operation_not_found`"),
        (503, "`inventory_unavailable`: scadbuddy-core did not answer"),
    ),
)
async def get_inventory_operation(operation_id: OperationId, inventory: InventoryDep) -> Operation:
    found = await inventory.core.read("/operations/one", params={"id": operation_id})
    return Operation.model_validate(found)
