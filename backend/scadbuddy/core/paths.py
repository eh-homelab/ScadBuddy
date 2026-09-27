from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from scadbuddy.core.fontconfig import fonts_dir

SOURCE_NAME = "model.scad"
#: The model's own metadata. Since #90 it is NOT the schema cache -- see below.
MODEL_META_NAME = "model.json"
# The DERIVED customizer schema. Never `model.json` and never inside `models/`:
# it is written lazily by the first render or schema read, outside any commit,
# so keeping it in the versioned tree would leave the repository permanently
# dirty and fold a cache blob into the next unrelated metadata commit.
SCHEMA_CACHE_NAME = "schema.json"
#: Presets a template ships with, beside its source: read-only through the API. The
#: presets people save are not kept here -- see :meth:`DataPaths.model_presets`.
TEMPLATE_PRESETS_NAME = "presets.json"
#: Where the built-in templates are mirrored from the image, inside the models
#: repository. Slugs are `[a-z0-9-]`, so it can never be one.
BUILTIN_DIR = "_builtin"
#: A built-in's id is this plus its slug. `:` is not a slug character either, so a
#: built-in and a template of mine can share a slug, and a bare slug keeps meaning
#: what it always has. Derived files (outputs, caches) are keyed by the id.
BUILTIN_PREFIX = "builtin:"


def is_builtin(model_id: str) -> bool:
    return model_id.startswith(BUILTIN_PREFIX)


def model_path(model_id: str) -> str:
    """A template's directory relative to ``models/`` -- which is also its path in git."""
    if is_builtin(model_id):
        return f"{BUILTIN_DIR}/{model_id.removeprefix(BUILTIN_PREFIX)}"
    return model_id


@dataclass(frozen=True)
class DataPaths:
    root: Path

    @property
    def models(self) -> Path:
        return self.root / "models"

    @property
    def outputs(self) -> Path:
        return self.root / "outputs"

    @property
    def jobs(self) -> Path:
        return self.root / "jobs"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def fonts(self) -> Path:
        return fonts_dir(self.root)

    @property
    def libraries(self) -> Path:
        """Third-party OpenSCAD library checkouts (#93). Not under ``models/``: each
        model pins the ones it uses in its ``model.json``; they are not versioned
        themselves."""
        return self.root / "libraries"

    @property
    def assets(self) -> Path:
        """Files attached to `// file` parameters (#204), content-addressed. Not
        under ``models/`` (they belong to renders, not to a template's history)
        and not under ``cache/`` (an output's parameters name them for good)."""
        return self.root / "assets"

    @property
    def presets(self) -> Path:
        """The presets people save, one file per template. Not under ``models/``: a
        built-in's directory is the image's and only the boot sync writes it, and a
        saved preset is not a change to the template, so it must not move the
        template's revision (which outputs are stamped with, and duplicates track)."""
        return self.root / "presets"

    def model_presets(self, slug: str) -> Path:
        return self.presets / f"{slug}.json"

    @property
    def builtins(self) -> Path:
        return self.models / BUILTIN_DIR

    def model_dir(self, slug: str) -> Path:
        return self.models / model_path(slug)

    def model_source(self, slug: str) -> Path:
        return self.model_dir(slug) / SOURCE_NAME

    def model_meta(self, slug: str) -> Path:
        return self.model_dir(slug) / MODEL_META_NAME

    @property
    def schema_cache(self) -> Path:
        return self.cache / "schema"

    def model_schema_cache(self, slug: str) -> Path:
        """Where the live model's derived schema is cached -- under ``cache/``,
        for the reason on :data:`SCHEMA_CACHE_NAME`."""
        return self.schema_cache / f"{slug}.json"

    @property
    def previews(self) -> Path:
        """Default-render previews: the catalogue thumbnail of a model with no image
        of its own and no generated output (#179 follow-up). Derived, like the
        schema cache, so under ``cache/`` and never in the models repository."""
        return self.cache / "previews"

    def model_preview(self, slug: str) -> Path:
        return self.previews / f"{slug}.png"

    def model_preview_record(self, slug: str) -> Path:
        """What the preview was rendered from, or that rendering it failed."""
        return self.previews / f"{slug}.json"

    @property
    def tombstones(self) -> Path:
        """Where a deleted model's directory waits for its ``rmtree`` -- outside
        ``models/``, so the listing and git never see a half-deleted model."""
        return self.cache / "tombstones"

    @property
    def model_revisions(self) -> Path:
        return self.cache / "revisions"

    def model_revision_dir(self, slug: str, commit: str) -> Path:
        """An old revision of a model, exported out of git.

        Outside ``models/`` on purpose: it is derived, it must not be versioned,
        and being an ordinary model directory means the schema cache and the
        renderer work on it unchanged. Commits are immutable, so once populated an
        entry never needs invalidating.
        """
        return self.model_revisions / slug / commit

    def output_dir(self, slug: str, output_id: str) -> Path:
        return self.outputs / slug / output_id

    def job_file(self, job_id: str) -> Path:
        return self.jobs / f"{job_id}.json"

    def job_work_dir(self, job_id: str) -> Path:
        return self.jobs / f"{job_id}.work"

    def ensure(self) -> None:
        for directory in (
            self.models,
            self.outputs,
            self.jobs,
            self.cache,
            self.fonts,
            self.libraries,
            self.assets,
            self.presets,
        ):
            directory.mkdir(parents=True, exist_ok=True)
