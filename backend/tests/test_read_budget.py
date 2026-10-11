"""#2087 — the read budgets: settings, per-request overrides, ceilings."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scadbuddy.core.settings import Settings, check_value
from scadbuddy.render.read_budget import (
    CEILINGS,
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


@pytest.mark.parametrize(("budget", "ceiling"), CEILINGS.items())
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
    base = ReadBudget()
    assert base.covers(base)
    assert base.covers(base.model_copy(update={"max_triangles": 1}))
    assert not base.covers(base.model_copy(update={"max_triangles": base.max_triangles + 1}))


def test_the_defaults_sit_under_their_ceilings() -> None:
    default = ReadBudget()
    assert all(getattr(default, name) <= ceiling for name, ceiling in CEILINGS.items())
