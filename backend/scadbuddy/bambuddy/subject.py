"""What a print is of (#1749, #1750): an output ScadBuddy rendered, or a file already in
Bambuddy's library. Every record of a print is keyed by its subject's ``key``
(``output:<id>`` or ``library:<file id>``), so the two kinds share one shape; the only
difference between them is how the 3MF to slice is obtained (``print_source``).
"""

from __future__ import annotations

import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

SubjectKind = Literal["output", "library"]

_KEY = re.compile(r"(?P<kind>output|library):(?P<id>.+)")


class PrintSubject(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: SubjectKind
    #: The output's id, or the library file's id as a string.
    id: str

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not self.id or (self.kind == "library" and not self.id.isdecimal()):
            raise ValueError(f"{self.id!r} is not a {self.kind} print subject's id")
        return self

    @classmethod
    def output(cls, output_id: str) -> PrintSubject:
        return cls(kind="output", id=output_id)

    @classmethod
    def library(cls, file_id: int) -> PrintSubject:
        return cls(kind="library", id=str(file_id))

    @classmethod
    def parse(cls, key: str) -> PrintSubject:
        """The subject a :attr:`key` names; ``ValueError`` for anything else."""
        match = _KEY.fullmatch(key)
        if match is None or (match["kind"] == "library" and not match["id"].isdecimal()):
            raise ValueError(f"{key!r} is not a print subject")
        return cls(kind=match["kind"], id=match["id"])  # type: ignore[arg-type]

    @classmethod
    def from_run_subject(cls, value: str) -> PrintSubject:
        """The subject a :attr:`run_subject` names."""
        return cls.parse(value) if value.startswith("library:") else cls.output(value)

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
