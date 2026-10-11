"""#2087 — the read budgets: settings, per-request overrides, ceilings."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scadbuddy.core.settings import Settings, check_value
from scadbuddy.render.read_budget import (
    CEILINGS,
    NO_LIMIT,
    ReadBudget,
    ReadBudgetOverride,
    budget_of,
    env_name,
)
from tests.conftest import UNUSED_DATABASE_URL, UNUSED_TEMPORAL_ADDRESS


def _settings(**update: int) -> Settings:
    return Settings(
        database_url=UNUSED_DATABASE_URL,
        temporal_address=UNUSED_TEMPORAL_ADDRESS,
        **update,  # type: ignore[arg-type]
    )


def test_the_settings_are_the_budget() -> None:
    assert budget_of(_settings()) == ReadBudget()
    assert budget_of(_settings(read_max_triangles=7)).max_triangles == 7


def test_an_override_replaces_only_what_it_names() -> None:
    budget = budget_of(
        _settings(read_max_triangles=7, read_max_visits=9),
        ReadBudgetOverride(max_triangles=11),
    )
    assert (budget.max_triangles, budget.max_visits) == (11, 9)
    assert budget.max_objects == ReadBudget().max_objects


@pytest.mark.parametrize(
    ("budget", "ceiling"), [(name, top) for name, top in CEILINGS.items() if top is not None]
)
def test_nothing_goes_past_a_ceiling(budget: str, ceiling: int) -> None:
    assert ReadBudgetOverride.model_validate({budget: ceiling})
    with pytest.raises(ValidationError):
        ReadBudgetOverride.model_validate({budget: ceiling + 1})
    with pytest.raises(ValidationError):
        ReadBudgetOverride.model_validate({budget: 0})
    with pytest.raises(ValueError, match=env_name(budget)):  # type: ignore[arg-type]
        check_value(f"read_{budget}", ceiling + 1)
    assert check_value(f"read_{budget}", ceiling) == ceiling


def test_a_budget_covers_one_no_larger_on_any_axis() -> None:
    base = ReadBudget(max_triangles=100)
    assert base.covers(base)
    assert base.covers(base.model_copy(update={"max_triangles": 1}))
    assert not base.covers(base.model_copy(update={"max_triangles": 101}))
    # No limit (0) is at least any limit, and no limit is past every one.
    unlimited = base.model_copy(update={"max_triangles": NO_LIMIT})
    assert unlimited.covers(base) and not base.covers(unlimited)
    assert ReadBudget.widest().covers(unlimited)


def test_triangles_default_to_no_limit_with_no_ceiling() -> None:
    assert ReadBudget().max_triangles == NO_LIMIT
    assert ReadBudget().limit("max_triangles") is None
    assert ReadBudget().model_bytes() is None
    assert ReadBudgetOverride(max_triangles=10**12).max_triangles == 10**12
    assert check_value("read_max_triangles", 0) == 0
    assert "0 is no limit" in ReadBudget(max_triangles=5).refusal("max_triangles")


def test_the_defaults_sit_under_their_ceilings() -> None:
    default = ReadBudget()
    assert all(
        ceiling is None or getattr(default, name) <= ceiling for name, ceiling in CEILINGS.items()
    )
