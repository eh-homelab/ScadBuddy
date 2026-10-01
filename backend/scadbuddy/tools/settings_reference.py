"""Print every ``SCADBUDDY_*`` variable the backend reads: type, default, and whether
the Settings page can change it.

Generated from :class:`scadbuddy.core.settings.Settings`, so the README points here
instead of listing each variable (#508): a new setting is one field, not one more line
in a shared list. What a variable does is in the comment beside its field.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from pydantic_core import PydanticUndefined

from scadbuddy.core.settings import APPLIES, BOOTSTRAP_FIELDS, Settings


def _type_name(annotation: Any) -> str:
    if isinstance(annotation, type):
        return annotation.__name__
    # A `|` inside a table cell would end it.
    return str(annotation).replace("pathlib.", "").replace("typing.", "").replace("|", "\\|")


def _default(value: Any) -> str:
    if value is PydanticUndefined:
        return "(required)"
    if value is None or value == "":
        return "(unset)"
    return f"`{value}`" if isinstance(value, str | Path | bool) else f"`{value!r}`"


def reference() -> str:
    prefix = Settings.model_config.get("env_prefix", "")
    # "In Settings": whether the UI can change it, and when a change takes effect
    # (`APPLIES`, #322); a bootstrap field is read from the environment only.
    lines = ["| Variable | Type | Default | In Settings |", "|---|---|---|---|"]
    for name, field in Settings.model_fields.items():
        # A required field defaults to "" only so its validator can say what to set
        # (#401, #546).
        required = field.validate_default and field.default == ""
        default = PydanticUndefined if required else field.default
        env = f"{prefix}{name}".upper()
        ui = "no" if name in BOOTSTRAP_FIELDS else APPLIES[name]
        lines.append(f"| `{env}` | `{_type_name(field.annotation)}` | {_default(default)} | {ui} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    del argv
    sys.stdout.write(reference())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
