"""Print-host credential keys in a slicer preset's settings (#5298 Task 32).

Here, and not in `slicing/presets.py`, so the Bambuddy client's models strip them when a
preset is parsed (`bambuddy/models.py` `LocalPresetDetail`) without importing the slicer.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

#: Bambu Studio's print-host credential keys (an OctoPrint-style host's API key, password
#: and user), which a user's local machine preset can carry. A key whose name holds one of
#: :data:`_CREDENTIAL_PARTS` goes too, at any depth. No slice reads them: the CLI never
#: talks to a print host, and neither ScadBuddy nor the Rust slicer worker reads any of
#: them (``git grep``, 2026-11-13). ``print_host`` and the host's other fields stay.
#: ScadBuddy strips these keys whenever it parses a preset, and never stores, forwards or
#: writes them into a slicer file. The API strips the kept presets an older build stored
#: at its start (`slicing/held_presets.py` `strip_kept_logged`). One window remains:
#: preset files that older builds put in the content store keep the keys until the
#: content sweep collects them. The print worker, which holds the Bambuddy key, can
#: still read Bambuddy's local presets, including any such keys, in the bytes Bambuddy
#: sends. What protects them is the Bambuddy key's scope.
CREDENTIAL_KEYS = frozenset({"printhost_apikey", "printhost_password", "printhost_user"})
_CREDENTIAL_PARTS = ("apikey", "password", "secret", "token")


def is_credential(key: str) -> bool:
    """Whether ``key`` names a print-host credential (:data:`CREDENTIAL_KEYS`)."""
    lowered = key.lower()
    return lowered in CREDENTIAL_KEYS or any(part in lowered for part in _CREDENTIAL_PARTS)


def strip_credentials[V](document: Mapping[str, V]) -> dict[str, V]:
    """``document`` without its print-host credential keys (:func:`is_credential`), in
    nested objects and in lists too. Order and every other value are kept, so a document
    with none of them dumps to the same JSON."""
    return {key: _stripped(value) for key, value in document.items() if not is_credential(key)}


def _stripped[V](value: V) -> V:
    if isinstance(value, Mapping):
        return cast(V, strip_credentials(cast(Mapping[str, Any], value)))
    if isinstance(value, list):
        return cast(V, [_stripped(item) for item in value])
    return value
