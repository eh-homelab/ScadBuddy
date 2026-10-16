from __future__ import annotations

import socket
from collections.abc import Iterator

import httpx
import pytest

from scadbuddy.library.settings_store import S3Settings
from scadbuddy.store.index import Pool
from tests.support.store import store_pool


@pytest.fixture
def pool(pg_conninfo: str) -> Iterator[Pool]:
    with store_pool(pg_conninfo) as opened:
        yield opened


@pytest.fixture
def s3() -> Iterator[S3Settings]:
    """moto's S3 on a free port with the bucket ``sb-test`` (S3 needs three characters or more)."""
    from moto.server import ThreadedMotoServer

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
    server.start()
    settings = S3Settings(
        endpoint=f"http://127.0.0.1:{port}",
        bucket="sb-test",
        prefix="t",
        access_key_id="testing",
        secret_access_key="testing",
    )
    httpx.put(f"{settings.endpoint}/sb-test").raise_for_status()
    try:
        yield settings
    finally:
        # moto's state is the process's, not the server's: the next test starts empty.
        httpx.post(f"{settings.endpoint}/moto-api/reset").raise_for_status()
        server.stop()
