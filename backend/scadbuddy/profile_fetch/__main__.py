"""``python -m scadbuddy.profile_fetch``: the profile-fetch worker (`worker.py`)."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Sequence

from pydantic import ValidationError

from scadbuddy.core.logging import configure_logging
from scadbuddy.intake_worker.hardening import forbid_dumping
from scadbuddy.profile_fetch.settings import (
    ProfileFetchRefusal,
    ProfileFetchSettings,
    check_environment,
    forget_login,
)
from scadbuddy.profile_fetch.worker import StartRefusedError, install_stop, run

logger = logging.getLogger(__name__)


async def _main(settings: ProfileFetchSettings) -> None:
    stop = asyncio.Event()
    install_stop(stop)
    await run(settings, stop=stop)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        check_environment(os.environ)
    except ProfileFetchRefusal as refusal:
        configure_logging()
        for reason in refusal.reasons:
            logger.error(reason)
        return 2
    # Before the login is read: no process of this user may ptrace this one or read its
    # memory, where the 3DFP login and the service credential live (`hardening.py`).
    forbid_dumping()
    try:
        settings = ProfileFetchSettings()  # type: ignore[call-arg]  # read from the environment
    except ValidationError as invalid:
        forget_login(os.environ)
        configure_logging()
        # Each refused setting by its name and why, never the value: pydantic's own text
        # quotes the input, which could be the login.
        for error in invalid.errors(include_input=False, include_url=False):
            name = ".".join(str(part) for part in error["loc"]) or "settings"
            logger.error("setting %s: %s", name, error["msg"])
        return 2
    # The settings hold the login now: no child process may inherit it.
    forget_login(os.environ)
    configure_logging(settings.log_level)
    try:
        asyncio.run(_main(settings))
    except StartRefusedError as refused:
        logger.error("%s", refused)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
