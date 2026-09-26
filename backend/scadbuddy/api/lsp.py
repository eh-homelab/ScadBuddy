from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, WebSocket, status

from scadbuddy.api.deps import STATE_ATTR, AppState, SlugPath
from scadbuddy.core.fontconfig import env_for
from scadbuddy.library.lsp import serve

logger = logging.getLogger(__name__)

router = APIRouter(tags=["editor"])


async def _serve(websocket: WebSocket, state: AppState, root: Path | None) -> None:
    """Refusals close the socket before it is accepted, so the editor sees a failed
    connection and carries on without a language server — which is also what it
    does on a machine with none installed."""
    binary = shutil.which(state.config.openscad_lsp)
    if binary is None:
        logger.warning("openscad-lsp is not on PATH; the editor runs without it")
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        return
    # No await between the check and the acquire, so nothing can take the permit
    # in between.
    if state.language_servers.locked():
        await websocket.close(code=status.WS_1013_TRY_AGAIN_LATER)
        return
    async with state.language_servers:
        await websocket.accept()
        env = env_for(state.config.data_dir)
        if root is not None:
            await serve(websocket, binary, root, env)
            return
        with tempfile.TemporaryDirectory(prefix="scadbuddy-lsp-") as scratch:
            await serve(websocket, binary, Path(scratch), env)


@router.websocket("/models/{slug}/lsp")
async def model_language_server(websocket: WebSocket, slug: SlugPath) -> None:
    """openscad-lsp for a saved model, rooted in its directory so the model's
    ``include``/``use`` of its sibling files resolve as they do on render."""
    state: AppState = getattr(websocket.app.state, STATE_ATTR)
    if not state.catalogue.exists(slug):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await _serve(websocket, state, state.paths.model_dir(slug))


@router.websocket("/lsp")
async def scratch_language_server(websocket: WebSocket) -> None:
    """openscad-lsp for source that is not a model yet, in an empty directory that
    lasts as long as the session."""
    state: AppState = getattr(websocket.app.state, STATE_ATTR)
    await _serve(websocket, state, None)
