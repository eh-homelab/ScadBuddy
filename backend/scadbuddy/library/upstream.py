"""Taking a duplicate's upstream updates (#157): detect, preview, three-way merge.

A duplicate records the upstream revision it includes as ``base``. When the
upstream's current revision is neither ``base`` nor the one the user
``dismissed``, there is an update, and ``git merge-file`` merges it into this
template's own edits with ``base`` as the merge base -- exactly what a merge
between two branches would do, without a branch per duplicate.

Only ``model.scad`` is merged. Every other file follows the upstream when this
template has not changed it since ``base``, and is otherwise kept and listed;
``model.json`` is always this template's own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from scadbuddy.core.paths import BUILTIN_PREFIX, MODEL_META_NAME, SOURCE_NAME, model_path
from scadbuddy.library.history import ModelHistory, RevisionNotFoundError

UpstreamState = Literal["current", "update", "dismissed", "gone"]

#: A line git's conflict markers begin, as `merge-file --diff3` writes them.
_MARKER = re.compile(r"^(<{7}|\|{7}|={7}|>{7})(?: |$)", re.MULTILINE)


class Upstream(BaseModel):
    """The template a duplicate was copied from, and the revision of it it includes."""

    id: str = Field(description="The upstream template's id: a slug, or `builtin:<slug>`")
    #: Stored, not derived from `id`: a merge base may name a commit where the
    #: upstream's source lived somewhere else.
    path: str = Field(description="The upstream's directory in the models repository")
    base: str | None = Field(
        description="The upstream commit this template includes; None without history"
    )
    dismissed: str | None = Field(
        default=None, description="An upstream commit the user chose not to take"
    )


class NoUpstreamError(LookupError):
    """The template is not a duplicate, so it has no upstream."""


class UpstreamStateError(ValueError):
    """The action does not apply to the upstream's current state."""

    def __init__(self, message: str, state: UpstreamState) -> None:
        super().__init__(message)
        self.state = state


class InvalidMergeBaseError(ValueError):
    """A ``merge_base`` that names no revision of the upstream."""


class MergeConflictError(Exception):
    """The merge left conflicts: nothing was written. ``state`` is where the template
    stood against its upstream when the merge was worked out."""

    def __init__(self, plan: MergePlan, state: UpstreamState) -> None:
        super().__init__(f"{plan.conflicts} conflicting hunk(s)")
        self.plan = plan
        self.state = state


def state_of(upstream: Upstream, *, exists: bool, revision: str | None) -> UpstreamState:
    """Where a duplicate stands against its upstream's current ``revision``.

    An upstream that exists but has no revision (its commit failed) offers nothing,
    so it reads as current rather than as gone.
    """
    if not exists:
        return "gone"
    if revision is None or revision == upstream.base:
        return "current"
    if revision == upstream.dismissed:
        return "dismissed"
    return "update"


def has_conflict_markers(source: str) -> bool:
    return _MARKER.search(source) is not None


class MergePreview(BaseModel):
    ours: str = Field(description="This template's `model.scad`")
    base: str = Field(description="The upstream's `model.scad` at `base`")
    theirs: str = Field(description="The upstream's current `model.scad`")
    patch: str = Field(
        description="The upstream's own changes since `base`: a unified patch from its "
        "directory at `base` (at `upstream.path`) to its current one, `model.json` aside"
    )
    merged: str = Field(
        description="`git merge-file -p --diff3 ours base theirs`: conflict markers when not clean"
    )
    clean: bool
    taken: list[str] = Field(
        default_factory=list,
        description="Other files that follow the upstream: unchanged here since `base`",
    )
    kept: list[str] = Field(
        default_factory=list,
        description="Other files changed both here and upstream since `base`: ours stay",
    )


class UpstreamStatus(BaseModel):
    state: UpstreamState
    upstream: Upstream
    revision: str | None = Field(
        description="The upstream's current revision; None when it is gone"
    )
    preview: MergePreview | None = Field(
        default=None,
        description="The merge a `POST …/upstream/merge` would make; on update or dismissed",
    )


@dataclass
class MergePlan:
    """A merge worked out but not yet written. ``files`` maps a path relative to the
    template to the upstream's bytes, or ``None`` where the upstream removed it;
    ``local`` is every file of the template's the plan was read from, as it read it."""

    revision: str
    preview: MergePreview
    conflicts: int
    files: dict[str, bytes | None] = field(default_factory=dict)
    local: dict[str, bytes | None] = field(default_factory=dict)

    def still_applies(self, directory: Path) -> bool:
        """Whether the template in ``directory`` still reads as this plan found it."""
        return all(_local(directory / name) == content for name, content in self.local.items())


def _local(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


def _read(history: ModelHistory, commit: str | None, path: str) -> bytes | None:
    if commit is None:
        return None
    try:
        return history.show(commit, path)
    except RevisionNotFoundError:
        return None


def plan_merge(
    history: ModelHistory, slug: str, directory: Path, upstream: Upstream, revision: str
) -> MergePlan:
    """Merge ``upstream`` at ``revision`` into the template in ``directory``.

    ``base`` is read from ``upstream.path`` -- where the source lived at that
    revision -- and ``theirs`` from where the upstream lives now.
    """
    theirs_path = model_path(upstream.id)
    # All three sides as bytes, decoded alike: no newline translation, so a CRLF
    # source compares line for line and keeps its line endings.
    ours_bytes = (directory / SOURCE_NAME).read_bytes()
    local: dict[str, bytes | None] = {SOURCE_NAME: ours_bytes}
    ours = ours_bytes.decode()
    base = (_read(history, upstream.base, f"{upstream.path}/{SOURCE_NAME}") or b"").decode()
    theirs = (_read(history, revision, f"{theirs_path}/{SOURCE_NAME}") or b"").decode()
    base_label = f"{upstream.id}@{upstream.base[:7]}" if upstream.base else "base"
    merged, conflicts = history.merge_file(
        ours, base, theirs, labels=(slug, base_label, upstream.id)
    )

    names = set(history.files_at(revision, theirs_path))
    if upstream.base is not None:
        names |= set(history.files_at(upstream.base, upstream.path))
    names -= {SOURCE_NAME, MODEL_META_NAME}
    files: dict[str, bytes | None] = {}
    kept: list[str] = []
    for name in sorted(names):
        at_base = _read(history, upstream.base, f"{upstream.path}/{name}")
        at_theirs = _read(history, revision, f"{theirs_path}/{name}")
        if at_theirs == at_base:
            continue
        at_ours = local[name] = _local(directory / name)
        if at_ours == at_theirs:
            continue
        if at_ours == at_base:
            files[name] = at_theirs
        else:
            kept.append(name)

    patch = history.diff_dirs(
        upstream.base,
        upstream.path,
        revision,
        theirs_path,
        label=upstream.id.removeprefix(BUILTIN_PREFIX),
        exclude=(MODEL_META_NAME,),
    )
    preview = MergePreview(
        ours=ours,
        base=base,
        theirs=theirs,
        patch=patch,
        merged=merged,
        clean=conflicts == 0,
        taken=sorted(files),
        kept=kept,
    )
    return MergePlan(
        revision=revision, preview=preview, conflicts=conflicts, files=files, local=local
    )
