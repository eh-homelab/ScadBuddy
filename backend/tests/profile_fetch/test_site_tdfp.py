"""#7025: the 3DFP driver's search and create, against the local replica in `site/`. The real
site is never reached, and the create never runs against it."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from scadbuddy.profile_fetch import site
from scadbuddy.profile_fetch.site import ThreeDfpSite
from scadbuddy.profile_fetch.steps import SiteError
from scadbuddy.profile_fetch.wire import CreateAsk, SearchAsk
from tests.profile_fetch.site.server import Replica, ReplicaServer
from tests.profile_fetch.test_site import EMAIL, PASSWORD, _site

pytestmark = pytest.mark.requires_chromium


@pytest.fixture(autouse=True)
def _quick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(site, "PACE", 0.05)
    monkeypatch.setattr(site, "BOT_CHECK_SECONDS", 1.0)
    monkeypatch.setattr(site, "STEP_TIMEOUT_MS", 3_000)
    monkeypatch.setattr(site, "LOGIN_WAIT_MS", 1_500)
    monkeypatch.setattr(site, "DEFAULTS_WAIT_MS", 1_000)
    monkeypatch.setattr(site, "SAVE_WAIT_MS", 2_000)


@pytest.fixture
def replica() -> Iterator[ReplicaServer]:
    server = ReplicaServer(Replica(email=EMAIL, password=PASSWORD))
    yield server
    server.close()


@pytest.fixture
async def browser(replica: ReplicaServer, tmp_path: Path) -> AsyncIterator[ThreeDfpSite]:
    async with _site(replica, tmp_path) as driver:
        await driver.login()
        yield driver


def _ask(**values: object) -> CreateAsk:
    fields: dict[str, object] = {
        "brand": "SUNLU",
        "material": "PLA+/Pro",
        "material_type": "Silk",
        "colour": "Copper",
        "hexes": ["#B87333", "#8C5A2B"],
        "nominal_weight_g": 1000,
        "mpn": "SL-SILK-CU",
        "gtin": "06975373321234",
        "td": 2.5,
    }
    fields.update(values)
    return CreateAsk.model_validate(fields)


async def test_the_search_reads_the_results_table(
    browser: ThreeDfpSite, replica: ReplicaServer
) -> None:
    answer = await browser.search(
        SearchAsk(brand="SUNLU", material="PLA+/Pro", material_type="Silk", colour="Bronze")
    )
    assert replica.site.searches == ["SUNLU Silk Bronze"]
    assert answer.query == "SUNLU Silk Bronze"
    # The page lists each filament as a card and a table row: each is read once, the
    # material's matches first.
    assert [c.tdfp_id for c in answer.candidates] == [38537, 1708, 8916]
    first = answer.candidates[0]
    assert (first.brand, first.material, first.material_type) == ("SUNLU", "PLA+/Pro", "Silk")
    assert (first.name, first.hexes, first.nominal_weight_g) == ("BRONZE (250g)", ["#779E7C"], 250)


async def test_the_search_finds_its_cells_by_their_header(
    browser: ThreeDfpSite, replica: ReplicaServer
) -> None:
    """A column hidden with the page's "Columns" control moves the others."""
    replica.site.columns = ["Color", "Brand", "Type", "Material", "RGB"]
    answer = await browser.search(SearchAsk(brand="SUNLU", colour="Bronze"))
    found = {c.tdfp_id: (c.brand, c.material, c.material_type) for c in answer.candidates}
    assert found[8916] == ("SUNLU", "PLA", "Silk")


async def test_a_search_with_no_rows_finds_nothing(
    browser: ThreeDfpSite, replica: ReplicaServer
) -> None:
    answer = await browser.search(SearchAsk(brand="DEEPLEE"))
    assert answer.candidates == []


async def test_the_create_fills_the_form_and_answers_the_new_id(
    browser: ThreeDfpSite, replica: ReplicaServer
) -> None:
    answer = await browser.create(_ask())
    assert answer.status == "created"
    assert answer.tdfp_id == 40000
    [created] = replica.site.created
    assert created == {
        "id": 40000,
        "brand": "SUNLU",
        "material": "PLA+/Pro",
        "material_type": "Silk",
        "color": "Copper",
        "rgbText-0": "#B87333",
        "rgbText-1": "#8C5A2B",
        "nominal_weight": "1000",
        "sku-0": "SL-SILK-CU",
        "gtin": "06975373321234",
        "asin": "",
    }
    # The form has no TD field: the key is answered, never guessed into another field.
    assert answer.sent == ["mpn", "gtin"]
    assert answer.not_sent == ["td"]


async def test_the_brand_is_typed_when_its_select_lacks_it(
    browser: ThreeDfpSite, replica: ReplicaServer
) -> None:
    replica.site.brand_select = False
    answer = await browser.create(_ask())
    assert answer.status == "created"
    assert replica.site.created[0]["brand"] == "SUNLU"


async def test_a_combination_with_no_defaults_is_refused(
    browser: ThreeDfpSite, replica: ReplicaServer
) -> None:
    with pytest.raises(SiteError) as caught:
        await browser.create(_ask(material_type="Kevlar"))
    assert caught.value.kind == "no_defaults"
    assert replica.site.created == []


async def test_a_type_the_form_lacks_is_option_missing(
    browser: ThreeDfpSite, replica: ReplicaServer
) -> None:
    with pytest.raises(SiteError) as caught:
        await browser.create(_ask(material_type="Galaxy"))
    assert caught.value.kind == "option_missing"
    assert "Galaxy" in str(caught.value)
    assert replica.site.created == []


async def test_a_likely_duplicate_stops_before_the_save(
    browser: ThreeDfpSite, replica: ReplicaServer
) -> None:
    replica.site.duplicates = [
        {
            "id": 1708,
            "brand": "SUNLU",
            "material": "PLA+/Pro",
            "type": "Silk",
            "name": "Copper",
            "hexes": ["#B87334"],
        }
    ]
    answer = await browser.create(_ask())
    assert answer.status == "duplicate"
    assert answer.tdfp_id is None
    assert [d.tdfp_id for d in answer.duplicates] == [1708]
    assert answer.duplicates[0].material == "PLA+/Pro"
    assert [d.tdfp_id for d in answer.likely] == [1708]
    assert "Avoid creating duplicates" in answer.duplicate_text
    assert replica.site.created == []


async def test_a_duplicate_the_person_cleared_is_saved(
    browser: ThreeDfpSite, replica: ReplicaServer
) -> None:
    replica.site.duplicates = [
        {
            "id": 1708,
            "brand": "SUNLU",
            "material": "PLA+/Pro",
            "type": "Silk",
            "name": "Copper",
            "hexes": ["#B87334"],
        }
    ]
    answer = await browser.create(_ask(not_duplicates=[1708]))
    assert answer.status == "created"
    assert [d.tdfp_id for d in answer.duplicates] == [1708]
    assert answer.likely == []


async def test_a_save_that_does_not_land_is_not_created(
    browser: ThreeDfpSite, replica: ReplicaServer
) -> None:
    """The filament may exist: the kind is ``not_created``, which no step retries."""
    replica.site.save_lands = False
    with pytest.raises(SiteError) as caught:
        await browser.create(_ask())
    assert caught.value.kind == "not_created"
