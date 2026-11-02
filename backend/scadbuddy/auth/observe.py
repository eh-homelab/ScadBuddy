"""The would-refuse counter and its rate-limited log (#4975 phase 3, spec 2026-11-01 §9.1)."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from scadbuddy.auth.routes import Reason, RouteClass
from scadbuddy.core.metrics import Metrics

logger = logging.getLogger(__name__)

#: One log line per class and reason this often; the counter has every one.
LOG_INTERVAL = 60.0


class WouldRefuse:
    def __init__(self, metrics: Metrics, clock: Callable[[], float] = time.monotonic) -> None:
        self._metrics = metrics
        self._clock = clock
        self._lock = threading.Lock()
        self._logged: dict[tuple[RouteClass, Reason], float] = {}

    def note(self, route_cls: RouteClass, reason: Reason, path: str) -> None:
        self._metrics.auth_would_refuse.labels(route_cls, reason).inc()
        now = self._clock()
        with self._lock:
            last = self._logged.get((route_cls, reason))
            if last is not None and now - last < LOG_INTERVAL:
                return
            self._logged[(route_cls, reason)] = now
        # The route's template, never the request's path: it can carry ids and names.
        logger.warning(
            "enforce would refuse a %s request (%s); served, as SCADBUDDY_AUTH_MODE=observe",
            route_cls,
            reason,
            extra={"route": path},
        )
