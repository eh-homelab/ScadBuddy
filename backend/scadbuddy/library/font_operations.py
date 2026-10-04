"""Installing a font family as an operation (#1054, spec 2026-10-01 §4.3, the
``library`` row: "font install"). Its subject is the family, lower-cased."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from scadbuddy.api import fonts as fonts_api
from scadbuddy.library.operations import answered_as_routes
from scadbuddy.operations.kinds import OperationKind

if TYPE_CHECKING:
    from scadbuddy.api.deps import AppState

#: Every face's download, then `fc-cache`.
FONT_INSTALL_TIMEOUT = timedelta(minutes=5)


def font_kinds(state: AppState) -> list[OperationKind]:
    """The font kind, bound to this process's state; ``library/operations.py`` exports
    it with the pins."""

    async def no_check(request: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def install_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        installed = await fonts_api.install_run(request["family"], request["force"], state)
        dumped: dict[str, Any] = installed.model_dump(mode="json")
        return dumped

    return [
        OperationKind(
            "font_install",
            no_check,
            answered_as_routes(install_run),
            queue="library",
            run_timeout=FONT_INSTALL_TIMEOUT,
        )
    ]
