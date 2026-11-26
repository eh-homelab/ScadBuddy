"""The agent's Claude credentials, ``ai_credentials`` (#255, #1093; #5299 D5): their re-wrap
under a new KEK at the API's start (`rotation.py`).

The routes that seal, open and write them are Go's (``tools/go/core/internal/secrets``,
#7667). The AAD binds a sealed secret to its row and to the columns that say where it is sent
(``credentialAad``: the row id, ``kind`` and ``base_url``), as ``agent/src/credentials.ts``
writes it.
"""

from __future__ import annotations

import json
from typing import Any

from psycopg import Connection

from scadbuddy.core.secrets import SEAL_V1, Envelope, Kek, SealError
from scadbuddy.core.secrets import rewrap as rewrap_envelope

LEGACY_FORMAT_MESSAGE = (
    "the stored credential was saved in an older format that did not bind its kind and"
    " base URL; save it again"
)


def credential_aad(row_id: str, kind: str, base_url: str | None) -> str:
    """credentials.ts ``credentialAad``: JSON as ``JSON.stringify`` writes it."""
    bound = json.dumps(
        {"kind": kind, "base_url": base_url}, separators=(",", ":"), ensure_ascii=False
    )
    return f"ai_credentials:{row_id}:{bound}"


def _dict(row: Any, names: list[str]) -> dict[str, Any]:
    return dict(row) if isinstance(row, dict) else dict(zip(names, row, strict=True))


def rewrap(conn: Connection[Any], previous: Kek, current: Kek) -> tuple[int, int]:
    """Key rotation (spec §9, "Rotating it re-wraps the data keys only"): each row whose
    data key is sealed under ``previous`` gets it sealed under ``current``; the sealed
    secret and ``updated_at`` stay. A row ``previous`` cannot open (altered, or v1) is
    left for the operator and counted as failed; each update is conditional on
    ``kek_id``, so two pods rotating at once do not overwrite each other. Answers
    (re-wrapped, failed)."""
    if previous.id == current.id:
        return 0, 0
    cur = conn.execute(
        "SELECT id, kind, base_url, secret_sealed, dek_sealed, kek_id FROM ai_credentials"
        " WHERE kek_id = %s",
        (previous.id,),
    )
    names = [d.name for d in cur.description or []]
    rewrapped = failed = 0
    for found in cur.fetchall():
        row = _dict(found, names)
        envelope = Envelope(
            secret_sealed=bytes(row["secret_sealed"]),
            dek_sealed=bytes(row["dek_sealed"]),
            kek_id=row["kek_id"],
        )
        try:
            if envelope.secret_sealed[:1] == bytes([SEAL_V1]):
                raise SealError(LEGACY_FORMAT_MESSAGE)
            aad = credential_aad(row["id"], row["kind"], row["base_url"])
            resealed = rewrap_envelope(previous, current, envelope, aad)
        except SealError:
            failed += 1
            continue
        updated = conn.execute(
            "UPDATE ai_credentials SET dek_sealed = %s, kek_id = %s WHERE id = %s AND kek_id = %s",
            (resealed.dek_sealed, resealed.kek_id, row["id"], previous.id),
        )
        rewrapped += updated.rowcount
    return rewrapped, failed
