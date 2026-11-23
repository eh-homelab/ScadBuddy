"""A flow's `print_output` call names the registry printer that prints (#4445), and its
`hosted_plugin_publish` call names the draft it publishes (#7199).

The script may name a registry printer (`printer`) and leave out the Bambuddy printer
it links (`printer_id`). The agent's `prepareApproval` (agent/src/tools/print.ts) adds
that `printer_id` for a session's call. This module does the same for a flow's call:
the run page shows the printer's name, and a person's approval binds to the resolved
pair. The agent's gate (agent/src/temporal/toolActivities.ts) adds the pair again when
the call runs and hashes it. A row re-linked between the approval and the run gives
another `printer_id`, so the hash does not match and the agent refuses the call.

A row that links no Bambuddy printer prints over the LAN: its call binds
`printer_id: null` here and in the agent's `flowBinding` alike. So the binding never
equals the script's raw arguments, and a row linked between the approval and the run
hashes otherwise. A row the registry does not hold binds `printer_id: null` here only,
so the run page can still show the call and its approval stays distinct from the raw
arguments; the agent's `prepareApproval` refuses that call when it runs (its read of
the row answers 404), so it never binds or runs it. A call that sends its own
`printer_id` is bound as sent: the agent refuses a pair the row does not link.

`printer` counts as named only in the form the agent's schema takes (zod's `uuid()`,
`UUID_FORM`); any other form is bound as sent, and the agent refuses it, since its
arguments do not parse. Both sides hash the string as the script sent it, case and all.

A flow's `hosted_plugin_publish` call (#7199) is bound the same way. The script names the
hosted plugin (`name`) and leaves out the draft's digest (`draft`). The agent's
`prepareApproval` (agent/src/tools/hostedPlugins.ts) adds the draft-diff's `draft_digest`,
and its `flowBinding` adds it to a flow's call. This module adds the digest the draft has
now (`DraftLookup`), so a person's approval binds that draft. The agent's gate reads the
digest again when the call runs, and the publish sends it as `expect_draft`. A draft edited
between the approval and the run gives another digest, so the agent refuses the call. A
plugin that does not exist binds `draft: null` here only; the agent refuses that call when
it runs. A call that sends its own `draft` is bound as sent. A `name` the agent's schema
does not take (`rules.NAME_RE`, no `--`) is bound as sent too, and the agent refuses it.

The run page gets the bound hash with the summary (`bound_hash`), and the person's
decision sends it back: the approval binds only the call the person was shown.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from scadbuddy.hosted_plugins import rules
from scadbuddy.printers.models import PrinterRow

logger = logging.getLogger(__name__)

#: The agent tools this binds.
PRINT_OUTPUT = "print_output"
HOSTED_PLUGIN_PUBLISH = "hosted_plugin_publish"

#: The uuid forms the agent's `z.string().uuid()` accepts (zod 4's RFC 9562 pattern,
#: agent/node_modules/zod/v4/core/regexes.js `uuid`): hyphenated, either case, versions 1
#: to 8 with the RFC variant, and the nil and max uuids. No braces, no `urn:uuid:`.
UUID_FORM = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}"
    r"|00000000-0000-0000-0000-000000000000|ffffffff-ffff-ffff-ffff-ffffffffffff"
)

#: Reads a registry row by its id; None when the registry does not hold it.
PrinterLookup = Callable[[str], Awaitable[PrinterRow | None]]

#: Reads the draft digest of a hosted plugin by its name (the draft-diff's `draft_digest`,
#: api/hosted_plugins.py `_draft_digest`); None when no hosted plugin has the name.
DraftLookup = Callable[[str], Awaitable[str | None]]

#: Whether a run on a registry row goes over the LAN, never through Bambuddy (the row's
#: id in `print_run.lan_printers`, as the agent's `linkedNative` reads the choices'
#: `lan_printers`, #4660). Only the run page's line reads it; the binding never does.
#: None: it could not be read, and the line names no path.
NativeCheck = Callable[[str], Awaitable[bool | None]]


@dataclass(frozen=True)
class BoundCall:
    """What an approval of the call binds to, and the line the run page shows."""

    args: dict[str, Any]
    summary: str | None = None


def named_printer(tool: str | None, args: dict[str, Any]) -> str | None:
    """The registry printer a `print_output` call names, else None."""
    printer = args.get("printer")
    if tool != PRINT_OUTPUT or not isinstance(printer, str):
        return None
    return printer if UUID_FORM.fullmatch(printer) else None


def named_draft(tool: str | None, args: dict[str, Any]) -> str | None:
    """The hosted plugin a `hosted_plugin_publish` call names and whose draft it leaves to
    the approval (it sends no `draft`), else None."""
    name = args.get("name")
    if tool != HOSTED_PLUGIN_PUBLISH or not isinstance(name, str) or "draft" in args:
        return None
    return name if rules.NAME_RE.fullmatch(name) and "--" not in name else None


def binds(tool: str | None, args: dict[str, Any]) -> bool:
    """Whether a call's approval binds more than its raw arguments (`bind_call`)."""
    return named_printer(tool, args) is not None or named_draft(tool, args) is not None


