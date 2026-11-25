from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from scadbuddy.core.components import Components, Core
from scadbuddy.core.events import InProcessEventBus
from scadbuddy.rows.component import COMPONENT as ROWS_COMPONENT
from scadbuddy.rows.component import ROWS
from scadbuddy.tasks.component import COMPONENT, TASKS
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres


def test_building_opens_nothing_and_first_use_opens_the_pool(
    pg_conninfo: str, pg_pool: PgPool
) -> None:
    core = cast(
        Core,
        SimpleNamespace(
            settings=SimpleNamespace(database_url=pg_conninfo), events=InProcessEventBus()
        ),
    )
    components = Components(core, [ROWS_COMPONENT, COMPONENT])
    tasks = components.get(TASKS)
    pool = components.get(ROWS).pool
    assert pool.closed
    try:
        assert tasks.store.get("nope") is None
        assert not pool.closed
    finally:
        pool.close()
