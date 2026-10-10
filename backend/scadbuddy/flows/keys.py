"""A flow run's payload keys (plan 2026-10-09-durable-phase-6-flows.md Ruling 10):
`ai_payload_keys` in the database, sealed under the KEK (`SCADBUDDY_SECRET_KEY_FILE`)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import psycopg

from scadbuddy.core.secrets import SecretKeyError, load_kek
from scadbuddy.core.settings import Settings
from scadbuddy.workflows.payload_codec import Connect, PgPayloadKeys

logger = logging.getLogger(__name__)


def payload_keys(settings: Settings) -> tuple[PgPayloadKeys, Connect] | None:
    """The flow payload keys over the database's `ai_payload_keys`, or None without a
    usable KEK (logged, never raised: the rest of the app runs without flows)."""
    if settings.secret_key_file is None:
        return None
    try:
        kek = load_kek(settings.secret_key_file)
    except SecretKeyError as err:
        logger.warning("flows are unavailable: %s", err)
        return None
    url = settings.database_url

    @asynccontextmanager
    async def connect() -> AsyncIterator[psycopg.AsyncConnection[Any]]:
        async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
            yield conn

    return PgPayloadKeys(connect, kek), connect
