"""The CRUD warm-up's status boundary (#6682, #6686): a server error fails the helper;
a refusal still built the routes, so it passes. `crud_support.py`
`threaded_print_crud(store, caller="projects")` relies on that: it warms the print
routes with another caller's credential, for the
`test_another_callers_credential_is_refused` tests."""

from __future__ import annotations

import httpx
import pytest

from tests.crud.crud_support import _warmed


@pytest.mark.parametrize("status", [200, 401, 403, 404, 499])
def test_an_answer_below_500_passes(status: int) -> None:
    _warmed(httpx.Response(status))


@pytest.mark.parametrize("status", [500, 503])
def test_a_server_error_fails_and_names_its_status(status: int) -> None:
    with pytest.raises(AssertionError, match=f"got {status}"):
        _warmed(httpx.Response(status, text="boom"))
