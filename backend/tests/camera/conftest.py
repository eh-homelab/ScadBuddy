from __future__ import annotations

import pytest

from scadbuddy.core.secrets import Kek
from tests.printers.conftest import KEK


@pytest.fixture
def kek() -> Kek:
    return KEK
