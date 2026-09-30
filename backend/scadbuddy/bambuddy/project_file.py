"""The editable project 3MF, filed into a project's Bambuddy folder on Generate (#317).

Generate with a project chosen uploads the output's 3MF into that project's folder
straight away, through the same library-copy cache a print uses (#316): the copy is
keyed by (folder, target), so a later print on the same printer reuses this file and the
folder ends up with it and its slice, not two copies.

No printer has been chosen at Generate time, so the target is the best guess at the
print to come: the printer and nozzle the project last printed on (Postgres, written by
every print into a project), else what the print dialog remembers for this model, else
the Settings printer or pipeline, else the fallback plate.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from pydantic import BaseModel

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.extruders import slicer_nozzle_stats
from scadbuddy.bambuddy.projects import folder_for
from scadbuddy.bambuddy.send import Target, attach_edit_link, ensure_copy, target_for
from scadbuddy.bambuddy.uploads import BambuddyUploadStore
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import OutputMeta, OutputStore
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.render.schema import ParamValue

#: How many changed params the file name spells out before it stops.
SUMMARY_PARAMS = 3
#: A generous cap well inside the 255 UTF-16 code units a FAT32/exFAT name may take
#: (a character outside the BMP, such as an emoji, takes two), leaving room for
#: `` (H2D)`` or `` (12)`` and the extension. Counted in those units, not characters.
STEM_MAX_UTF16_UNITS = 120
#: What Bambuddy refuses in a print file name (``utils/filename.py``: the printer's SD
#: card is FAT32/exFAT), plus control characters.
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class ProjectFileRequest(BaseModel):
    project_id: int


class ProjectFile(BaseModel):
    """Where the project file is, and whether this call put it there."""

    project_id: int
    folder_id: int
    library_file_id: int
    #: The name Bambuddy holds it under.
    filename: str
    #: ``False`` when the project already had this output's file for this target.
    created: bool
    #: The project's page in Bambuddy, which lists the folder's files.
    bambuddy_url: str
    #: The "Edit in ScadBuddy" link noted on the file, when one was attached (#80).
    edit_url: str | None = None


def _clean(text: str) -> str:
    return " ".join(_UNSAFE.sub("-", text).split())


def _within_utf16_units(text: str, limit: int) -> str:
    """The longest prefix of ``text`` that is at most ``limit`` UTF-16 code units, never
    ending halfway through a surrogate pair."""
    units = 0
    for index, char in enumerate(text):
        units += 2 if ord(char) > 0xFFFF else 1
        if units > limit:
            return text[:index]
    return text


def _value_text(name: str, value: ParamValue) -> str:
    if isinstance(value, bool):
        return name if value else f"no {name}"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def project_stem(
    template: str,
    params: Mapping[str, ParamValue],
    defaults: Mapping[str, ParamValue | None],
    *,
    name: str | None = None,
) -> str:
    """``Name sign — Reagan``: the template and the values that differ from its defaults.

    In the schema's order, at most :data:`SUMMARY_PARAMS` of them. With none changed it
    is the output's own name if it has one, else the template alone. Characters a print
    file name may not carry become ``-``, and a trailing dot or space is dropped.
    """
    changed = [
        _value_text(key, params[key])
        for key, default in defaults.items()
        if key in params and params[key] != default
    ]
    summary = ", ".join(changed[:SUMMARY_PARAMS]) + ("…" if len(changed) > SUMMARY_PARAMS else "")
    if not summary and name:
        summary = name
    stem = _clean(template) or "ScadBuddy"
    if summary.strip():
        stem = f"{stem} — {_clean(summary)}"
    return _within_utf16_units(stem, STEM_MAX_UTF16_UNITS).rstrip(" .") or "ScadBuddy"


async def generate_target(
    client: BambuddyClient,
    uploads: BambuddyUploadStore,
    meta: OutputMeta,
    settings: StoredSettings,
    project_id: int,
) -> Target:
    """What the project file is laid out for at Generate time, before any printer is
    chosen: the project's last print, else the model's remembered printer and nozzle,
    else the Settings default (:func:`target_for`, which ends at the fallback plate).

    With a printer and nozzle it also states which of the printer's sides has that
    nozzle now (#834), as the print does, so a print on the same printer and nozzles
    reuses this file. An unreadable status states nothing, as the print's does."""
    remembered = await uploads.project_target(project_id)
    if remembered is not None:
        printer_id: int | None = remembered.printer_id
        nozzle = remembered.nozzle_diameter
    else:
        choices = settings.model_print_choices.get(meta.slug)
        printer_id = (choices.printer_id if choices else None) or settings.printer_id
        nozzle = choices.nozzles[0].size if choices and choices.nozzles else None
    stats = None
    if printer_id is not None and nozzle is not None:
        try:
            stats = slicer_nozzle_stats(await client.printer_status(printer_id), nozzle)
        except (ApiError, ValueError):
            stats = None
    return await target_for(
        client, settings, printer_id=printer_id, nozzle_diameter=nozzle, nozzle_stats=stats
    )


async def file_into_project(
    client: BambuddyClient,
    store: OutputStore,
    uploads: BambuddyUploadStore,
    meta: OutputMeta,
    settings: StoredSettings,
    project_id: int,
    *,
    stem: str,
) -> ProjectFile:
    """Put this output's 3MF in the project's folder, once per (folder, target).

    The folder is created and linked if the project has none (:func:`folder_for`). The
    edit link is noted on a new file only: a reused one already carries it.
    """
    folder_id = await folder_for(client, project_id)
    target = await generate_target(client, uploads, meta, settings, project_id)
    ensured = await ensure_copy(
        client, store, uploads, meta, settings, target=target, folder_id=folder_id, stem=stem
    )
    link = (
        await attach_edit_link(client, ensured.library_file_id, meta, settings)
        if ensured.created
        else None
    )
    return ProjectFile(
        project_id=project_id,
        folder_id=folder_id,
        library_file_id=ensured.library_file_id,
        filename=ensured.filename,
        created=ensured.created,
        bambuddy_url=client.config.web_url(f"/projects/{project_id}"),
        edit_url=link,
    )
