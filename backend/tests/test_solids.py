from __future__ import annotations

import asyncio
import os
import shutil
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

import pytest
import trimesh
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from scadbuddy.core.config import Config, load_config
from scadbuddy.render import solids as solids_module
from scadbuddy.render.colours import CSS_COLOURS
from scadbuddy.render.runner import OpenSCADError, ProcessOutput
from scadbuddy.render.schema import CustomizerSchema, ParamValue
from scadbuddy.render.solids import (
    SPLIT_FALLBACK,
    WRAPPER_PREFIX,
    render_solids,
    targets_for,
    wrapper_source,
)
from scadbuddy.render.split import ColourPart
from tests.conftest import FIXTURES

#: 28 distinct colours: the most an AMS template can declare (#282).
MANY_COLOURS = [f"#0000{index:02X}" for index in range(1, 29)]


def test_css_table_is_openscads_own_colour_list() -> None:
    assert len(CSS_COLOURS) == 147
    assert CSS_COLOURS["red"] == "#FF0000"
    assert CSS_COLOURS["gray"] == CSS_COLOURS["grey"] == "#808080"
    assert CSS_COLOURS["hotpink"] == "#FF69B4"
    assert all(value == value.upper() and len(value) == 7 for value in CSS_COLOURS.values())


def test_targets_cover_every_way_a_model_can_write_the_colour() -> None:
    assert targets_for("#ff0000") == ["#FF0000", "name:red"]
    assert targets_for("#808080") == ["#808080", "name:gray", "name:grey"]
    assert targets_for("#123456") == ["#123456"]


def test_wrapper_shadows_color_and_includes_the_model() -> None:
    source = wrapper_source("model.scad")
    assert "module color(c, alpha = 1)" in source
    assert source.endswith("include <model.scad>\n")


@pytest.mark.requires_openscad
async def test_named_and_vector_colours_each_render_as_a_closed_solid(tmp_path: Path) -> None:
    model = tmp_path / "named_colours.scad"
    shutil.copy(FIXTURES / "named_colours.scad", model)
    work = tmp_path / "work"
    work.mkdir()

    solids = await render_solids(
        model, CustomizerSchema(), {}, ["#FF0000", "#0080FF"], work, config=load_config()
    )

    assert solids.warnings == []
    assert sorted(solids.meshes) == ["#0080FF", "#FF0000"]
    assert all(mesh.is_watertight for mesh in solids.meshes.values())
    assert round(float(solids.meshes["#FF0000"].volume), 3) == 1000.0
    assert round(float(solids.meshes["#0080FF"].volume), 3) == 125.0


@pytest.mark.requires_openscad
async def test_a_colour_the_model_never_uses_is_reported_not_raised(tmp_path: Path) -> None:
    model = tmp_path / "named_colours.scad"
    shutil.copy(FIXTURES / "named_colours.scad", model)
    work = tmp_path / "work"
    work.mkdir()

    solids = await render_solids(
        model, CustomizerSchema(), {}, ["#ABCDEF"], work, config=load_config()
    )

    assert solids.meshes == {}
    assert len(solids.warnings) == 1
    assert solids.warnings[0].startswith("#ABCDEF: no closed solid")


@pytest.mark.requires_openscad
async def test_a_colour_openscad_cannot_export_falls_back_to_its_split_mesh(
    tmp_path: Path,
) -> None:
    """#952: spinning-top's stem, alone in its colour, is a PolySet OpenSCAD cannot
    write as a 3MF. It exits 0 with an empty file: a fallback, not a BadZipFile."""
    model = tmp_path / "lone_lathe.scad"
    shutil.copy(FIXTURES / "lone_lathe.scad", model)
    work = tmp_path / "work"
    work.mkdir()

    solids = await render_solids(
        model, CustomizerSchema(), {}, ["#E53935", "#1E88E5"], work, config=load_config()
    )

    assert sorted(solids.meshes) == ["#1E88E5"]
    assert solids.warnings == [
        "#E53935: no closed solid (openscad could not export: Can't add triangle to 3MF"
        f" model.); {SPLIT_FALLBACK}"
    ]


