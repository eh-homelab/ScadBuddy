"""#8042 (#7905): `clean_tag_uid` against the shared vectors,
`test-fixtures/filament/tag-uid-vectors.json`, which the browser's copy
(`frontend/src/lib/tagUid.ts`, `tagUid.test.ts`) is tested against too."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scadbuddy.spools.models import MIFARE_UID_SUFFIX, clean_tag_uid

VECTORS = Path(__file__).resolve().parents[3] / "test-fixtures/filament/tag-uid-vectors.json"
_DATA: dict[str, Any] = json.loads(VECTORS.read_text())


def test_clean_tag_uid_matches_the_vectors() -> None:
    assert _DATA["suffix"] == MIFARE_UID_SUFFIX
    for case in _DATA["cases"]:
        assert clean_tag_uid(case["in"]) == case["out"], case
