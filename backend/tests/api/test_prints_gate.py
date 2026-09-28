"""The print-media gate with no database (#522 review): nothing is linked, so an
archive is refused with a 404, never a 500."""

from __future__ import annotations

import pytest

from scadbuddy.api.prints import require_linked_archive
from scadbuddy.bambuddy.print_links import PrintLinkStore
from scadbuddy.core.problems import ApiError


async def test_without_a_database_no_archive_is_served() -> None:
    with pytest.raises(ApiError) as raised:
        await require_linked_archive(35, PrintLinkStore(None))
    assert raised.value.status == 404
