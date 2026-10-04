"""The payload codec's data keys (spec 2026-10-01 §6.5, plan ruling 11), read from the
agent's ai_payload_keys: each row's data key is sealed under the KEK whose id is its
kek_id, with AAD "dek:ai_payload_keys:<subject>". Keys are cached for 60 s, so a
subject forgotten by any process stops decrypting here within a minute. The agent
service creates and deletes the rows (agent/src/temporal/payloadKeys.ts)."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Protocol

from psycopg_pool import AsyncConnectionPool

from scadbuddy_durable.secrets import Kek, SealError, open_bytes

CACHE_SECONDS = 60.0


def data_key_context(subject: str) -> str:
    return f"dek:ai_payload_keys:{subject}"


class DataKeySource(Protocol):
    async def data_key(self, subject: str) -> bytes | None: ...


class PayloadKeys:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        keks: Sequence[Kek],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._pool = pool
        self._keks = {k.id: k for k in keks}
        self._clock = clock
        self._cache: dict[str, tuple[bytes, float]] = {}

    async def data_key(self, subject: str) -> bytes | None:
        cached = self._cache.get(subject)
        if cached is not None and cached[1] > self._clock():
            return cached[0]
        self._cache.pop(subject, None)
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT dek_sealed, kek_id FROM ai_payload_keys WHERE subject = %s", (subject,)
            )
            row = await cur.fetchone()
        if row is None:
            return None
        dek_sealed, kek_id = row
        kek = self._keks.get(kek_id)
        if kek is None:
            raise SealError(
                f"payload key for {subject} was sealed with key {kek_id}, which is not configured"
            )
        key = open_bytes(kek.key, bytes(dek_sealed), data_key_context(subject))
        self._cache[subject] = (key, self._clock() + CACHE_SECONDS)
        return key