@pytest.mark.requires_openscad
async def test_the_wrapper_is_removed_from_the_model_directory(tmp_path: Path) -> None:
    model = tmp_path / "named_colours.scad"
    shutil.copy(FIXTURES / "named_colours.scad", model)
    work = tmp_path / "work"
    work.mkdir()

    await render_solids(model, CustomizerSchema(), {}, ["#FF0000"], work, config=load_config())

    assert sorted(p.name for p in tmp_path.iterdir()) == ["named_colours.scad", "work"]


# Bounded concurrency for the per-colour wrapper renders (#282).


def _colour_of(extra_defines: Sequence[str]) -> str:
    """The colour a wrapper render was asked for, read back off its ``-D``."""
    return extra_defines[1].split('"')[1]


def _box(size: int) -> list[ColourPart]:
    """A split result whose volume says which render it came from."""
    return [ColourPart(1, "part", "#000000", trimesh.creation.box(extents=(size, 1, 1)))]


def _solid_index(path: Path) -> int:
    return int(path.stem.removeprefix("solid_"))


class FakeRenders:
    """Stands in for `render_3mf`: each colour's render takes ``delays[colour]`` (or
    ``delay``) seconds, and the peak number running at once is recorded."""

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay
        self.delays: dict[str, float] = {}
        self.fail: dict[str, BaseException] = {}
        self.active = 0
        self.peak = 0
        self.started: list[str] = []
        self.cancelled: list[str] = []
        #: When set, each render waits here instead of sleeping, until this many have
        #: started; the last to start wakes them all in one go.
        self.together = 0
        self._all_started = asyncio.Event()

    async def render_3mf(
        self,
        scad_path: Path,
        schema: CustomizerSchema,
        params: Mapping[str, ParamValue],
        out_path: Path,
        *,
        config: Config,
        extra_defines: Sequence[str] = (),
        failure_is_fallback: bool = False,
    ) -> ProcessOutput:
        assert failure_is_fallback
        colour = _colour_of(extra_defines)
        assert scad_path.name.startswith(WRAPPER_PREFIX)
        assert scad_path.is_file()
        self.started.append(colour)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if self.together:
                if len(self.started) == self.together:
                    self._all_started.set()
                await self._all_started.wait()
            else:
                await asyncio.sleep(self.delays.get(colour, self.delay))
            if colour in self.fail:
                raise self.fail[colour]
        except asyncio.CancelledError:
            self.cancelled.append(colour)
            raise
        finally:
            self.active -= 1
        return ProcessOutput(returncode=0, log_tail=[], duration_s=0.0)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeRenders:
    renders = FakeRenders()
    monkeypatch.setattr(solids_module, "render_3mf", renders.render_3mf)
    monkeypatch.setattr(solids_module, "split_by_material", lambda p: _box(_solid_index(p)))
    return renders


