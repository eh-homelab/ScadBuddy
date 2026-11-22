"""#7025: the worker's search and create activities against a fake site: the site's answer,
or its failure as `site_failure` makes it (its kind, retryable or not), the browser closed
after a crash or a timeout."""

from __future__ import annotations

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from scadbuddy.profile_fetch.activities import SiteHolder, TdfpActivities
from scadbuddy.profile_fetch.steps import SiteError
from scadbuddy.profile_fetch.wire import CreateAsk, SearchAsk
from tests.profile_fetch.test_activities import SEARCH_ANSWER, FakeSite

ASK = CreateAsk(
    brand="SUNLU", material="PLA+/Pro", material_type="Silk", colour="Copper", hexes=["#B87333"]
)


async def test_the_search_answers_the_sites_candidates() -> None:
    site = FakeSite()
    activities = TdfpActivities(SiteHolder(lambda: site))
    answer = await ActivityEnvironment().run(activities.search, SearchAsk(brand="SUNLU"))
    assert answer == SEARCH_ANSWER
    assert site.logins == 1


async def test_the_create_answers_the_new_id() -> None:
    site = FakeSite()
    activities = TdfpActivities(SiteHolder(lambda: site))
    answer = await ActivityEnvironment().run(activities.create, ASK)
    assert answer.tdfp_id == 40000
    assert site.creates == [ASK]


@pytest.mark.parametrize(
    ("kind", "retryable", "dropped"),
    [
        ("no_defaults", False, False),
        ("not_created", False, False),
        ("option_missing", False, False),
        ("timeout", True, True),
        ("browser", True, True),
    ],
)
async def test_a_site_failure_keeps_its_kind(kind: str, retryable: bool, dropped: bool) -> None:
    site = FakeSite()
    site.tdfp_error = SiteError(kind)  # type: ignore[arg-type]
    activities = TdfpActivities(SiteHolder(lambda: site))
    with pytest.raises(ApplicationError) as caught:
        await ActivityEnvironment().run(activities.create, ASK)
    assert caught.value.type == kind
    assert caught.value.non_retryable is not retryable
    assert caught.value.__cause__ is None
    assert site.closed == (1 if dropped else 0)


async def test_an_unexpected_error_is_a_browser_failure_of_fixed_text() -> None:
    site = FakeSite()
    site.tdfp_error = RuntimeError("SCADBUDDY_TDFP_PASSWORD=hunter2")
    activities = TdfpActivities(SiteHolder(lambda: site))
    with pytest.raises(ApplicationError) as caught:
        await ActivityEnvironment().run(activities.search, SearchAsk(brand="SUNLU"))
    assert caught.value.type == "browser"
    assert "hunter2" not in str(caught.value)
