"""Projects: one of Bambuddy's, with the library folder that belongs to it (#79).

ScadBuddy does not keep a project of its own. A project here is
``POST /api/v1/projects/`` plus ``POST /api/v1/library/folders/`` with ``project_id``
set, which is the pairing Bambuddy's own UI makes — its project page lists the folder's
files, its timeline and BOM read the archives and queue entries attached to it.

Three details of Bambuddy's shape are load-bearing:

* **Reading folders is the slashless path and creating one is not.**
  ``GET /api/v1/library/folders`` and ``POST /api/v1/library/folders/``; both routes are
  real, unlike ``/printers``.
* **A project's archives cannot be attached when the print starts.** An archive is
  produced *after* a print, so at run time there is nothing to attach. What can be done
  at run time is to put the 3MF in the project's folder and, on the queue route, set
  ``project_id`` on the queue item itself. The archives are attached later, from the
  queue entries Bambuddy has by then filled in — which is why :func:`attach_results`
  exists as its own call rather than being folded into the run.
* **The project routes need the ``Manage Projects`` scope**, which the key the rest of
  ScadBuddy uses may not carry. Every call here declares it, so a key without it
  reports *that scope by name* rather than a bare 403.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping
from datetime import UTC, datetime
from typing import Protocol

import psycopg
from fastapi import status
from pydantic import BaseModel, Field

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.commits import PROJECT_NOT_FOUND, refused, visible
from scadbuddy.bambuddy.errors import UNAVAILABLE_PROBLEM
from scadbuddy.bambuddy.linking import link_item
from scadbuddy.bambuddy.models import Folder, FolderCreate, Project, ProjectCreate
from scadbuddy.bambuddy.print_links import PrintLinkStore
from scadbuddy.bambuddy.uploads import DestinationKind
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import DestinationSort

logger = logging.getLogger(__name__)

#: Joins a folder's path (#3651), as the frontend's ``breadcrumbs`` joins a project's.
PATH_SEPARATOR = " \N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK} "


class ProjectView(BaseModel):
    """A Bambuddy project and the library folder sends to it land in.

    ``folder_id`` is ``None`` for a project that has no folder yet — an existing
    Bambuddy project made outside ScadBuddy usually does not, and linking one creates
    it rather than refusing.
    """

    id: int
    name: str
    description: str | None = None
    colour: str | None = None
    status: str
    #: The project this one is nested under (#930); ``None`` is a top-level project.
    parent_id: int | None = None
    archive_count: int = 0
    queue_count: int = 0
    folder_id: int | None = None
    folder_name: str | None = None
    #: When a file in its folder last changed in Bambuddy, else when the project was
    #: made (#3651): the picker's "modified" order.
    modified_at: datetime | None = None
    #: The later of ``modified_at`` and the last print, filing or choice of it from
    #: ScadBuddy (#3651): the picker's "last touched" order, its default.
    touched_at: datetime | None = None


class FolderView(BaseModel):
    """A library folder of no project, offered as a destination of its own (#3651)."""

    id: int
    name: str
    #: Its parents and its name, as the picker shows it, so two folders of one name can
    #: be told apart.
    path: str
    parent_id: int | None = None
    file_count: int | None = None
    modified_at: datetime | None = None
    touched_at: datetime | None = None


class ProjectChoices(BaseModel):
    projects: list[ProjectView] = Field(default_factory=list)
    #: #3651 — the library folders a print or Generate may be filed into instead of a
    #: project: every folder of no project, except external ones and the inbox (from
    #: Settings) with what ScadBuddy keeps under it.
    folders: list[FolderView] = Field(default_factory=list)
    #: The project the last send went to, so the picker opens where it was left.
    last_project_id: int | None = None
    #: The project Settings defaults to (#3651), when content is not in one already.
    default_project_id: int | None = None
    #: How Settings says the picker opens sorted (#3651).
    sort: DestinationSort = "touched"


class ProjectRequest(BaseModel):
    """Create a project, or link an existing one.

    ``project_id`` set means "link that one"; otherwise ``name`` is required and a new
    project is created. Only the fields ScadBuddy has an opinion about are sent —
    ``ProjectCreate`` also carries ``target_count``, ``due_date``, ``budget`` and more,
    and inventing values for them would put numbers on Bambuddy's project page that
    nobody chose.
    """

    project_id: int | None = None
    name: str | None = None
    description: str | None = None
    colour: str | None = None
    tags: str | None = None
    url: str | None = None
    #: The project to nest a new one under (#930); its folder is nested to match.
    parent_id: int | None = None
    #: The folder to link, when the project already has one nobody wants duplicated.
    #: Linked where it stands, so it is refused together with ``parent_id``.
    folder_id: int | None = None


#: When ScadBuddy last touched each project and folder (``BambuddyUploadStore``).
Touches = Mapping[tuple[DestinationKind, int], datetime]


def _utc(moment: datetime | None) -> datetime | None:
    """Bambuddy's timestamps are naive UTC; Postgres's are aware. One kind to compare."""
    if moment is None or moment.tzinfo is not None:
        return moment
    return moment.replace(tzinfo=UTC)


def _latest(*moments: datetime | None) -> datetime | None:
    known = [moment for moment in moments if moment is not None]
    return max(known) if known else None


def _view(project: Project, folder: Folder | None, touched: datetime | None = None) -> ProjectView:
    modified = _utc(folder.latest_activity_at if folder else None) or _utc(project.created_at)
    return ProjectView(
        id=project.id,
        name=project.name,
        description=project.description,
        colour=project.color,
        status=project.status,
        parent_id=project.parent_id,
        archive_count=project.archive_count,
        queue_count=project.queue_count,
        folder_id=folder.id if folder else None,
        folder_name=folder.name if folder else None,
        modified_at=modified,
        touched_at=_latest(modified, touched),
    )


def _destination_folders(
    tops: list[Folder], touches: Touches, inbox_id: int | None
) -> list[FolderView]:
    """Every folder a send may go to that is not a project's (:class:`ProjectChoices`).

    A folder linked to a project is offered as that project, and its sub-folders
    (``Media/``, #309) are the project's own. Under the inbox is ScadBuddy's: the inbox
    itself is "No project", and the blob store keeps its files beneath it.
    """
    found: list[FolderView] = []

    def visit(folder: Folder, path: list[str]) -> None:
        if folder.id == inbox_id or folder.is_external or folder.project_id is not None:
            return
        if folder.archive_id is not None:
            return
        here = [*path, folder.name]
        modified = _utc(folder.latest_activity_at)
        found.append(
            FolderView(
                id=folder.id,
                name=folder.name,
                path=PATH_SEPARATOR.join(here),
                parent_id=folder.parent_id,
                file_count=folder.file_count,
                modified_at=modified,
                touched_at=_latest(modified, touches.get(("folder", folder.id))),
            )
        )
        for child in folder.children:
            visit(child, here)

    for top in tops:
        visit(top, [])
    return found


async def describe_projects(
    client: BambuddyClient,
    *,
    last_project_id: int | None = None,
    default_project_id: int | None = None,
    sort: DestinationSort = "touched",
    touches: Touches | None = None,
    inbox_id: int | None = None,
) -> ProjectChoices:
    """Every project, with the folder it owns, and the folders of no project (#3651).

    The folders are read once and matched on ``project_id`` rather than asking
    ``/library/folders/by-project/{id}`` per project: that is one request instead of one
    per row, and the flat list already carries the link.
    """
    touched = touches or {}
    projects = await client.projects()
    tops = await client.folders()
    # Flattened, because `/library/folders` answers with a *tree*: a sub-folder arrives
    # inside its parent's `children`, so a project folder nested under another one is
    # invisible to a scan of the top level.
    by_project: dict[int, Folder] = {}
    for folder in (row for top in tops for row in top.walk()):
        if folder.project_id is not None:
            by_project.setdefault(folder.project_id, folder)
    return ProjectChoices(
        projects=[
            _view(project, by_project.get(project.id), touched.get(("project", project.id)))
            for project in projects
        ],
        folders=_destination_folders(tops, touched, inbox_id),
        last_project_id=last_project_id,
        default_project_id=default_project_id,
        sort=sort,
    )


class FolderlessProjects(Protocol):
    """The projects this ScadBuddy created whose folder is not linked yet (#2263).

    :class:`~scadbuddy.bambuddy.uploads.BambuddyUploadStore` in Postgres. A project is
    remembered the moment Bambuddy answers its create and forgotten once its folder is
    linked, so a row left behind is exactly a project whose folder step failed.
    """

    async def folderless(self, name: str, parent_id: int | None) -> list[int]: ...

    async def remember_folderless(
        self, project_id: int, name: str, parent_id: int | None
    ) -> None: ...

    async def forget_folderless(self, project_id: int) -> None: ...


async def ensure_project(
    client: BambuddyClient,
    request: ProjectRequest,
    *,
    folderless: FolderlessProjects | None = None,
) -> ProjectView:
    """Create or link a project, and make sure it has a library folder.

    Linking is idempotent on the folder: a project that already has one keeps it, so
    linking the same project twice does not leave Bambuddy with two folders of the same
    name pointing at it.

    Creating is idempotent on a failed folder step, given ``folderless`` (#2263): a
    create pressed again after Bambuddy kept the project but its folder failed finds
    that project and gives it the folder, rather than making a second project of the
    same name. The key is ScadBuddy's own record of the project id it created, not the
    name: Bambuddy allows two projects of one name, and a folderless one is also what
    every project made in Bambuddy's UI is, so a name match would adopt a project the
    user made on purpose. The name and parent only pick which record this create means.
    """
    created = False
    if request.project_id is not None:
        if request.parent_id is not None:
            # Linking never re-parents a project; dropping the field would look like it did.
            raise ApiError(400, "parent_id applies only to a new project, not a linked one")
        project = await client.project(request.project_id)
    else:
        # Trimmed: a name of spaces made a blank project and folder in Bambuddy (#1332).
        name = (request.name or "").strip()
        if not name:
            raise ApiError(400, "a new project needs a name")
        if request.parent_id is not None and request.folder_id is not None:
            # A linked folder stays where it is, so the project would be nested and its
            # folder not.
            raise ApiError(400, "parent_id cannot be combined with folder_id")
        found = (
            await _created_before(client, folderless, name, request.parent_id)
            if folderless is not None
            else None
        )
        if found is not None:
            project = found
        else:
            project = await client.create_project(
                ProjectCreate(
                    name=name,
                    description=request.description,
                    color=request.colour,
                    tags=request.tags,
                    url=request.url,
                    parent_id=request.parent_id,
                )
            )
            created = True
            if folderless is not None:
                await folderless.remember_folderless(project.id, name, request.parent_id)

    folder = project_folder(await client.folders_by_project(project.id))
    if folder is None and request.folder_id is not None:
        # Flattened, like every other folder lookup here: `/library/folders` answers
        # with a tree, so a folder nested under another is invisible to a scan of the
        # top level — and this path would then silently create a second one.
        folder = next(
            (
                row
                for top in await client.folders()
                for row in top.walk()
                if row.id == request.folder_id
            ),
            None,
        )
    if folder is None:
        folder = await _create_folder(
            client,
            project,
            FolderCreate(
                name=project.name,
                project_id=project.id,
                parent_id=await _parent_folder_id(client, project),
            ),
            created=created,
        )
    if folderless is not None:
        await folderless.forget_folderless(project.id)
    return _view(project, folder)


async def _created_before(
    client: BambuddyClient, folderless: FolderlessProjects, name: str, parent_id: int | None
) -> Project | None:
    """The project an earlier create of ``name`` under ``parent_id`` made and left
    without a folder, if Bambuddy still has it under that name and parent.

    A record whose project is gone (its commit failed, or it was deleted) or was renamed
    or moved since is forgotten: it is no longer the project this create means.
    """
    for project_id in await folderless.folderless(name, parent_id):
        try:
            project = await client.project(project_id)
        except ApiError as error:
            if not refused(error, PROJECT_NOT_FOUND):
                raise
        else:
            if project.name.strip() == name and project.parent_id == parent_id:
                return project
        await folderless.forget_folderless(project_id)
    return None


async def _create_folder(
    client: BambuddyClient, project: Project, folder: FolderCreate, *, created: bool
) -> Folder:
    """Create the project's folder, waiting out a project Bambuddy has not committed.

    Bambuddy answers ``POST /projects/`` before it commits the row: its route only
    flushes, and the commit is the exit of its ``get_db`` dependency, which FastAPI runs
    after the response is sent (#2065). The folder that follows at once runs on another
    session, cannot see the project and is refused with 404 "Project not found". So for
    a project created in this call, and only that refusal, the project is read back
    until it is visible and the folder is created again. A linked project that 404s is
    really gone and is never retried.
    """
    try:
        return await client.create_folder(folder)
    except ApiError as error:
        if not created or not refused(error, PROJECT_NOT_FOUND):
            raise
    await _wait_for_commit(client, project)
    return await client.create_folder(folder)


async def _wait_for_commit(client: BambuddyClient, project: Project) -> None:
    """Return once Bambuddy can read ``project`` back, or raise when it never can: its
    commit failed (SQLite's "database is locked"), so the project does not exist and
    nothing was linked to it."""
    if await visible(lambda: client.project(project.id), PROJECT_NOT_FOUND):
        return
    raise ApiError(
        status.HTTP_502_BAD_GATEWAY,
        f"Bambuddy answered that it created the {project.name!r} project (id {project.id}) "
        "but did not keep it, so it has no folder and nothing was linked to it. "
        "Create the project again.",
        type_=UNAVAILABLE_PROBLEM,
    )


async def _parent_folder_id(client: BambuddyClient, project: Project) -> int | None:
    """The folder a nested project's folder goes in: its parent project's own (#930).

    ``None`` (the library's top level) when the project has no parent, or the parent
    has no folder — making one for it would write to a project nobody touched.
    """
    if project.parent_id is None:
        return None
    parent = project_folder(await client.folders_by_project(project.parent_id))
    return parent.id if parent else None


async def folder_for(client: BambuddyClient, project_id: int) -> int:
    """The folder a project's sends belong in, created and linked if it has none yet.

    Never ``None``: falling back to the Settings folder would put the project's copy in
    the inbox, where the next inbox upload supersedes and deletes it (#316) — and the
    picker offers folderless projects on the promise that the first send creates the
    folder. One read when the folder exists; :func:`ensure_project` otherwise.
    """
    folder = project_folder(await client.folders_by_project(project_id))
    if folder is not None:
        return folder.id
    view = await ensure_project(client, ProjectRequest(project_id=project_id))
    if view.folder_id is None:  # pragma: no cover - ensure_project always links one
        raise ApiError(
            status.HTTP_502_BAD_GATEWAY,
            f"Bambuddy did not link a library folder to project {project_id}",
        )
    return view.folder_id


def project_folder(folders: list[Folder]) -> Folder | None:
    """The project's own folder among the folders linked to it.

    A sub-folder linked to the project too (``Media/``, #309) would be in the by-project
    list, and, in whatever order Bambuddy lists them, "the first one" could be it. The
    project folder is the one whose parent is not another folder of the same project.
    """
    ids = {folder.id for folder in folders}
    return next((folder for folder in folders if folder.parent_id not in ids), None)


class AttachResult(BaseModel):
    """What was attached, so the UI can say so rather than claiming more than happened."""

    project_id: int
    queue_item_ids: list[int] = Field(default_factory=list)
    archive_ids: list[int] = Field(default_factory=list)


async def attach_results(
    client: BambuddyClient,
    project_id: int,
    *,
    queue_item_ids: list[int],
    output_id: str | None = None,
    links: PrintLinkStore | None = None,
    linkable: Collection[int] = (),
) -> AttachResult:
    """Attach this output's queue entries, and any archives they have produced.

    Split from the run on purpose. A plate's queue item only exists once it has sliced,
    and an archive only once a print has finished — so attaching at run time would
    attach only part of it. This is called once the ids are known, and attaching the
    same id twice is Bambuddy's problem to dedupe, not a reason to keep state here.

    With ``output_id`` and ``links``, the archive of each item in ``linkable`` is also
    linked to the output (#306), since the items are being read anyway. Only those:
    ``queue_item_ids`` can come from the caller, and an item that is not the output's
    must never open the media proxy to its archive.
    """
    archives: list[int] = []
    for item_id in queue_item_ids:
        try:
            item = await client.queue_item(item_id)
        except ApiError as error:
            if error.status != 404:
                raise
            # Deleted in Bambuddy (a queue item otherwise outlives its print); there is
            # nothing left to attach it by.
            logger.info("a queue entry was gone before it could be attached", extra={"id": item_id})
            continue
        if item.archive_id is not None:
            archives.append(item.archive_id)
            if output_id is not None and links is not None and item_id in linkable:
                # A side effect of the attach, which must not fail over it.
                try:
                    await link_item(links, output_id, item)
                except psycopg.Error:
                    logger.exception("could not link a queue item's archive", extra={"id": item_id})

    if queue_item_ids:
        await client.add_queue_items_to_project(project_id, queue_item_ids)
    if archives:
        await client.add_archives_to_project(project_id, archives)
    return AttachResult(
        project_id=project_id, queue_item_ids=list(queue_item_ids), archive_ids=archives
    )


class ProjectAttach(BaseModel):
    """Which of this output's queue entries to file under the project.

    The ids come from the progress read (#89): a plate's queue item only exists once it
    has sliced, so the caller learns them by polling.
    """

    #: Omitted means the remembered project; an explicit ``null`` is "No project" (#317).
    project_id: int | None = None
    queue_item_ids: list[int] = Field(default_factory=list)
