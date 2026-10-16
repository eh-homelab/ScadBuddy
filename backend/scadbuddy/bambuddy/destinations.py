"""Where a print or a Generate is filed by default (#3651).

One rule, wherever ScadBuddy asks where an output goes: when every piece of content in
question (each output or library file being printed or arranged) is already in one
Bambuddy project, that project; otherwise the project Settings names; otherwise the
project the last send went to. The user can always choose another.

An output is in a project when a copy of it sits in the project's folder (Generate
filed it, or a print uploaded it there, #316) or it was last printed into it. A library
file is in the project its folder belongs to. Content in two projects, or in none, is
not "in one project", so the Settings default applies.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.uploads import BambuddyUploadStore
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import OutputMeta
from scadbuddy.library.settings_store import StoredSettings

#: Why the default is what it is: the content's own project, the Settings default, the
#: last send's project, or none at all ("No project").
DefaultReason = Literal["content", "settings", "last", "none"]


class DefaultDestination(BaseModel):
    """The destination a picker opens on for some content."""

    project_id: int | None = None
    reason: DefaultReason = "none"


async def default_destination(
    client: BambuddyClient,
    uploads: BambuddyUploadStore,
    settings: StoredSettings,
    *,
    outputs: Sequence[OutputMeta] = (),
    library_file_ids: Sequence[int] = (),
) -> DefaultDestination:
    """This module's rule for the outputs and library files given.

    Two reads of Bambuddy, the projects and the folder tree, plus one per library file.
    A project that is no longer in Bambuddy is never a default, so a deleted Settings
    default falls through to the last send's project rather than failing the print.
    """
    known = {project.id for project in await client.projects()}
    folder_project = {
        folder.id: folder.project_id
        for top in await client.folders()
        for folder in top.walk()
        if folder.project_id is not None
    }
    copies = await uploads.for_outputs(meta.id for meta in outputs) if outputs else {}

    homes: list[set[int]] = []
    for meta in outputs:
        home = {
            folder_project[copy.folder_id]
            for copy in copies.get(meta.id, [])
            if copy.folder_id in folder_project
        }
        if meta.project_id is not None:
            home.add(meta.project_id)
        homes.append(home)
    for file_id in library_file_ids:
        try:
            file = await client.library_file(file_id)
        except ApiError as error:
            if error.status != 404:
                raise
            homes.append(set())
            continue
        project = folder_project.get(file.folder_id) if file.folder_id is not None else None
        homes.append({project} if project is not None else set())

    shared = set.intersection(*homes) if homes else set()
    if len(shared) == 1 and all(len(home) == 1 for home in homes):
        (project_id,) = shared
        if project_id in known:
            return DefaultDestination(project_id=project_id, reason="content")
    if settings.default_project_id is not None and settings.default_project_id in known:
        return DefaultDestination(project_id=settings.default_project_id, reason="settings")
    if settings.last_project_id is not None and settings.last_project_id in known:
        return DefaultDestination(project_id=settings.last_project_id, reason="last")
    return DefaultDestination()
