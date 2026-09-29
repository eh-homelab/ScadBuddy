from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, WebSocket, status
from pydantic import BaseModel, Field

from scadbuddy.api.deps import STATE_ATTR, AppState, SlugPath, StateDep
from scadbuddy.api.models import MAX_SOURCE_CHARS
from scadbuddy.core.fontconfig import env_for
from scadbuddy.core.problems import ApiError
from scadbuddy.library.lsp import serve
from scadbuddy.library.lsp_diagnostics import LspDiagnostic, LspDiagnosticsError, lsp_diagnostics
from scadbuddy.library.slugs import MAX_MODEL_ID_LENGTH, MODEL_ID_PATTERN

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


#: What a 503 from a busy language-server budget says to wait.
LSP_RETRY_AFTER = 2


class LspDiagnosticsRequest(BaseModel):
    source: str = Field(max_length=MAX_SOURCE_CHARS, description="The OpenSCAD source")
    slug: str | None = Field(
        default=None,
        pattern=MODEL_ID_PATTERN,
        max_length=MAX_MODEL_ID_LENGTH,
        description="Open the source in this model's directory, so its includes resolve",
    )


class LspDiagnostics(BaseModel):
    available: bool = Field(description="False when no openscad-lsp is installed to ask")
    diagnostics: list[LspDiagnostic] = Field(default_factory=list)


@router.post(
    "/lsp/diagnostics",
    response_model=LspDiagnostics,
    summary="openscad-lsp's diagnostics for a source",
    description=(
        "Runs the editor's language server once on `source` and returns what it "
        "publishes: tree-sitter parse errors with line and column ranges, and a missing "
        "file for a leading `include`. Saves nothing and runs no OpenSCAD; "
        "`POST /models/check` is OpenSCAD's own check. Shares the editor's "
        "`SCADBUDDY_LSP_SESSIONS` budget: 503 with Retry-After when it is full (#252)."
    ),
)
async def post_lsp_diagnostics(body: LspDiagnosticsRequest, state: StateDep) -> LspDiagnostics:
    root: Path | None = None
    if body.slug is not None:
        if not state.catalogue.exists(body.slug):
            raise ApiError(status.HTTP_404_NOT_FOUND, f"no model named {body.slug!r}")
        root = state.paths.model_dir(body.slug)
    binary = shutil.which(state.config.openscad_lsp)
    if binary is None:
        return LspDiagnostics(available=False)
    if state.language_servers.locked():
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "every language server is in use; try again shortly",
            headers={"Retry-After": str(LSP_RETRY_AFTER)},
        )
    async with state.language_servers:
        env = env_for(state.config.data_dir)
        try:
            if root is not None:
                found = await lsp_diagnostics(binary, root, body.source, env=env)
            else:
                with tempfile.TemporaryDirectory(prefix="scadbuddy-lsp-") as scratch:
                    found = await lsp_diagnostics(binary, Path(scratch), body.source, env=env)
        except LspDiagnosticsError as error:
            raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, str(error)) from None
    return LspDiagnostics(available=True, diagnostics=found)
