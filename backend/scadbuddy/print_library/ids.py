"""Native print library ids (plan 2026-10-17-native-print-library, Ruling 1)."""

from __future__ import annotations

#: The identity columns of `library_folders` and `library_files` start here. Bambuddy's
#: SQLite ids are far below it, so a mount's cache (keyed by API host and file id) never
#: serves one backend's bytes for the other's file. `agent/src/sessions/touched.ts` copies it
#: (`agent/test/touched.test.ts` reads this line).
NATIVE_ID_BASE = 1_000_000_000


def is_native_id(id: int) -> bool:
    return id >= NATIVE_ID_BASE
