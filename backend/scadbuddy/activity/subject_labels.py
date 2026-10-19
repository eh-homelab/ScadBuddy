"""The names `/activity/facets` found for its subjects, kept a while (#4002): the
Activity page re-reads the facets every few seconds, and each read would otherwise ask
the catalogue and up to `LABELLED_SUBJECTS` outputs again."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Mapping

#: How long a subject's name (or that it has none) is kept: a renamed model or output
#: shows its new name on its chip within this.
LABELS_TTL = 30.0


class SubjectLabels:
    """Subject → its name, or ``None`` when ScadBuddy holds none, each kept for
    ``ttl`` seconds. One per app, on the activity component."""

    def __init__(self, ttl: float = LABELS_TTL, clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, str | None]] = {}

    def known(self, values: Iterable[str]) -> tuple[dict[str, str], set[str]]:
        """The names kept for ``values``, and the values with nothing kept."""
        now = self._clock()
        labels: dict[str, str] = {}
        missing: set[str] = set()
        with self._lock:
            for value in values:
                entry = self._entries.get(value)
                if entry is None or entry[0] <= now:
                    missing.add(value)
                elif entry[1] is not None:
                    labels[value] = entry[1]
        return labels, missing

    def keep(self, settled: Iterable[str], labels: Mapping[str, str]) -> None:
        """Keep each of ``settled``'s name from ``labels``, or that it has none. Expired
        entries go, so the map holds only the subjects read in the last ``ttl``."""
        now = self._clock()
        expires = now + self._ttl
        with self._lock:
            self._entries = {k: e for k, e in self._entries.items() if e[0] > now}
            for value in settled:
                self._entries[value] = (expires, labels.get(value))
