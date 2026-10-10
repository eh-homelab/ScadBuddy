"""Shared fixtures of the flow tests: one Temporal dev server for the module set."""

from collections.abc import Iterator

import pytest

from tests.support.temporal import temporal_available, temporal_server


@pytest.fixture(scope="session")
def temporal_address() -> Iterator[str]:
    if not temporal_available():
        pytest.skip("no Temporal")
    with temporal_server() as address:
        yield address
