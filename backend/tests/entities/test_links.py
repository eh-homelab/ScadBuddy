"""An import's links (#6950, spec 6.3): `EntityLinkStore.add_from_source` keeps a web URL
under its own source and skips one a person already kept or one that is not a web page."""

from __future__ import annotations

import pytest

from scadbuddy.entities.links import EntityLinkStore, LinkInput
from scadbuddy.entities.registry import EntityStore
from tests.conftest import PgPool
from tests.entities.test_sweep import BY, _spool

pytestmark = pytest.mark.requires_postgres


def test_an_import_skips_a_link_that_is_not_a_web_page(pg_pool: PgPool) -> None:
    entities = EntityStore(pg_pool)
    links = EntityLinkStore(pg_pool, entities)
    spool = _spool(pg_pool)
    with pg_pool.connection() as conn:
        for url in ("javascript:alert(1)", "ftp://x", "http://"):
            assert links.add_from_source(conn, spool, LinkInput(url=url), "import", BY) is None
        assert entities.find(conn, spool) is None

        kept = links.add_from_source(
            conn, spool, LinkInput(url="https://example.com/a"), "import", BY
        )
    assert kept is not None and kept.source == "import"
    assert [link.url for link in links.list(spool)] == ["https://example.com/a"]


def test_an_import_skips_a_link_a_person_kept(pg_pool: PgPool) -> None:
    entities = EntityStore(pg_pool)
    links = EntityLinkStore(pg_pool, entities)
    spool = _spool(pg_pool)
    links.add(spool, LinkInput(url="https://example.com/a"), BY)
    with pg_pool.connection() as conn:
        assert (
            links.add_from_source(conn, spool, LinkInput(url="https://example.com/a"), "import", BY)
            is None
        )
    assert [link.source for link in links.list(spool)] == ["manual"]
