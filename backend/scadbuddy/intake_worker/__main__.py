"""``python -m scadbuddy.intake_worker``: the intake worker (`worker.py`)."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Sequence

from scadbuddy.core.logging import configure_logging
from scadbuddy.intake_worker.settings import IntakeRefusal, IntakeSettings, check_environment
from scadbuddy.intake_worker.worker import install_stop, run

logger = logging.getLogger(__name__)


async def _main(settings: IntakeSettings) -> None:
    stop = asyncio.Event()
    install_stop(stop)
    await run(settings, stop=stop)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        check_environment(os.environ)
    except IntakeRefusal as refusal:
        configure_logging()
        for reason in refusal.reasons:
            logger.error(reason)
        return 2
    settings = IntakeSettings()  # type: ignore[call-arg]  # read from the environment
    configure_logging(settings.log_level)
    asyncio.run(_main(settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
