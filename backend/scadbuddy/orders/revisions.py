"""The bodies of an item's design revisions (#5280, spec 2026-11-02 §4.3).

The writer is ``Lifecycle._revise``: the one caller of ``OrderStore.set_design``, so a
revision is the only way an item's design changes past its insert (§4.1)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RevisionInput(BaseModel):
    """A new revision of the item's current design: the shop's note and, optionally,
    the slots' filaments (``{slot: filament_id | None}``)."""

    model_config = ConfigDict(extra="forbid")

    note: str = Field(default="", max_length=4000)
    filaments: dict[int, str | None] | None = None


class RevisionAnswerInput(BaseModel):
    """The customer's answer to the newest sent revision (`superseded` is never theirs)."""

    model_config = ConfigDict(extra="forbid")

    answer: Literal["accepted", "changes_requested"]
    note: str = Field(default="", max_length=4000)
