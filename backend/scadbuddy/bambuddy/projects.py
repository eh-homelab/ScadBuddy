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

from pydantic import BaseModel, Field

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.models import Folder, FolderCreate, Project, ProjectCreate
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
    archive_count: int = 0
    queue_count: int = 0
    folder_id: int | None = None
    folder_name: str | None = None


class ProjectChoices(BaseModel):
    projects: list[ProjectView] = Field(default_factory=list)
    #: This model's own project, if one has been remembered for it.
    model_project_id: int | None = None


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
    #: The folder to link, when the project already has one nobody wants duplicated.
    folder_id: int | None = None


def _view(project: Project, folder: Folder | None) -> ProjectView:
    return ProjectView(
        id=project.id,
        name=project.name,
        description=project.description,
        colour=project.color,
        status=project.status,
        archive_count=project.archive_count,
        queue_count=project.queue_count,
        folder_id=folder.id if folder else None,
        folder_name=folder.name if folder else None,
    )


async def describe_projects(
    client: BambuddyClient, *, model_project_id: int | None = None
) -> ProjectChoices:
    """Every project, with the folder it owns.

    The folders are read once and matched on ``project_id`` rather than asking
    ``/library/folders/by-project/{id}`` per project: that is one request instead of one
    per row, and the flat list already carries the link.
    """
    projects = await client.projects()
    folders = await client.folders()
    by_project: dict[int, Folder] = {}
    for folder in folders:
        if folder.project_id is not None:
            by_project.setdefault(folder.project_id, folder)
    return ProjectChoices(
        projects=[_view(project, by_project.get(project.id)) for project in projects],
        model_project_id=model_project_id,
    )


async def ensure_project(client: BambuddyClient, request: ProjectRequest) -> ProjectView:
    """Create or link a project, and make sure it has a library folder.

    Linking is idempotent on the folder: a project that already has one keeps it, so
    linking the same project twice does not leave Bambuddy with two folders of the same
    name pointing at it.
    """
    if request.project_id is not None:
        project = await client.project(request.project_id)
    else:
        if not request.name:
            raise ApiError(400, "a new project needs a name")
        project = await client.create_project(
            ProjectCreate(
                name=request.name,
                description=request.description,
                color=request.colour,
                tags=request.tags,
                url=request.url,
            )
        )

    folder = next(iter(await client.folders_by_project(project.id)), None)
    if folder is None and request.folder_id is not None:
        folder = next((row for row in await client.folders() if row.id == request.folder_id), None)
    if folder is None:
        folder = await client.create_folder(FolderCreate(name=project.name, project_id=project.id))
    return _view(project, folder)


async def folder_for(client: BambuddyClient, project_id: int) -> int | None:
    """The folder a project's sends belong in, or ``None`` if it has none.

    ``None`` is not an error: the caller falls back to the folder from Settings, which
    is where every send went before this issue.
    """
    folder = next(iter(await client.folders_by_project(project_id)), None)
    return folder.id if folder else None


class AttachResult(BaseModel):
    """What was attached, so the UI can say so rather than claiming more than happened."""

    project_id: int
    queue_item_ids: list[int] = Field(default_factory=list)
    archive_ids: list[int] = Field(default_factory=list)


async def attach_results(
    client: BambuddyClient, project_id: int, *, queue_item_ids: list[int]
) -> AttachResult:
    """Attach this output's queue entries, and any archives they have produced.

    Split from the run on purpose. ``jobs[].queue_entry_id`` is null when a pipeline run
    answers 202, and an archive only exists once a print has finished — so attaching at
    run time would attach nothing on one route and only half on the other. This is
    called once the ids are known, and attaching the same id twice is Bambuddy's
    problem to dedupe, not a reason to keep state here.
    """
    archives: list[int] = []
    for item_id in queue_item_ids:
        try:
            item = await client.queue_item(item_id)
        except ApiError as error:
            if error.status != 404:
                raise
            # Bambuddy drops a dispatched entry from the queue; its archive is then
            # already on the project by Bambuddy's own accounting.
            logger.info("a queue entry was gone before it could be attached", extra={"id": item_id})
            continue
        if item.archive_id is not None:
            archives.append(item.archive_id)

    if queue_item_ids:
        await client.add_queue_items_to_project(project_id, queue_item_ids)
    if archives:
        await client.add_archives_to_project(project_id, archives)
    return AttachResult(
        project_id=project_id, queue_item_ids=list(queue_item_ids), archive_ids=archives
    )
