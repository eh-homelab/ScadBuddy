"""The rule for a prompt's schema."""

from __future__ import annotations

from typing import Any

from scadbuddy.core.input_schema import schema_problems
from scadbuddy.core.json_values import unstorable


def prompt_schema_problems(
    schema: Any,
    *,
    stored: bool = False,
    defaults: bool = True,
    widgets: bool = True,
    retry_timeouts: bool = False,
) -> list[str]:
    """Everything wrong with ``schema`` as a prompt's; ``stored``, ``widgets``, ``defaults``
    and ``retry_timeouts`` as `schema_problems`."""
    # First: the schema check itself writes the schema as JSON and recurses into it.
    for bad in unstorable(schema):
        return [f"the schema {bad.message}"]
    return schema_problems(
        schema,
        stored=stored,
        formats=True,
        widgets=widgets,
        defaults=defaults,
        retry_timeouts=retry_timeouts,
    )
