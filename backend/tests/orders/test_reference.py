"""A line item's reference (#4249, spec 2026-10-18 §3.3, plan Rulings 1, 6, 7): checked
against the catalogue and the model's own history, inputs normalised against the
commit's schema, libraries and colours copied from the output it came from."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scadbuddy.core.gitrepo import RevisionNotFoundError
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.library.catalogue import Catalogue, ModelMeta
from scadbuddy.library.history import ModelHistory
from scadbuddy.library.outputs import INPUTS_NAME, META_NAME, OutputStore
from scadbuddy.orders.models import ColourSlot
from scadbuddy.orders.reference import (
    ReferenceError,
    TemplateDeletedError,
    colours_of,
    resolve_reference,
)
from scadbuddy.render.job_models import render_key
from scadbuddy.render.solids import WRAPPER_PREFIX
from tests.support.media import MemoryMediaStore

pytestmark = [pytest.mark.requires_git, pytest.mark.requires_postgres]

SOURCE = 'width = 10;\nlabel = "hi";\n'


class Library:
    """The catalogue, history and outputs a resolution reads, over one data dir."""

    def __init__(self, paths: DataPaths, settings: Settings) -> None:
        self.paths = paths
        self.config = settings.to_config()
        self.history = ModelHistory(paths.models, wrapper_prefix=WRAPPER_PREFIX)
        self.history.ensure_repo()
        self.catalogue = Catalogue(
            paths, self.history, media_store=MemoryMediaStore(), wrapper_prefix=WRAPPER_PREFIX
        )
        self.outputs = OutputStore(paths)

    def create(self, slug: str, name: str = "Demo") -> str:
        record = self.catalogue.create(slug, SOURCE, ModelMeta(name=name))
        assert record.version is not None
        return record.version

    def write_output(self, slug: str, output_id: str, commit: str | None, **meta: object) -> None:
        directory = self.paths.output_dir(slug, output_id)
        directory.mkdir(parents=True)
        record = {
            "id": output_id,
            "slug": slug,
            "model_version": commit,
            "job_id": "j" * 32,
            "created_at": datetime.now(UTC).isoformat(),
            "bbox_mm": {"min": [0, 0, 0], "max": [1, 1, 1], "size": [1, 1, 1]},
            "colors": ["#FF0000", "#00ff00"],
            "libraries": [
                {
                    "name": "BOSL2",
                    "url": "https://example.test/b.git",
                    "ref": "v1",
                    "commit": "b" * 40,
                }
            ],
            **meta,
        }
        (directory / META_NAME).write_text(json.dumps(record), encoding="utf-8")
        (directory / INPUTS_NAME).write_text(
            json.dumps({"params": {"width": 12}, "v": 1}), encoding="utf-8"
        )

    async def resolve(self, **kwargs: object) -> object:
        return await resolve_reference(
            self.catalogue,
            self.history,
            self.outputs,
            paths=self.paths,
            config=self.config,
            fetcher=None,
            **kwargs,  # type: ignore[arg-type]
        )


@pytest.fixture
def library(paths: DataPaths, settings: Settings) -> Library:
    return Library(paths, settings)


async def test_a_reference_is_checked_and_normalised(library: Library) -> None:
    commit = library.create("demo")
    resolved = await library.resolve(
        slug="demo", commit=commit, inputs={"params": {"width": 12}}, output_id=None
    )
    reference = resolved.reference  # type: ignore[attr-defined]
    assert (reference.slug, reference.commit) == ("demo", commit)
    assert reference.inputs == {"params": {"width": 12}, "v": 0}
    assert reference.template_name == "Demo"
    assert reference.libraries == []
    assert reference.render_key == render_key("demo", {"width": 12}, commit)
    # Nothing to take colours from without an output (Ruling 7): the quote fills them.
    assert resolved.colours == []  # type: ignore[attr-defined]


async def test_reference_from_an_output_copies_commit_libraries_and_colours(
    library: Library,
) -> None:
    commit = library.create("demo")
    library.write_output("demo", "out1", commit)
    resolved = await library.resolve(slug=None, commit=None, inputs=None, output_id="out1")
    reference = resolved.reference  # type: ignore[attr-defined]
    assert (reference.slug, reference.commit) == ("demo", commit)
    assert reference.inputs == {"params": {"width": 12}, "v": 1}
    assert [lib.name for lib in reference.libraries] == ["BOSL2"]
    assert resolved.colours == [  # type: ignore[attr-defined]
        ColourSlot(slot=0, colour="#FF0000"),
        ColourSlot(slot=1, colour="#00ff00"),
    ]
    assert colours_of(["#abcdef"]) == [ColourSlot(slot=0, colour="#abcdef")]
    # The body may name the slug, commit and inputs beside the output; they must agree.
    with pytest.raises(ReferenceError) as refused:
        await library.resolve(slug="other", commit=None, inputs=None, output_id="out1")
    assert refused.value.field == "output_id"
    with pytest.raises(ReferenceError) as refused:
        await library.resolve(slug=None, commit=None, inputs=None, output_id="missing")
    assert refused.value.field == "output_id"


async def test_a_commit_or_inputs_beside_an_output_must_be_the_outputs(
    library: Library, paths: DataPaths
) -> None:
    """#4458: the item keeps the output's colours and pointer, so a commit or inputs
    that describe another render are refused, not kept beside them."""
    commit = library.create("demo")
    paths.model_source("demo").write_text("width = 11;\n", encoding="utf-8")
    later = library.history.commit("Widen", "demo")
    assert later is not None and later != commit
    library.write_output("demo", "out1", commit)
    # The output's own, given back (a short commit, the same inputs) agree.
    agreed = await library.resolve(
        slug="demo", commit=commit[:7], inputs={"params": {"width": 12}, "v": 1}, output_id="out1"
    )
    assert agreed.reference.commit == commit  # type: ignore[attr-defined]
    with pytest.raises(ReferenceError) as refused:
        await library.resolve(slug=None, commit=later, inputs=None, output_id="out1")
    assert refused.value.field == "commit"
    assert "out1" in refused.value.problem
    with pytest.raises(ReferenceError) as refused:
        await library.resolve(
            slug=None, commit=None, inputs={"params": {"width": 20}}, output_id="out1"
        )
    assert refused.value.field == "inputs"
    assert "out1" in refused.value.problem
    # The output's params without its `v` (1 here) read as `v` 0: give them whole.
    with pytest.raises(ReferenceError) as refused:
        await library.resolve(
            slug=None, commit=None, inputs={"params": {"width": 12}}, output_id="out1"
        )
    assert refused.value.field == "inputs"
    assert "`v` included" in refused.value.problem


async def test_an_output_without_a_revision_cannot_be_ordered(library: Library) -> None:
    commit = library.create("demo")
    library.write_output("demo", "out2", None)
    with pytest.raises(ReferenceError) as refused:
        await library.resolve(slug=None, commit=None, inputs=None, output_id="out2")
    assert refused.value.field == "commit"
    # Nor pinned by a commit given beside it: no revision backs the output's colours.
    with pytest.raises(ReferenceError) as refused:
        await library.resolve(slug=None, commit=commit, inputs=None, output_id="out2")
    assert refused.value.field == "commit"
    assert "no revision" in refused.value.problem


async def test_an_output_whose_revision_is_gone_names_it(library: Library) -> None:
    commit = library.create("demo")
    library.write_output("demo", "out3", "0" * 40)
    with pytest.raises(ReferenceError) as refused:
        await library.resolve(slug=None, commit=commit, inputs=None, output_id="out3")
    assert refused.value.field == "commit"
    assert "0" * 40 in refused.value.problem
    assert "no longer has" in refused.value.problem
    # The same words when the output's commit stands in for one left out, or is
    # given back, whole or short (#4484).
    for given in (None, "0" * 40, "0" * 7):
        with pytest.raises(ReferenceError) as refused:
            await library.resolve(slug=None, commit=given, inputs=None, output_id="out3")
        assert refused.value.field == "commit", given
        assert "out3" in refused.value.problem, given
        assert "no longer has" in refused.value.problem, given


async def test_an_ambiguous_short_id_of_a_live_output_revision_is_not_gone(
    library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A short id `rev-parse` refuses as ambiguous: the output's revision is still there."""
    commit = library.create("demo")
    library.write_output("demo", "out5", commit)
    resolve = library.history.resolve

    def ambiguous(given: str) -> str:
        if len(given) < 40:
            raise RevisionNotFoundError(given)
        return resolve(given)

    monkeypatch.setattr(library.history, "resolve", ambiguous)
    with pytest.raises(ReferenceError) as refused:
        await library.resolve(slug=None, commit=commit[:7], inputs=None, output_id="out5")
    assert refused.value.field == "commit"
    assert "no longer has" not in refused.value.problem


