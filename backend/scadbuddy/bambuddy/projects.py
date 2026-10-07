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
from collections.abc import Collection

import psycopg
from fastapi import status
from pydantic import BaseModel, Field

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.linking import link_item
from scadbuddy.bambuddy.models import Folder, FolderCreate, Project, ProjectCreate
from scadbuddy.bambuddy.print_links import PrintLinkStore
from scadbuddy.bambuddy.uploads import DatabaseRequiredError
from scadbuddy.core.problems import ApiError

logger = logging.getLogger(__name__)


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


class ProjectChoices(BaseModel):
    projects: list[ProjectView] = Field(default_factory=list)
    #: The project the last send went to, so the picker opens where it was left.
    last_project_id: int | None = None


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


def _view(project: Project, folder: Folder | None) -> ProjectView:
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
    )


async def describe_projects(
    client: BambuddyClient, *, last_project_id: int | None = None
) -> ProjectChoices:
    """Every project, with the folder it owns.

    The folders are read once and matched on ``project_id`` rather than asking
    ``/library/folders/by-project/{id}`` per project: that is one request instead of one
    per row, and the flat list already carries the link.
    """
    projects = await client.projects()
    # Flattened, because `/library/folders` answers with a *tree*: a sub-folder arrives
    # inside its parent's `children`, so a project folder nested under another one is
    # invisible to a scan of the top level.
    folders = [row for top in await client.folders() for row in top.walk()]
    by_project: dict[int, Folder] = {}
    for folder in folders:
        if folder.project_id is not None:
            by_project.setdefault(folder.project_id, folder)
    return ProjectChoices(
        projects=[_view(project, by_project.get(project.id)) for project in projects],
        last_project_id=last_project_id,
    )


async def ensure_project(client: BambuddyClient, request: ProjectRequest) -> ProjectView:
    """Create or link a project, and make sure it has a library folder.

    Linking is idempotent on the folder: a project that already has one keeps it, so
    linking the same project twice does not leave Bambuddy with two folders of the same
    name pointing at it.
    """
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
        folder = await client.create_folder(
            FolderCreate(
                name=project.name,
                project_id=project.id,
                parent_id=await _parent_folder_id(client, project),
            )
        )
    return _view(project, folder)


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
                except (psycopg.Error, DatabaseRequiredError):
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
