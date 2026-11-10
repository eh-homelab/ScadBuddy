"""The edge's sign-out retries (#5283, review of #5953): a sign-out the CRUD side did not
answer is tried again in the background, a bounded number of times, from a bounded queue."""

from __future__ import annotations

import asyncio
import logging

import pytest

from scadbuddy.crud.client import CrudUnavailableError
from scadbuddy.storefront.sign_out import SIGN_OUT_RETRY_DELAYS, SignOutRetries

SESSION = "t" * 43


class Crud:
    """`StorefrontCrud.account_sign_out`, answering after ``fails`` refusals to answer."""

    def __init__(self, fails: int) -> None:
        self.fails = fails
        self.calls: list[str] = []

    async def account_sign_out(self, *, visitor_id: str, client: str, session: str) -> None:
        self.calls.append(session)
        if len(self.calls) <= self.fails:
            raise CrudUnavailableError("down", "unreached")


async def settled(retries: SignOutRetries) -> None:
    for _ in range(200):
        if retries.pending == 0:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the retries never settled")


def test_the_retries_take_about_ten_minutes_over_five_tries() -> None:
    assert len(SIGN_OUT_RETRY_DELAYS) == 5
    assert 9 * 60 <= sum(SIGN_OUT_RETRY_DELAYS) <= 11 * 60
    assert list(SIGN_OUT_RETRY_DELAYS) == sorted(SIGN_OUT_RETRY_DELAYS)


async def test_a_retry_ends_the_session_once_the_crud_side_answers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    crud = Crud(fails=2)
    retries = SignOutRetries(crud.account_sign_out, delays=(0, 0, 0, 0, 0))
    with caplog.at_level(logging.INFO, logger="scadbuddy.storefront.sign_out"):
        assert retries.add(visitor_id="v", client="c", session=SESSION)
        await settled(retries)
    assert crud.calls == [SESSION] * 3
    messages = [record.getMessage() for record in caplog.records]
    assert sum("no answer" in m for m in messages) == 2
    assert any("ended the session" in m for m in messages)


async def test_the_retries_give_up_after_the_last_try(caplog: pytest.LogCaptureFixture) -> None:
    crud = Crud(fails=99)
    retries = SignOutRetries(crud.account_sign_out, delays=(0, 0, 0))
    with caplog.at_level(logging.INFO, logger="scadbuddy.storefront.sign_out"):
        retries.add(visitor_id="v", client="c", session=SESSION)
        await settled(retries)
    assert len(crud.calls) == 3
    assert any("gave up" in record.getMessage() for record in caplog.records)


async def test_an_unexpected_failure_ends_the_retries_with_a_log_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Review of #5953: every give-up is logged, not only the expected ones."""
    calls = 0

    async def broken(*, visitor_id: str, client: str, session: str) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    retries = SignOutRetries(broken, delays=(0, 0, 0))
    with caplog.at_level(logging.ERROR, logger="scadbuddy.storefront.sign_out"):
        retries.add(visitor_id="v", client="c", session=SESSION)
        await settled(retries)
    assert calls == 1
    (record,) = [r for r in caplog.records if r.exc_info]
    assert "stopping" in record.getMessage()


async def test_the_queue_is_bounded(caplog: pytest.LogCaptureFixture) -> None:
    crud = Crud(fails=99)
    retries = SignOutRetries(crud.account_sign_out, delays=(60,), limit=2)
    with caplog.at_level(logging.WARNING, logger="scadbuddy.storefront.sign_out"):
        assert retries.add(visitor_id="v", client="c", session="a" * 43)
        assert retries.add(visitor_id="v", client="c", session="b" * 43)
        assert not retries.add(visitor_id="v", client="c", session="c" * 43)
    assert retries.pending == 2
    assert any("full" in record.getMessage() for record in caplog.records)
    await retries.aclose()
    assert retries.pending == 0
    assert crud.calls == []
