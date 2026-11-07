"""``python -m scadbuddy.storefront``: serves the edge on port 8083."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Sequence

import uvicorn

from scadbuddy.core.logging import configure_logging
from scadbuddy.storefront.app import create_app
from scadbuddy.storefront.settings import (
    STOREFRONT_PORT,
    EdgeRefusal,
    StorefrontSettings,
    check_environment,
)

logger = logging.getLogger(__name__)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        check_environment(os.environ)
    except EdgeRefusal as refusal:
        configure_logging()
        for reason in refusal.reasons:
            logger.error(reason)
        return 2
    settings = StorefrontSettings()  # type: ignore[call-arg]  # read from the environment
    configure_logging(settings.log_level)
    # `proxy_headers=False`: the edge reads `X-Forwarded-For` itself, and only from a peer
    # in `SCADBUDDY_TRUSTED_PROXIES` (`core/proxies.py`).
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",
        port=STOREFRONT_PORT,
        proxy_headers=False,
        log_config=None,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
