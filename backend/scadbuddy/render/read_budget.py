"""What one read of a library 3MF may spend (#2087).

A small file can name a large one: components that place one object thousands of times,
a mesh placed by each of them, painting carried into every copy. So every read of a
library 3MF (:mod:`scadbuddy.render.objects3mf`: Arrange's objects, a library file's
preview and print checks) spends against four budgets, and is refused past any of them
with an error naming it.

Each budget's default is a setting, ``SCADBUDDY_READ_MAX_<BUDGET>``, which Settings can
change (:func:`budget_of`). One request may override any of them
(:class:`ReadBudgetOverride`, a body's ``read_budget`` or a GET's query parameters), and
neither may go past the budget's ceiling (:data:`CEILINGS`): the ceilings are what the
API pod's memory can hold, which a request cannot argue with.

A budget bounds the work a read does, so a result kept from a read (#1973) answers any
budget, while a refusal answers only a budget no larger on any axis than the one that
read it (:meth:`ReadBudget.covers`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.core.config import (
    DEFAULT_READ_MAX_OBJECTS,
    DEFAULT_READ_MAX_PAINT_DIGITS,
    DEFAULT_READ_MAX_TRIANGLES,
    DEFAULT_READ_MAX_VISITS,
    READ_MAX_OBJECTS_CEILING,
    READ_MAX_PAINT_DIGITS_CEILING,
    READ_MAX_TRIANGLES_CEILING,
    READ_MAX_VISITS_CEILING,
)

if TYPE_CHECKING:
    from scadbuddy.core.settings import Settings

Budget = Literal["max_objects", "max_visits", "max_triangles", "max_paint_digits"]

#: Each budget's ceiling: no setting or override goes past it.
CEILINGS: Final[dict[Budget, int]] = {
    "max_objects": READ_MAX_OBJECTS_CEILING,
    "max_visits": READ_MAX_VISITS_CEILING,
    "max_triangles": READ_MAX_TRIANGLES_CEILING,
    "max_paint_digits": READ_MAX_PAINT_DIGITS_CEILING,
}

#: What a read past each budget found, as its refusal says it.
PAST: Final[dict[Budget, str]] = {
    "max_objects": "places more than {:,} distinct objects",
    "max_visits": "expands, through its components, to more than {:,} objects",
    "max_triangles": "expands to more than {:,} triangles",
    "max_paint_digits": "carries more than {:,} digits of painting",
}


def setting_of(budget: Budget) -> str:
    """The setting that holds ``budget``'s default: ``read_max_triangles``, …"""
    return f"read_{budget}"


class ReadBudget(BaseModel):
    """The budgets one read spends against, each within its ceiling."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_objects: int = Field(default=DEFAULT_READ_MAX_OBJECTS, gt=0, le=READ_MAX_OBJECTS_CEILING)
    max_visits: int = Field(default=DEFAULT_READ_MAX_VISITS, gt=0, le=READ_MAX_VISITS_CEILING)
    max_triangles: int = Field(
        default=DEFAULT_READ_MAX_TRIANGLES, gt=0, le=READ_MAX_TRIANGLES_CEILING
    )
    max_paint_digits: int = Field(
        default=DEFAULT_READ_MAX_PAINT_DIGITS, gt=0, le=READ_MAX_PAINT_DIGITS_CEILING
    )

    def covers(self, other: ReadBudget) -> bool:
        """Whether every budget here is at least ``other``'s: what this budget refused,
        ``other`` would refuse too."""
        return all(getattr(self, name) >= getattr(other, name) for name in CEILINGS)

    def refusal(self, budget: Budget) -> str:
        """Why a read past ``budget`` was refused, and how to raise it."""
        return (
            f"the 3MF {PAST[budget].format(getattr(self, budget))}, past the {budget} read"
            f" budget; raise it under Settings → Projects & files ({env_name(budget)}, at"
            f" most {CEILINGS[budget]:,}) or for one request (its read_budget.{budget},"
            f" or ?{budget}= on a GET)"
        )


class ReadBudgetOverride(BaseModel):
    """One request's budgets, each omitted one the setting's. A value past its ceiling
    is a 422 naming it."""

    model_config = ConfigDict(extra="forbid")

    max_objects: int | None = Field(default=None, gt=0, le=READ_MAX_OBJECTS_CEILING)
    max_visits: int | None = Field(default=None, gt=0, le=READ_MAX_VISITS_CEILING)
    max_triangles: int | None = Field(default=None, gt=0, le=READ_MAX_TRIANGLES_CEILING)
    max_paint_digits: int | None = Field(default=None, gt=0, le=READ_MAX_PAINT_DIGITS_CEILING)

    def over(self, base: ReadBudget) -> ReadBudget:
        """``base`` with every budget this override sets in its place."""
        return base.model_copy(update=self.model_dump(exclude_none=True))


def env_name(budget: Budget) -> str:
    return f"SCADBUDDY_{setting_of(budget).upper()}"


def budget_of(settings: Settings, override: ReadBudgetOverride | None = None) -> ReadBudget:
    """The budget a read runs with: the settings in effect, then ``override``."""
    base = ReadBudget(**{name: getattr(settings, setting_of(name)) for name in CEILINGS})
    return override.over(base) if override is not None else base
