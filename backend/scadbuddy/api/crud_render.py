"""The render worker's CRUD routes (#5298, inventory R1): the store settings it renders
with, for `crud/render.py` `RemoteRenderSettingsSource`.

The answer holds secrets in plaintext, released only to ``crud.render`` (plan D8): the
render key (or the full key while no render key is saved) and the S3 keys. The CRUD
router answers ``Cache-Control: no-store``, and each answer is logged with the secrets it
released, never their values.
"""

from __future__ import annotations

import asyncio
import logging

from scadbuddy.api.deps import StateDep
from scadbuddy.crud.guard import crud_router
from scadbuddy.library.settings_store import RenderStoreSettings, load_render_store_settings

logger = logging.getLogger(__name__)

router = crud_router("render")


def released(current: RenderStoreSettings) -> list[str]:
    """The names of the secrets in ``current``, for the log."""
    names = []
    if current.api_key is not None:
        names.append("bambuddy_api_key" if current.key_is_fallback else "bambuddy_render_api_key")
    if current.s3 is not None:
        names += ["store_s3_access_key_id", "store_s3_secret_access_key"]
    return names


@router.get("/settings", name="crud_render_settings")
async def render_settings(state: StateDep) -> RenderStoreSettings:
    current = await asyncio.to_thread(
        load_render_store_settings, state.settings_store.pool, state.settings
    )
    logger.info(
        "released the render settings to the render worker, secrets: %s",
        ", ".join(released(current)) or "none",
    )
    return current
