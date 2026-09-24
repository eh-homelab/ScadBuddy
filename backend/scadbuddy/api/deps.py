from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, Path, Request

from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.library.catalogue import Catalogue
from scadbuddy.library.fonts import FontService
from scadbuddy.library.history import COMMIT_ID_PATTERN, ModelHistory
from scadbuddy.library.outputs import OUTPUT_ID_PATTERN, OutputStore
from scadbuddy.library.settings_store import SETTINGS_NAME, SettingsStore
from scadbuddy.library.slugs import SLUG_PATTERN
from scadbuddy.render.jobs import RenderQueue
from scadbuddy.render.solids import WRAPPER_PREFIX

logger = logging.getLogger(__name__)

STATE_ATTR = "scadbuddy"
VERSION_TIMEOUT = 10.0
JOB_ID_PATTERN = r"^[0-9a-f]{32}$"


@dataclass
class AppState:
    settings: Settings
    config: Config
    paths: DataPaths
    history: ModelHistory
    catalogue: Catalogue
    outputs: OutputStore
    settings_store: SettingsStore
    fonts: FontService
    queue: RenderQueue
    openscad_version: str | None = field(default=None)


def build_state(settings: Settings) -> AppState:
    config = settings.to_config()
    paths = DataPaths(root=settings.data_dir)
    history = ModelHistory(paths.models, wrapper_prefix=WRAPPER_PREFIX, timeout=config.git_timeout)
    return AppState(
        settings=settings,
        config=config,
        paths=paths,
        history=history,
        catalogue=Catalogue(paths, history),
        outputs=OutputStore(paths),
        settings_store=SettingsStore(paths.root / SETTINGS_NAME, settings),
        fonts=FontService(
            paths.root,
            api_key=config.google_fonts_api_key,
            catalogue_ttl=config.fonts_catalogue_ttl,
        ),
        queue=RenderQueue(config, paths, history=history),
    )


async def probe_openscad_version(config: Config) -> str | None:
    """``openscad --version`` writes to stderr, so both streams are merged."""
    if shutil.which(config.openscad) is None:
        return None
    try:
        process = await asyncio.create_subprocess_exec(
            config.openscad,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=VERSION_TIMEOUT)
    except (OSError, TimeoutError):
        logger.exception("could not read the openscad version")
        return None
    if process.returncode != 0:
        return None
    first = stdout.decode("utf-8", "replace").strip().splitlines()
    return first[0].strip() if first else None


def get_state(request: Request) -> AppState:
    state: AppState = getattr(request.app.state, STATE_ATTR)
    return state


StateDep = Annotated[AppState, Depends(get_state)]


def get_config(state: StateDep) -> Config:
    return state.config


def get_paths(state: StateDep) -> DataPaths:
    return state.paths


def get_catalogue(state: StateDep) -> Catalogue:
    return state.catalogue


def get_history(state: StateDep) -> ModelHistory:
    return state.history


def get_outputs(state: StateDep) -> OutputStore:
    return state.outputs


def get_settings_store(state: StateDep) -> SettingsStore:
    return state.settings_store


def get_fonts(state: StateDep) -> FontService:
    return state.fonts


def get_queue(state: StateDep) -> RenderQueue:
    return state.queue


ConfigDep = Annotated[Config, Depends(get_config)]
PathsDep = Annotated[DataPaths, Depends(get_paths)]
CatalogueDep = Annotated[Catalogue, Depends(get_catalogue)]
HistoryDep = Annotated[ModelHistory, Depends(get_history)]
OutputsDep = Annotated[OutputStore, Depends(get_outputs)]
SettingsStoreDep = Annotated[SettingsStore, Depends(get_settings_store)]
FontsDep = Annotated[FontService, Depends(get_fonts)]
QueueDep = Annotated[RenderQueue, Depends(get_queue)]

SlugPath = Annotated[str, Path(pattern=SLUG_PATTERN, max_length=100)]
JobIdPath = Annotated[str, Path(pattern=JOB_ID_PATTERN)]
OutputIdPath = Annotated[str, Path(pattern=OUTPUT_ID_PATTERN)]
# Abbreviated ids are accepted the way git accepts them; the API always answers
# with the full 40 characters.
CommitPath = Annotated[str, Path(pattern=COMMIT_ID_PATTERN)]
