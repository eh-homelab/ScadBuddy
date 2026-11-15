"""What a print is of (#1749, #1750): an output ScadBuddy rendered, a file already in
Bambuddy's library, or (#4698) a model, for a print ScadBuddy did not start that a person
imported onto that model. Every record of a print is keyed by its subject's ``key``
(``output:<id>``, ``library:<file id>`` or ``model:<slug>``), so the kinds share one
shape. An output and a library file differ only in how the 3MF to slice is obtained
(``print_source``); a model subject is never printed by a run of ScadBuddy's.
"""

from __future__ import annotations

import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

from scadbuddy.library.slugs import MODEL_ID_PATTERN

SubjectKind = Literal["output", "library", "model"]

_KEY = re.compile(r"(?P<kind>output|library|model):(?P<id>.+)")
_MODEL_ID = re.compile(MODEL_ID_PATTERN)


def _valid_id(kind: str, value: str) -> bool:
    if kind == "library":
        return value.isdecimal()
    if kind == "model":
        return _MODEL_ID.fullmatch(value) is not None
    return bool(value)


class PrintSubject(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: SubjectKind
    #: The output's id, or the library file's id as a string.
    id: str

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not self.id or not _valid_id(self.kind, self.id):
            raise ValueError(f"{self.id!r} is not a {self.kind} print subject's id")
        return self

    @classmethod
    def output(cls, output_id: str) -> PrintSubject:
        return cls(kind="output", id=output_id)

    @classmethod
    def library(cls, file_id: int) -> PrintSubject:
        return cls(kind="library", id=str(file_id))

    @classmethod
    def model(cls, slug: str) -> PrintSubject:
        """A print imported onto the model ``slug`` (#4698): no output of it printed it."""
        return cls(kind="model", id=slug)

    @classmethod
    def parse(cls, key: str) -> PrintSubject:
        """The subject a :attr:`key` names; ``ValueError`` for anything else."""
        match = _KEY.fullmatch(key)
        if match is None or not _valid_id(match["kind"], match["id"]):
            raise ValueError(f"{key!r} is not a print subject")
        return cls(kind=match["kind"], id=match["id"])  # type: ignore[arg-type]

    @classmethod
    def from_run_subject(cls, value: str) -> PrintSubject:
        """The subject a :attr:`run_subject` names."""
        return cls.parse(value) if value.startswith(("library:", "model:")) else cls.output(value)

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.id}"

    @property
    def run_subject(self) -> str:
        """What a run was keyed and announced under before #1750: an output's bare id,
        a library file's ``library:<file id>``. A run's idempotency key, workflow id,
        ``print:`` event topic and ``PrintRun.output_id`` keep it, so a retry across the
        upgrade finds its run and clients read what they always did."""
        return self.id if self.kind == "output" else self.key

    @property
    def output_id(self) -> str | None:
        return self.id if self.kind == "output" else None

    @property
    def file_id(self) -> int | None:
        return int(self.id) if self.kind == "library" else None

    @property
    def slug(self) -> str | None:
        """The model a model subject names; None for an output's or a library file's."""
        return self.id if self.kind == "model" else None


def library_slug(subject: PrintSubject) -> str:
    """The ``slug`` a library file's print run and its ``print.*`` events carry, where an
    output's carry its model's: ``library-<file id>`` (#742, #1751)."""
    return f"library-{subject.id}"
