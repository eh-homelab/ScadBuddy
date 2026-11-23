"""An entity reference, `kind:ref` (#6950, spec 6.1)."""

from __future__ import annotations

import re
from dataclasses import dataclass

_PARSE = re.compile(r"^([a-z_]{1,40}):(.+)$", re.DOTALL)


class BadEntityRef(ValueError):  # noqa: N818
    """The text is not a `kind:ref` reference."""


@dataclass(frozen=True)
class EntityRef:
    kind: str
    ref: str

    def __str__(self) -> str:
        return f"{self.kind}:{self.ref}"

    @classmethod
    def parse(cls, text: str) -> EntityRef:
        found = _PARSE.match(text)
        if found is None:
            raise BadEntityRef(text)
        return cls(found.group(1), found.group(2))
