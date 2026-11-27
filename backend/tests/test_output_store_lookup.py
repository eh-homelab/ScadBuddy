"""#2755, #7257: a listing's details read each output's row; no read searches the share."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from scadbuddy.api.outputs import detail
from scadbuddy.library.outputs import OutputNotFoundError
from tests.conftest import PgPool
from tests.support.arrange import copied_output, saved_output


def _count_globs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    patterns: list[str] = []
    real = Path.glob

    def counted(self: Path, pattern: str, *args: Any, **kwargs: Any) -> Any:
        patterns.append(pattern)
        return real(self, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "glob", counted)
    return patterns


async def test_a_listing_and_its_details_glob_nothing(
    tmp_path: Path, pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every read of an output by id used to glob every model's directory (~90 ms each
    on the production NAS); the rows answer now (#7257)."""
    store, meta, _ = await saved_output(tmp_path, pg_pool)
    for n in range(4):
        copied_output(store, meta, slug="demo", output_id=f"{n:032x}")
    patterns = _count_globs(monkeypatch)

    metas = store.list_for("demo")
    details = [detail(store, m, []) for m in metas]

    assert len(details) == 5
    assert patterns == []


async def test_a_deleted_output_is_not_found(tmp_path: Path, pg_pool: PgPool) -> None:
    store, meta, _ = await saved_output(tmp_path, pg_pool)
    assert store.name_and_slug(meta.id) == (meta.name, meta.slug)

    store.delete(meta.id)

    with pytest.raises(OutputNotFoundError):
        store.get(meta.id)
    with pytest.raises(OutputNotFoundError):
        store.name_and_slug(meta.id)
