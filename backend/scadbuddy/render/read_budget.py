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
    READ_MAX_VISITS_CEILING,
)

if TYPE_CHECKING:
    from scadbuddy.core.settings import Settings

Budget = Literal["max_objects", "max_visits", "max_triangles", "max_paint_digits"]

#: Each budget's ceiling: no setting or override goes past it. ``None`` is a budget
#: with none, whose 0 is no limit at all (:data:`NO_LIMIT`).
CEILINGS: Final[dict[Budget, int | None]] = {
    "max_objects": READ_MAX_OBJECTS_CEILING,
    "max_visits": READ_MAX_VISITS_CEILING,
    "max_triangles": None,
    "max_paint_digits": READ_MAX_PAINT_DIGITS_CEILING,
}
#: A budget of 0, where it has no ceiling: nothing is counted against it.
NO_LIMIT: Final = 0

#: What a read past each budget found, as its refusal says it.
PAST: Final[dict[Budget, str]] = {
    "max_objects": "places more than {:,} distinct objects",
    "max_visits": "expands, through its components, to more than {:,} objects",
    "max_triangles": "expands to more than {:,} triangles",
    "max_paint_digits": "carries more than {:,} digits of painting",
}


#: What one triangle may cost of a model file's XML, about three times what one takes:
#: a triangle and its share of the vertices measured 78 bytes in ScadBuddy's own Bambu
#: 3MF (`write_bambu_3mf`, an icosphere of 81,920 triangles). A painted one adds its
#: ``paint_color``, which the paint-digit budget allows for on its own.
MODEL_BYTES_PER_TRIANGLE: Final = 256
#: What a model file holds besides its meshes: metadata, resources, build items.
MODEL_BYTES_OVERHEAD: Final = 64 * 1024 * 1024


def setting_of(budget: Budget) -> str:
    """The setting that holds ``budget``'s default: ``read_max_triangles``, …"""
    return f"read_{budget}"


class ReadBudget(BaseModel):
    """The budgets one read spends against, each within its ceiling."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_objects: int = Field(default=DEFAULT_READ_MAX_OBJECTS, gt=0, le=READ_MAX_OBJECTS_CEILING)
    max_visits: int = Field(default=DEFAULT_READ_MAX_VISITS, gt=0, le=READ_MAX_VISITS_CEILING)
    #: 0 is no limit.
    max_triangles: int = Field(default=DEFAULT_READ_MAX_TRIANGLES, ge=0)
    max_paint_digits: int = Field(
        default=DEFAULT_READ_MAX_PAINT_DIGITS, gt=0, le=READ_MAX_PAINT_DIGITS_CEILING
    )

    @classmethod
    def widest(cls) -> ReadBudget:
        """Every budget at its ceiling, or no limit where it has none: covers any."""
        return cls(**{name: NO_LIMIT if top is None else top for name, top in CEILINGS.items()})

    def limit(self, budget: Budget) -> int | None:
        """``budget``'s limit, or ``None`` when it is :data:`NO_LIMIT`."""
        value: int = getattr(self, budget)
        return None if CEILINGS[budget] is None and value == NO_LIMIT else value

    def covers(self, other: ReadBudget) -> bool:
        """Whether every budget here is at least ``other``'s: what this budget refused,
        ``other`` would refuse too. No limit is at least any."""

        def at_least(name: Budget) -> bool:
            mine, theirs = self.limit(name), other.limit(name)
            return mine is None or (theirs is not None and mine >= theirs)

        return all(at_least(name) for name in CEILINGS)

    def model_bytes(self) -> int | None:
        """How many bytes a read's model files may inflate to, all together: a generous
        :data:`MODEL_BYTES_PER_TRIANGLE` for every triangle the budget allows, its paint
        digits, and :data:`MODEL_BYTES_OVERHEAD`. What bounds the parse of a file's XML,
        whose arrays the triangle budget bounds as they are read; the rest of the archive
        (thumbnails, attachments) is never read, so its size counts for nothing. ``None``
        when triangles have no limit: then nothing bounds the parse either."""
        triangles = self.limit("max_triangles")
        if triangles is None:
            return None
        return triangles * MODEL_BYTES_PER_TRIANGLE + self.max_paint_digits + MODEL_BYTES_OVERHEAD

    def refusal(self, budget: Budget, past: str | None = None) -> str:
        """Why a read past ``budget`` was refused, and how to raise it: ``past`` says what
        the file was found to hold, when not the budget's own (:data:`PAST`)."""
        found = past if past is not None else PAST[budget].format(getattr(self, budget))
        if CEILINGS[budget] is None:
            # The operator's limit: a request may lower it but never raise it.
            return (
                f"the 3MF {found}, past the {budget} read budget; raise it under Settings"
                f" → Projects & files ({env_name(budget)}, 0 is no limit)"
            )
        return (
            f"the 3MF {found}, past the {budget} read"
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
    #: 0 is no limit, for this request: honoured only where the setting is no limit too
    #: (:meth:`over`).
    max_triangles: int | None = Field(default=None, ge=0)
    max_paint_digits: int | None = Field(default=None, gt=0, le=READ_MAX_PAINT_DIGITS_CEILING)

    def over(self, base: ReadBudget) -> ReadBudget:
        """``base`` with every budget this override sets in its place. A budget with no
        ceiling (triangles) is the operator's to bound: where the setting is a limit, a
        request may only lower it, never raise it or remove it (0); where the setting is
        no limit, a request may set any."""
        update = self.model_dump(exclude_none=True)
        for name, ceiling in CEILINGS.items():
            limit = base.limit(name)
            if name in update and ceiling is None and limit is not None:
                value = update[name]
                update[name] = limit if value == NO_LIMIT else min(value, limit)
        return base.model_copy(update=update)


def env_name(budget: Budget) -> str:
    return f"SCADBUDDY_{setting_of(budget).upper()}"


def budget_of(settings: Settings, override: ReadBudgetOverride | None = None) -> ReadBudget:
    """The budget a read runs with: the settings in effect, then ``override``."""
    base = ReadBudget(**{name: getattr(settings, setting_of(name)) for name in CEILINGS})
    return override.over(base) if override is not None else base
