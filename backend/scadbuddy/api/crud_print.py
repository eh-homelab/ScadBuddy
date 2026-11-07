"""The print worker's CRUD routes (#5298): the settings it prints with, for
`crud/print.py` `RemoteSettingsStore`.

The settings answer holds secrets in plaintext, released only to ``crud.print`` (plan D8):
the Bambuddy key, which the Bambuddy spool provider also uses (`PRINT_SECRETS`). Every
other sealed field is null (`print_projection`). The CRUD router answers
``Cache-Control: no-store``, and each answer is logged with the secrets it released and
the credential it released them to, never their values.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import Request

from scadbuddy.api.deps import StateDep
from scadbuddy.auth.gate import caller_of
from scadbuddy.auth.models import Service
from scadbuddy.crud.guard import crud_router
from scadbuddy.library.settings_store import PRINT_SECRETS, PrintSettings, print_projection

logger = logging.getLogger(__name__)

router = crud_router("print")


def released(current: PrintSettings) -> list[str]:
    """The names of the secrets in ``current``, for the log."""
    return sorted(name for name in PRINT_SECRETS if getattr(current, name) is not None)


@router.get("/settings", name="crud_print_settings")
async def print_settings(request: Request, state: StateDep) -> PrintSettings:
    snapshot = await asyncio.to_thread(state.settings_store.snapshot)
    current = print_projection(snapshot)
    # The guard admitted only a print `Service`.
    principal = caller_of(request.scope)
    assert isinstance(principal, Service)
    logger.info(
        "released the print settings to service credential %s, secrets: %s",
        principal.credential_id,
        ", ".join(released(current)) or "none",
    )
    return current
