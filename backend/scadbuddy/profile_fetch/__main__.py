"""``python -m scadbuddy.profile_fetch``: the profile-fetch worker (`worker.py`). The browser
service is ``python -m scadbuddy.profile_fetch.browser`` (`browser.py`, #6780)."""

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
)
from scadbuddy.profile_fetch.worker import install_stop, run

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
    # Before the proof and the RPC token are read: no process of this user may ptrace this
    # one or read its memory, where the service credential lives (`hardening.py`).
    forbid_dumping()
    try:
        settings = ProfileFetchSettings()  # type: ignore[call-arg]  # read from the environment
    except ValidationError as invalid:
        configure_logging()
        # Each refused setting by its name and why, never the value.
        for error in invalid.errors(include_input=False, include_url=False):
            name = ".".join(str(part) for part in error["loc"]) or "settings"
            logger.error("setting %s: %s", name, error["msg"])
        return 2
    configure_logging(settings.log_level)
    asyncio.run(_main(settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
