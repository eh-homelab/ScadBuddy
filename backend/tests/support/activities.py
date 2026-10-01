"""A render worker's deps over one demo template, for the activity and worker-cache tests."""

from __future__ import annotations

from pathlib import Path

from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.assets import AssetStore
from scadbuddy.render.projection import JobProjection
from scadbuddy.store import BlobRefs
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.workflows.activities import WorkerDeps
from scadbuddy.workflows.models import PieceRequest, piece_key
from tests.conftest import fake_3mf_openscad

REVISION = "c0ffee0"


class History:
    """A repository whose every template was last committed at `REVISION`."""

    available = True

    def last_commit(self, path: str) -> str:
        return REVISION


def demo_paths(tmp_path: Path, source: str = "cube();\n") -> DataPaths:
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    paths.model_dir("demo").mkdir(parents=True)
    paths.model_source("demo").write_text(source, encoding="utf-8")
    return paths


def worker_deps(
    tmp_path: Path,
    paths: DataPaths,
    *,
    projection: JobProjection | None = None,
    refs: BlobRefs | None = None,
) -> WorkerDeps:
    return WorkerDeps(
        config=Config(openscad=fake_3mf_openscad(tmp_path / "bin"), data_dir=paths.root),
        paths=paths,
        assets=AssetStore(paths.assets),
        blobs=LocalBlobStore(paths.blobs),
        refs=refs,  # type: ignore[arg-type]
        projection=projection,  # type: ignore[arg-type]
        history=History(),  # type: ignore[arg-type]
    )


def piece_request(revision: str | None = REVISION) -> PieceRequest:
    params = {"width": 12}
    # A piece with no revision is its job's own (#642, #870): it takes a scope.
    scope = None if revision is not None else "job:test"
    return PieceRequest(
        slug="demo",
        revision=revision,
        scope=scope,
        params=dict(params),
        piece_key=piece_key(
            "demo", revision if revision is not None else scope, "model.scad", params
        ),
    )
