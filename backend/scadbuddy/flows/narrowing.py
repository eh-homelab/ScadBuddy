"""Whether a parent's input for a parked call is a narrowing of it (#2482, spec
2026-10-11 §4.1): the same call, made smaller, never another one.

The vocabulary is closed, as the grant's bounds are (Ruling 3). A print may drop copies
(when it named them), all plates, or some of the plates its `plate_ids` chose (keeping
at least one); an arrange may drop objects, or copies of one (when
it named them). Every other field must stay as the call had it: a different printer,
source, plate, model or option is a substitution, not a narrowing, and an agent tool's
arguments carry no meaning ScadBuddy could judge smaller, so a tool call is never
narrowed (its parent denies it or passes it up). A field left out is the same as one
sent as null.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

Check = Callable[[Any, Any, str], str | None]


def _present(value: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in value.items() if v is not None}


def _fewer(original: Any, proposed: Any, what: str) -> str | None:
    """`proposed` is a count no larger than `original`, which the call named."""
    if not isinstance(original, int) or isinstance(original, bool):
        return f"{what} was not named by the call, so it cannot be narrowed"
    if not isinstance(proposed, int) or isinstance(proposed, bool):
        return f"{what} must be a whole number"
    if not 0 <= proposed <= original:
        return f"{what} may only go down, from {original}"
    return None


def _fields(
    original: dict[str, Any], proposed: dict[str, Any], narrowable: dict[str, Check], where: str
) -> str | None:
    original, proposed = _present(original), _present(proposed)
    for key in sorted(set(original) | set(proposed)):
        if key in narrowable:
            # Left out where the call named it is checked too: an omitted count is the
            # remembered or the source's own, which may be more.
            problem = narrowable[key](original.get(key), proposed.get(key), f"{where}{key}")
            if problem is not None:
                return problem
        elif original.get(key) != proposed.get(key):
            return f"{where}{key} must stay as the call had it"
    return None


def _all_plates(original: Any, proposed: Any, what: str) -> str | None:
    if proposed not in (None, True, False):
        return f"{what} is true or false"
    if proposed is True and original is not True:
        return f"{what} may only be turned off"
    return None


def _plate_ids(original: Any, proposed: Any, what: str) -> str | None:
    """Fewer of the call's chosen plates (#3652), never others or none."""
    if proposed == original:
        return None
    if not isinstance(original, list):
        return f"{what} must stay as the call had it"
    if not isinstance(proposed, list) or not proposed:
        return f"{what} must keep at least one of the call's plates"
    if any(plate not in original for plate in proposed):
        return f"{what} may only leave out plates the call chose"
    return None


def _print_copies(original: Any, proposed: Any, what: str) -> str | None:
    problem = _fewer(original, proposed, what)
    if problem is None and proposed < 1:
        return f"{what} is at least 1; deny the call instead"
    return problem


def _objects(original: Any, proposed: Any, what: str) -> str | None:
    """An order-kept subset of the call's objects, each as it was but for a lower
    `count`."""
    if not isinstance(original, list) or not isinstance(proposed, list) or not proposed:
        return f"{what} must keep at least one of the call's objects"
    at = 0
    for i, item in enumerate(proposed):
        if not isinstance(item, dict):
            return f"{what}[{i}] is not an object"
        while at < len(original):
            candidate = original[at]
            at += 1
            if isinstance(candidate, dict) and (
                _fields(candidate, item, {"count": _fewer}, f"{what}[{i}].") is None
            ):
                break
        else:
            return f"{what}[{i}] is not one of the call's objects, in its order, or fewer of it"
    return None


def narrowing_problem(fn: str, original: dict[str, Any], proposed: dict[str, Any]) -> str | None:
    """None when `proposed` (the call's arguments as `flow_grant_check` sees them) is a
    narrowing of `original`, the parked call's; else why not."""
    if fn == "queue_print":
        if proposed.get("source") != original.get("source"):
            return "source must stay as the call had it"
        return _fields(
            original.get("request") or {},
            proposed.get("request") or {},
            {"copies": _print_copies, "all_plates": _all_plates, "plate_ids": _plate_ids},
            "",
        )
    if fn == "arrange":
        return _fields(
            original.get("request") or {}, proposed.get("request") or {}, {"objects": _objects}, ""
        )
    return "an agent tool's call cannot be narrowed: deny it or pass it up"