async def test_an_outputs_unreadable_inputs_are_named_as_its_own(
    library: Library, paths: DataPaths
) -> None:
    """#4484: the fault is the output's stored inputs, not a mismatch with the caller's."""
    commit = library.create("demo")
    library.write_output("demo", "out4", commit)
    (paths.output_dir("demo", "out4") / INPUTS_NAME).write_text(
        json.dumps({"params": 3}), encoding="utf-8"
    )
    for given in (None, {"params": {"width": 12}, "v": 1}):
        with pytest.raises(ReferenceError) as refused:
            await library.resolve(slug=None, commit=None, inputs=given, output_id="out4")
        assert refused.value.field == "inputs", given
        assert "stored inputs" in refused.value.problem, given
        assert "out4" in refused.value.problem, given


async def test_a_commit_of_another_model_is_refused(library: Library) -> None:
    library.create("demo")
    other = library.create("other", name="Other")
    # `other`'s commit resolves in the one repository, but never touched `demo`.
    with pytest.raises(ReferenceError) as refused:
        await library.resolve(slug="demo", commit=other, inputs={}, output_id=None)
    assert refused.value.field == "commit"
    assert "demo" in refused.value.problem


async def test_a_short_commit_is_stored_full(library: Library) -> None:
    commit = library.create("demo")
    resolved = await library.resolve(slug="demo", commit=commit[:7], inputs={}, output_id=None)
    assert resolved.reference.commit == commit  # type: ignore[attr-defined]
    # A prefix of nothing, and a shape that is not a commit at all.
    for bad in ("0" * 7, "HEAD", "main", "xyz"):
        with pytest.raises(ReferenceError) as refused:
            await library.resolve(slug="demo", commit=bad, inputs={}, output_id=None)
        assert refused.value.field == "commit", bad