async def bind_call(
    tool: str | None,
    args: dict[str, Any],
    lookup: PrinterLookup,
    native: NativeCheck | None = None,
    drafts: DraftLookup | None = None,
) -> BoundCall:
    """The call as its approval binds it: a `print_output` call that names a linked
    registry printer and no `printer_id` gets the `printer_id` the row links now, and a
    `hosted_plugin_publish` call that sends no `draft` gets the draft's digest now
    (`drafts`; without it, the call is bound with no line, so it cannot be approved).

    The line says "over the LAN" for a run that does not go through Bambuddy, as the
    agent's `prepareApproval` words it: a row that links no Bambuddy printer, or a linked
    one `native` says prints natively. "Bambuddy printer N" only when `native` says the
    run goes through Bambuddy; without `native`, or when it fails, the line names the
    printer alone, never a path it could not check."""
    plugin = named_draft(tool, args)
    if plugin is not None:
        return await _bind_draft(plugin, args, drafts)
    printer = named_printer(tool, args)
    if printer is None:
        return BoundCall(args)
    row = await lookup(printer)
    if row is None:
        summary = f"Prints on printer {printer}, which the registry does not hold"
    elif row.bambuddy_printer_id is None and args.get("path") == "bambuddy":
        # #5352: the run refuses it, as the agent's `printerOf` does: nothing goes out.
        summary = (
            f"Refused: {row.name} links no Bambuddy printer, so it cannot print through"
            " Bambuddy's queue"
        )
    elif row.bambuddy_printer_id is None:
        summary = f"Prints on {row.name} over the LAN"
    elif args.get("path") == "bambuddy":
        # #5352: the call asked for Bambuddy's queue, so the run goes through Bambuddy.
        summary = f"Prints on {row.name} (Bambuddy printer {row.bambuddy_printer_id})"
    else:
        lan = await _lan_of(row, native)
        if lan is False and args.get("path") == "native":
            # #5602: the run refuses it (`print_run.LAN_PATH_REFUSAL`): nothing goes out.
            summary = (
                f"Refused: {row.name} prints through Bambuddy (Bambuddy printer"
                f" {row.bambuddy_printer_id}), so it cannot print over the LAN as the call asks"
            )
        else:
            summary = f"Prints on {row.name}{_path_named(row, lan)}"
    if "printer_id" in args:
        # The script sent the pair: bound as sent. The agent refuses a pair that disagrees.
        return BoundCall(args, summary)
    linked = None if row is None else row.bambuddy_printer_id
    return BoundCall({**args, "printer_id": linked}, summary)


async def _bind_draft(plugin: str, args: dict[str, Any], drafts: DraftLookup | None) -> BoundCall:
    """A publish of `plugin`'s draft, bound to its digest now."""
    if drafts is None:
        return BoundCall(args)
    digest = await drafts(plugin)
    if digest is None:
        summary = f"Publishes hosted plugin {plugin}, which does not exist"
    else:
        short = digest.removeprefix("sha256:")[:12]
        summary = f"Publishes the draft of hosted plugin {plugin} as it is now (draft {short})"
    return BoundCall({**args, "draft": digest}, summary)


async def _lan_of(row: PrinterRow, native: NativeCheck | None) -> bool | None:
    """Whether a run on a linked row goes over the LAN; None when it cannot be read."""
    if native is None:
        return None
    try:
        return await native(row.id)
    except Exception:
        # Only the line's wording: the binding stands, and the line says no path.
        logger.warning("could not read whether a run on a printer goes over the LAN", exc_info=True)
        return None


def _path_named(row: PrinterRow, lan: bool | None) -> str:
    """How a linked row's line names the path its run takes."""
    if lan is None:
        return ""
    return " over the LAN" if lan else f" (Bambuddy printer {row.bambuddy_printer_id})"


def api_lookup(client: httpx.AsyncClient) -> PrinterLookup:
    """A lookup through the API's own `GET /api/v1/printers/{id}`: the projects worker
    reads the registry over HTTP, never from the database (#5295)."""

    async def lookup(printer: str) -> PrinterRow | None:
        answer = await client.get(f"/api/v1/printers/{printer}")
        if answer.status_code == httpx.codes.NOT_FOUND:
            return None
        answer.raise_for_status()
        return PrinterRow.model_validate(answer.json())

    return lookup


def api_drafts(client: httpx.AsyncClient) -> DraftLookup:
    """A draft lookup through the API's own `GET /api/v1/hosted-plugins/{name}/draft-diff`:
    the projects worker reads it over HTTP, never from the database (#5295)."""

    async def drafts(plugin: str) -> str | None:
        answer = await client.get(f"/api/v1/hosted-plugins/{plugin}/draft-diff")
        if answer.status_code == httpx.codes.NOT_FOUND:
            return None
        answer.raise_for_status()
        # An answer that is no JSON, or holds no digest, is a failed read, as an HTTP error is:
        # `flow_decide` turns any `httpx.HTTPError` into its "could not read" refusal (#7211).
        try:
            digest = answer.json().get("draft_digest")
        except ValueError as err:
            raise httpx.DecodingError(
                "the draft-diff answered no JSON", request=answer.request
            ) from err
        if not isinstance(digest, str):
            raise httpx.DecodingError(
                "the draft-diff answered no draft_digest", request=answer.request
            )
        return digest

    return drafts