def _model(tmp_path: Path) -> tuple[Path, Path]:
    model = tmp_path / "model.scad"
    model.write_text("cube(1);\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    return model, work


async def test_solids_come_back_in_colour_order_whatever_order_they_finish_in(
    fake: FakeRenders, tmp_path: Path
) -> None:
    model, work = _model(tmp_path)
    colours = MANY_COLOURS[:6]
    # The first colour is the slowest and the last the fastest: they finish in the
    # reverse of the order they were asked for.
    fake.delays = {colour: 0.02 * (len(colours) - rank) for rank, colour in enumerate(colours)}
    fake.fail[colours[2]] = OpenSCADError("openscad exited with 1", [])

    result = await render_solids(
        model, CustomizerSchema(), {}, colours, work, config=Config(solid_concurrency=6)
    )

    assert fake.peak == 6
    assert list(result.meshes) == [c for c in colours if c != colours[2]]
    assert [round(float(m.volume)) for m in result.meshes.values()] == [1, 2, 4, 5, 6]
    # An OpenSCAD failure is still that colour's fallback (spec §6.3), not the job's.
    assert result.warnings == [
        f"{colours[2]}: no closed solid (openscad exited with 1); {SPLIT_FALLBACK}"
    ]


@pytest.mark.parametrize("slots", [1, 3, 4])
async def test_no_more_than_the_configured_colours_render_at_once(
    slots: int, fake: FakeRenders, tmp_path: Path
) -> None:
    model, work = _model(tmp_path)

    result = await render_solids(
        model, CustomizerSchema(), {}, MANY_COLOURS, work, config=Config(solid_concurrency=slots)
    )

    assert fake.peak == slots
    assert list(result.meshes) == MANY_COLOURS
    assert result.warnings == []


async def test_a_failing_colour_fails_the_render_and_cancels_its_siblings(
    fake: FakeRenders, tmp_path: Path
) -> None:
    model, work = _model(tmp_path)
    colours = MANY_COLOURS[:8]
    fake.delays = dict.fromkeys(colours, 30.0)
    fake.delays[colours[1]] = 0.05
    fake.fail[colours[1]] = RuntimeError("the solid's 3MF is corrupt")

    started = time.monotonic()
    # The error the job records is the colour's own, not an ExceptionGroup's.
    with pytest.raises(RuntimeError, match="the solid's 3MF is corrupt"):
        async with asyncio.timeout(10):
            await render_solids(
                model, CustomizerSchema(), {}, colours, work, config=Config(solid_concurrency=4)
            )

    assert time.monotonic() - started < 5
    # The three siblings in flight were cancelled; the four waiting never started.
    assert fake.started == colours[:4]
    assert sorted(fake.cancelled) == [colours[0], colours[2], colours[3]]
    assert fake.active == 0
    assert not list(tmp_path.glob(f"{WRAPPER_PREFIX}*"))


async def test_a_second_colour_failing_at_once_is_logged_not_lost(
    fake: FakeRenders, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    model, work = _model(tmp_path)
    colours = MANY_COLOURS[:2]
    fake.fail = {colours[0]: RuntimeError("first broke"), colours[1]: RuntimeError("second broke")}
    # Both raise in the same turn of the event loop, before the TaskGroup can cancel
    # either. Two equal sleeps did that only while their timers fired together; under
    # load the first failure cancelled the second before it raised (#1617).
    fake.together = len(colours)

    with pytest.raises(RuntimeError, match="broke") as raised:
        await render_solids(
            model, CustomizerSchema(), {}, colours, work, config=Config(solid_concurrency=2)
        )

    logged = [r.exc_info[1] for r in caplog.records if r.exc_info is not None]
    assert len(logged) == 1
    assert {str(raised.value), str(logged[0])} == {"first broke", "second broke"}


async def test_the_solid_mesh_is_parsed_off_the_event_loop(
    fake: FakeRenders, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model, work = _model(tmp_path)
    threads: list[int] = []

    def split(path: Path) -> list[ColourPart]:
        threads.append(threading.get_ident())
        # Two parts, so the concatenate runs in the same thread too.
        return _box(1) + _box(2)

    monkeypatch.setattr(solids_module, "split_by_material", split)

    result = await render_solids(
        model, CustomizerSchema(), {}, MANY_COLOURS[:3], work, config=Config(solid_concurrency=3)
    )

    assert len(threads) == 3
    assert threading.get_ident() not in threads
    assert [round(float(m.volume)) for m in result.meshes.values()] == [3, 3, 3]


async def test_a_many_colour_model_renders_in_a_fraction_of_the_sequential_time(
    fake: FakeRenders, tmp_path: Path
) -> None:
    model, work = _model(tmp_path)
    fake.delay = 0.1
    sequential = fake.delay * len(MANY_COLOURS)

    started = time.monotonic()
    result = await render_solids(
        model, CustomizerSchema(), {}, MANY_COLOURS, work, config=Config(solid_concurrency=4)
    )
    elapsed = time.monotonic() - started

    assert list(result.meshes) == MANY_COLOURS
    # Seven rounds of four is 0.7 s; one at a time, as before #282, it was 2.8 s.
    assert elapsed < sequential / 2


def _fake_openscad(tmp_path: Path, body: str) -> str:
    binary = tmp_path / "fake-openscad"
    binary.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    binary.chmod(0o755)
    return str(binary)


async def test_a_colour_waiting_for_a_slot_is_not_charged_against_its_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The real runner: each wrapper render gets the whole `render_timeout` from the
    moment its own process starts, so five 0.4 s renders one at a time -- 2 s in all
    -- each fit a 1 s timeout."""
    monkeypatch.setattr(solids_module, "split_by_material", lambda p: _box(_solid_index(p)))
    model, work = _model(tmp_path)
    colours = MANY_COLOURS[:5]
    config = Config(
        openscad=_fake_openscad(tmp_path, "sleep 0.4"),
        data_dir=tmp_path / "data",
        render_timeout=1.0,
        solid_concurrency=1,
    )

    result = await render_solids(model, CustomizerSchema(), {}, colours, work, config=config)

    assert result.warnings == []
    assert list(result.meshes) == colours


async def test_each_colour_is_a_solid_span_with_its_export_beneath(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spans: InMemorySpanExporter
) -> None:
    monkeypatch.setattr(solids_module, "split_by_material", lambda p: _box(_solid_index(p)))
    model, work = _model(tmp_path)
    config = Config(openscad=_fake_openscad(tmp_path, "exit 0"), data_dir=tmp_path / "data")

    await render_solids(model, CustomizerSchema(), {}, MANY_COLOURS[:2], work, config=config)

    finished = spans.get_finished_spans()
    solid_spans = [s for s in finished if s.name == "render.solid"]
    assert sorted((s.attributes or {})["scadbuddy.colour_index"] for s in solid_spans) == [1, 2]
    exports = [s for s in finished if s.name == "openscad.export"]
    assert len(exports) == 2
    for solid_span in solid_spans:
        assert solid_span.context is not None
        assert [
            e
            for e in exports
            if e.parent is not None and e.parent.span_id == solid_span.context.span_id
        ]


class _ParseError(Exception):
    pass


async def test_a_colours_mesh_parse_is_inside_its_solid_span_and_fails_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spans: InMemorySpanExporter
) -> None:
    # Review 5 of #1064: the parse is the colour's work, so its time and its failure
    # (a real job failure, not a fallback) belong to that colour's span.
    def split(path: Path) -> list[ColourPart]:
        if _solid_index(path) == 2:
            raise _ParseError
        return _box(_solid_index(path))

    monkeypatch.setattr(solids_module, "split_by_material", split)
    model, work = _model(tmp_path)
    config = Config(openscad=_fake_openscad(tmp_path, "exit 0"), data_dir=tmp_path / "data")

    with pytest.raises(_ParseError):
        await render_solids(model, CustomizerSchema(), {}, MANY_COLOURS[:2], work, config=config)

    by_index = {
        (s.attributes or {})["scadbuddy.colour_index"]: s
        for s in spans.get_finished_spans()
        if s.name == "render.solid"
    }
    failed = by_index[2]
    assert failed.status.status_code is StatusCode.ERROR
    assert (failed.attributes or {})["scadbuddy.failure_class"] == "_ParseError"
    assert "scadbuddy.openscad.exit_code" not in (failed.attributes or {})


async def test_a_colour_that_times_out_falls_back_without_failing_its_siblings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spans: InMemorySpanExporter
) -> None:
    monkeypatch.setattr(solids_module, "split_by_material", lambda p: _box(_solid_index(p)))
    model, work = _model(tmp_path)
    colours = MANY_COLOURS[:3]
    config = Config(
        openscad=_fake_openscad(tmp_path, f"case \"$*\" in *'{colours[1]}'*) exec sleep 30;; esac"),
        data_dir=tmp_path / "data",
        render_timeout=0.5,
        solid_concurrency=3,
    )

    result = await render_solids(model, CustomizerSchema(), {}, colours, work, config=config)

    assert list(result.meshes) == [colours[0], colours[2]]
    assert result.warnings == [
        f"{colours[1]}: no closed solid (openscad timed out after 0.5s); {SPLIT_FALLBACK}"
    ]
    # #1134: the export says why the colour fell back, and still ends UNSET.
    exports = [s for s in spans.get_finished_spans() if s.name == "openscad.export"]
    timed_out = [
        s
        for s in exports
        if (s.attributes or {}).get("scadbuddy.failure_class") == "RenderTimeoutError"
    ]
    assert len(timed_out) == 1
    assert {s.status.status_code for s in exports} == {StatusCode.UNSET}


async def test_cancelled_siblings_leave_no_openscad_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The real runner: when one colour fails the job, the `openscad` processes of
    the colours still rendering are killed, not left holding the CPU."""
    pids = tmp_path / "pids"
    pids.touch()
    colours = MANY_COLOURS[:4]
    # The first colour waits until its three siblings are running, then exits and
    # its split fails; the siblings would sleep for 30 s.
    body = (
        f"case \"$*\" in *'{colours[0]}'*)\n"
        f'  while [ "$(wc -l < {pids})" -lt 3 ]; do sleep 0.05; done; exit 0;;\n'
        "esac\n"
        f"echo $$ >> {pids}\n"
        "exec sleep 30"
    )

    def split(path: Path) -> list[ColourPart]:
        raise RuntimeError(f"cannot read {path.name}")

    monkeypatch.setattr(solids_module, "split_by_material", split)
    model, work = _model(tmp_path)
    config = Config(
        openscad=_fake_openscad(tmp_path, body),
        data_dir=tmp_path / "data",
        render_timeout=20.0,
        solid_concurrency=4,
    )

    with pytest.raises(RuntimeError, match=r"cannot read solid_1\.3mf"):
        async with asyncio.timeout(10):
            await render_solids(model, CustomizerSchema(), {}, colours, work, config=config)

    running = [int(line) for line in pids.read_text(encoding="utf-8").split()]
    assert len(running) == 3
    for pid in running:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert not list(tmp_path.glob(f"{WRAPPER_PREFIX}*"))


@pytest.mark.requires_openscad
async def test_a_sixteen_colour_model_renders_the_same_solids_at_any_concurrency(
    tmp_path: Path,
) -> None:
    """dollhouse-kit's window piece has 16 live colours: rendered four at a time the
    solids are the ones a one-at-a-time render gives, in the same order."""
    colours = [f"#{index * 15:02X}{255 - index * 15:02X}40" for index in range(16)]
    model = tmp_path / "sixteen.scad"
    model.write_text(
        "".join(
            f'color("{colour}") translate([{index * 3}, 0, 0]) cube([2, 2, {index + 1}]);\n'
            for index, colour in enumerate(colours)
        ),
        encoding="utf-8",
    )
    config = load_config()
    results = []
    for slots in (1, 4):
        work = tmp_path / f"work-{slots}"
        work.mkdir()
        results.append(
            await render_solids(
                model,
                CustomizerSchema(),
                {},
                colours,
                work,
                config=replace(config, solid_concurrency=slots),
            )
        )

    one, four = results
    assert one.warnings == four.warnings == []
    assert list(one.meshes) == list(four.meshes) == colours
    assert all(mesh.is_watertight for mesh in four.meshes.values())
    volumes = [round(float(mesh.volume), 3) for mesh in four.meshes.values()]
    assert volumes == [round(float(mesh.volume), 3) for mesh in one.meshes.values()]
    assert volumes == [4.0 * (index + 1) for index in range(16)]
