"""Which print runs take the hot path without Bambuddy (#3818 tier A, plan
2026-10-18-hot-path-without-bambuddy D2).

There is no switch of its own: the stored backends are the user's control. A run is
native when the slicer and the library are ScadBuddy's own, the store is one a native run
can read its copy from (``s3``, or ``bambuddy``, whose calls are the store's and outside
the print path), and the printer row's resolved path (`print_run.run_path`, plan
2026-10-16 Task 8) is the LAN. Decided per run, from the settings as they are then: never
once at a process's start."""

from __future__ import annotations

from typing import Literal

from scadbuddy.library.settings_store import StoredSettings

#: The stores a native run reads its copy from (D2).
NATIVE_STORES = ("s3", "bambuddy")


def native_backends(settings: StoredSettings) -> bool:
    """Whether the stored backends put a LAN-path run on the hot path (D2), whatever its
    printer: the frontend's `nativeBackends` is the same rule."""
    return (
        settings.slicer_backend == "native"
        and settings.library_backend == "native"
        and settings.store_backend in NATIVE_STORES
    )


def hot_path_native(settings: StoredSettings, path: Literal["bambuddy", "native"]) -> bool:
    """Whether a run whose printer row resolved to ``path`` is native (D2)."""
    return path == "native" and native_backends(settings)