async def test_an_unknown_slug_is_refused(library: Library) -> None:
    with pytest.raises(ReferenceError) as refused:
        await library.resolve(slug="nothing", commit="a" * 40, inputs={}, output_id=None)
    assert refused.value.field == "slug"


async def test_a_deleted_template_is_refused(library: Library) -> None:
    commit = library.create("demo")
    library.catalogue.delete("demo")
    with pytest.raises(TemplateDeletedError) as refused:
        await library.resolve(slug="demo", commit=commit, inputs={}, output_id=None)
    assert refused.value.slug == "demo"


async def test_inputs_are_normalised_against_the_commit(library: Library) -> None:
    commit = library.create("demo")
    with pytest.raises(ReferenceError) as refused:
        await library.resolve(
            slug="demo", commit=commit, inputs={"params": {"depth": 3}}, output_id=None
        )
    assert refused.value.field == "inputs"
    assert "depth" in refused.value.problem
    with pytest.raises(ReferenceError) as refused:
        await library.resolve(
            slug="demo", commit=commit, inputs={"params": {"width": "wide"}}, output_id=None
        )
    assert refused.value.field == "inputs"
    with pytest.raises(ReferenceError) as refused:
        await library.resolve(slug="demo", commit=commit, inputs={"params": 3}, output_id=None)
    assert refused.value.field == "inputs"


async def test_a_later_revision_keeps_the_earlier_schema(
    library: Library, paths: DataPaths
) -> None:
    first = library.create("demo")
    # The second revision drops `label`; an item pinned at the first still takes it.
    paths.model_source("demo").write_text("width = 10;\n%%RANGED%%\n", encoding="utf-8")
    second = library.history.commit("Narrow the schema", "demo")
    assert second is not None and second != first
    resolved = await library.resolve(
        slug="demo", commit=first, inputs={"params": {"label": "x"}}, output_id=None
    )
    assert resolved.reference.commit == first  # type: ignore[attr-defined]
    with pytest.raises(ReferenceError) as refused:
        await library.resolve(
            slug="demo", commit=second, inputs={"params": {"label": "x"}}, output_id=None
        )
    assert refused.value.field == "inputs"
    assert Path(paths.model_source("demo")).read_text(encoding="utf-8").startswith("width")
