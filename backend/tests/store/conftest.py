from __future__ import annotations

from collections.abc import Iterator

import pytest

from scadbuddy.store.index import Pool
from tests.support.store import store_pool


@pytest.fixture
def pool(pg_conninfo: str) -> Iterator[Pool]:
    with store_pool(pg_conninfo) as opened:
        yield opened
