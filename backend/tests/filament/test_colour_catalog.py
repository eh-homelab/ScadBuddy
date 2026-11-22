"""The colour catalog's sources (#6907): Bambuddy's colour catalog, filamentcolors.xyz and
Bambu Studio's colour codes, each written through `upsert_colours`."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from scadbuddy.bambuddy.models import BbColour
from scadbuddy.filament.bb_pull_apply import LocalBbPull
from scadbuddy.filament.bb_pull_models import ColoursBody, PullInstanceChangedError
from scadbuddy.filament.bb_sync_nudge import instance_for
from scadbuddy.filament.bb_sync_results import reset_links
from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.colour_catalog import (
    FILAMENTCOLORS_API,
    ColourIn,
    ColourSyncRunningError,
    ColourSyncs,
    bambuddy_rows,
    run_bambu,
    run_filamentcolors,
    swatch_row,
)
from scadbuddy.filament.colour_codes import TABLE
from scadbuddy.filament.models import Colour, ColourCreate, ColourPatch
from scadbuddy.library.settings_store import StoredSettings
from tests.conftest import PgPool
from tests.filament.test_bambuddy_pull import BB, NOW, _filament

pg = pytest.mark.requires_postgres

FIXTURE = Path(__file__).parents[1] / "fixtures" / "bambu_filaments_color_codes.json"


def _colours(pool: PgPool, *, archived: bool = True) -> list[Colour]:
    rows = CatalogStore(pool).list("colours", include_archived=archived)
    return [row for row in rows if isinstance(row, Colour)]


def _one(pool: PgPool, name: str) -> Colour:
    (row,) = [row for row in _colours(pool) if row.name == name]
    return row


def _bb(entry_id: int = 1, **values: Any) -> BbColour:
    fields: dict[str, Any] = {
        "id": entry_id,
        "manufacturer": "Bambu Lab",
        "color_name": "Jade White",
        "hex_color": "#ffffff",
        "material": "PLA Basic",
    }
    return BbColour.model_validate(fields | values)


def _pull(pool: PgPool) -> LocalBbPull:
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    return LocalBbPull(_filament(pool), lambda: settings, clock=lambda: NOW)


async def _send(pool: PgPool, *entries: BbColour) -> Any:
    return await _pull(pool).colours(ColoursBody(instance=instance_for(BB), colours=list(entries)))


# -- Bambuddy's colour catalog ----------------------------------------------------------


def test_bambuddy_entries_read_as_rows() -> None:
    rows, bad = bambuddy_rows(
        [
            _bb(extra_colors="ff0000,0000ff80", effect_type="sparkle"),
            _bb(2, hex_color="nope"),
            _bb(3, color_name="  "),
        ]
    )
    assert bad == 2
    assert rows == [
        ColourIn(
            manufacturer="Bambu Lab",
            name="Jade White",
            rgba="#FFFFFFFF",
            material_name="PLA Basic",
            extra_colours=["#FF0000FF", "#0000FF80"],
            effect_name="sparkle",
            bb_colour_id=1,
        )
    ]


@pg
async def test_a_bambuddy_colour_is_added_linked_with_its_material_and_effect(
    pg_pool: PgPool,
) -> None:
    applied = await _send(pg_pool, _bb(effect_type="sparkle", hex_color="#F4EE2A80"))
    assert (applied.added, applied.skipped) == (1, 0)
    row = _one(pg_pool, "Jade White")
    assert (row.rgba, row.material_name, row.source, row.bb_colour_id) == (
        "#F4EE2A80",
        "PLA Basic",
        "bambuddy",
        1,
    )
    materials = {m.name: m.id for m in CatalogStore(pg_pool).list("materials")}  # type: ignore[attr-defined]
    effects = {e.name: e.id for e in CatalogStore(pg_pool).list("effects")}  # type: ignore[attr-defined]
    # "PLA Basic" is no material: its first word is.
    assert row.material_id == materials["PLA"]
    assert row.effect_id == effects["Sparkle"]
    assert row.synced_at == row.updated_at


@pg
async def test_a_repull_updates_by_link_and_counts_the_rest_unchanged(pg_pool: PgPool) -> None:
    await _send(pg_pool, _bb(), _bb(2, color_name="Black", hex_color="#000000"))
    again = await _send(
        pg_pool,
        _bb(color_name="Ivory White", hex_color="#FFFFF0"),
        _bb(2, color_name="Black", hex_color="#000000"),
    )
    assert (again.added, again.updated, again.unchanged) == (0, 1, 1)
    names = sorted(row.name for row in _colours(pg_pool))
    assert names == ["Black", "Ivory White"]
    assert _one(pg_pool, "Ivory White").rgba == "#FFFFF0FF"


@pg
async def test_a_person_edit_since_the_last_pull_wins(pg_pool: PgPool) -> None:
    await _send(pg_pool, _bb())
    row = _one(pg_pool, "Jade White")
    CatalogStore(pg_pool).update("colours", row.id, ColourPatch(rgba="#FAFAFA"))
    again = await _send(pg_pool, _bb(hex_color="#EEEEEE"))
    assert (again.kept, again.updated) == (1, 0)
    assert _one(pg_pool, "Jade White").rgba == "#FAFAFAFF"


@pg
async def test_a_person_edit_wins_whatever_its_clock_says(pg_pool: PgPool) -> None:
    # An edit whose transaction began before the pull's can stamp an earlier `updated_at`
    # than the pull's `synced_at`; the edit clears `synced_at`, so the row stays the
    # person's (Decision 217).
    await _send(pg_pool, _bb())
    row = _one(pg_pool, "Jade White")
    CatalogStore(pg_pool).update("colours", row.id, ColourPatch(rgba="#FAFAFA"))
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE filament_colours SET updated_at = now() - interval '1 hour' WHERE id = %s",
            (row.id,),
        )
    edited = _one(pg_pool, "Jade White")
    assert (edited.source, edited.synced_at) == ("bambuddy", None)
    again = await _send(pg_pool, _bb(hex_color="#EEEEEE"))
    assert (again.kept, again.updated) == (1, 0)
    assert _one(pg_pool, "Jade White").rgba == "#FAFAFAFF"


@pg
async def test_a_re_order_or_an_archive_leaves_the_row_to_the_syncs(pg_pool: PgPool) -> None:
    await _send(pg_pool, _bb())
    store = CatalogStore(pg_pool)
    row = _one(pg_pool, "Jade White")
    store.update("colours", row.id, ColourPatch(sort_order=5))
    store.update("colours", row.id, ColourPatch(archived=True))
    store.update("colours", row.id, ColourPatch(archived=False))
    assert _one(pg_pool, "Jade White").synced_at is not None
    again = await _send(pg_pool, _bb(hex_color="#EEEEEE"))
    assert (again.kept, again.updated) == (0, 1)
    assert _one(pg_pool, "Jade White").rgba == "#EEEEEEFF"


@pg
async def test_a_person_s_own_row_is_kept_and_linked(pg_pool: PgPool) -> None:
    CatalogStore(pg_pool).create(
        "colours",
        ColourCreate(
            name="Jade White", manufacturer="bambu lab", material_name="pla basic", rgba="#FEFEFE"
        ),
    )
    applied = await _send(pg_pool, _bb())
    assert (applied.added, applied.kept) == (0, 1)
    row = _one(pg_pool, "Jade White")
    assert (row.rgba, row.source, row.bb_colour_id) == ("#FEFEFEFF", None, 1)


@pg
async def test_an_archived_row_is_neither_brought_back_nor_added_again(pg_pool: PgPool) -> None:
    await _send(pg_pool, _bb())
    row = _one(pg_pool, "Jade White")
    CatalogStore(pg_pool).archive("colours", row.id, True)
    again = await _send(pg_pool, _bb(hex_color="#000000"))
    assert again.kept == 1
    (only,) = _colours(pg_pool)
    assert only.archived_at is not None and only.rgba == "#FFFFFFFF"


@pg
async def test_an_archived_synced_row_keeps_its_values(pg_pool: PgPool) -> None:
    # Archiving leaves `synced_at` as it is, but a sync still leaves the row alone.
    await _send(pg_pool, _bb())
    row = _one(pg_pool, "Jade White")
    CatalogStore(pg_pool).update("colours", row.id, ColourPatch(archived=True))
    assert _one(pg_pool, "Jade White").synced_at is not None
    again = await _send(pg_pool, _bb(hex_color="#123456"))
    assert (again.kept, again.updated) == (1, 0)
    assert _one(pg_pool, "Jade White").rgba == "#FFFFFFFF"


@pg
async def test_one_name_of_two_materials_is_two_rows(pg_pool: PgPool) -> None:
    applied = await _send(
        pg_pool,
        _bb(1, color_name="Black", hex_color="#000000", material="PLA Basic"),
        _bb(2, color_name="Black", hex_color="#101010", material="PETG HF"),
    )
    assert applied.added == 2
    assert sorted(row.material_name or "" for row in _colours(pg_pool)) == ["PETG HF", "PLA Basic"]


@pg
async def test_another_bambuddy_s_colours_are_refused(pg_pool: PgPool) -> None:
    with pytest.raises(PullInstanceChangedError):
        await _pull(pg_pool).colours(
            ColoursBody(instance=instance_for("https://other.test"), colours=[_bb()])
        )
    assert _colours(pg_pool) == []


@pg
async def test_a_reset_of_the_links_unlinks_the_colours(pg_pool: PgPool) -> None:
    await _send(pg_pool, _bb())
    f = _filament(pg_pool)
    done = reset_links(
        f, instance_for(BB), instance_for("https://new.test"), None, expect_kept=False
    )
    assert done.deleted.colours == 1
    assert _one(pg_pool, "Jade White").bb_colour_id is None


# -- filamentcolors.xyz -----------------------------------------------------------------


def _swatch(swatch_id: int, name: str, hex_color: str, **more: Any) -> dict[str, Any]:
    return {
        "id": swatch_id,
        "manufacturer": {"id": 1, "name": "Polymaker"},
        "color_name": name,
        "filament_type": {"id": 2, "name": "PLA"},
        "hex_color": hex_color,
        "color_parent": "BLK",
        "alt_color_parent": None,
    } | more


def test_a_translucent_swatch_takes_alpha_80() -> None:
    row = swatch_row(_swatch(1, "Clear Red", "d01020", color_parent="TRN"))
    assert row is not None and row.rgba == "#D0102080"
    assert swatch_row({"color_name": "No maker", "hex_color": "000000"}) is None


def _page(results: list[dict[str, Any]], *, count: int, more: bool) -> dict[str, Any]:
    return {
        "count": count,
        "next": "https://filamentcolors.xyz/next" if more else None,
        "results": results,
    }


async def _no_sleep(_: float) -> None:
    return None


async def _run(pool: PgPool, *, force: bool = False) -> Any:
    syncs = ColourSyncs(pool)
    async with httpx.AsyncClient() as http:
        return await run_filamentcolors(
            syncs, syncs.start("filamentcolors"), http, force=force, sleep=_no_sleep
        )


@pg
async def test_filamentcolors_pages_fill_the_catalog(pg_pool: PgPool) -> None:
    with respx.mock() as router:
        router.get(f"{FILAMENTCOLORS_API}/version/").respond(
            json={"db_version": 1, "db_last_modified": 100}
        )
        swatches = router.get(f"{FILAMENTCOLORS_API}/swatch/")
        swatches.side_effect = [
            httpx.Response(
                200,
                json=_page([_swatch(1, "Jet Black", "000000"), {"bad": 1}], count=3, more=True),
            ),
            httpx.Response(200, json=_page([_swatch(2, "Snow", "FFFFFF")], count=3, more=False)),
        ]
        done = await _run(pg_pool)
        assert swatches.calls[0].request.url.params["page_size"] == "100"
    assert (done.state, done.pages_read, done.pages_total, done.total) == ("done", 2, 1, 3)
    assert (done.added, done.skipped) == (2, 1)
    row = _one(pg_pool, "Jet Black")
    assert (row.manufacturer, row.material_name, row.source) == (
        "Polymaker",
        "PLA",
        "filamentcolors",
    )


@pg
async def test_an_unchanged_version_reads_no_page_unless_forced(pg_pool: PgPool) -> None:
    with respx.mock() as router:
        router.get(f"{FILAMENTCOLORS_API}/version/").respond(json={"db_last_modified": 7})
        pages = router.get(f"{FILAMENTCOLORS_API}/swatch/").respond(
            json=_page([_swatch(1, "Jet Black", "000000")], count=1, more=False)
        )
        assert (await _run(pg_pool)).state == "done"
        again = await _run(pg_pool)
        assert again.state == "unchanged" and pages.call_count == 1
        forced = await _run(pg_pool, force=True)
        assert (forced.state, forced.unchanged, pages.call_count) == ("done", 1, 2)


@pg
async def test_a_page_that_keeps_failing_is_reported_and_keeps_what_was_written(
    pg_pool: PgPool,
) -> None:
    with respx.mock() as router:
        router.get(f"{FILAMENTCOLORS_API}/version/").respond(json={"db_last_modified": 1})
        pages = router.get(f"{FILAMENTCOLORS_API}/swatch/")
        pages.side_effect = [
            httpx.Response(
                200, json=_page([_swatch(1, "Jet Black", "000000")], count=200, more=True)
            ),
            httpx.Response(503),
            httpx.ConnectError("down"),
            httpx.Response(429, headers={"Retry-After": "1"}),
        ]
        done = await _run(pg_pool)
    assert done.state == "failed"
    assert done.error == "filamentcolors.xyz answered 429"
    assert done.pages_read == 1 and done.added == 1
    assert [row.name for row in _colours(pg_pool)] == ["Jet Black"]


@pg
@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (httpx.Response(404), "filamentcolors.xyz answered 404"),
        (httpx.Response(200, text="<html>"), "filamentcolors.xyz answered with no JSON"),
        (
            httpx.Response(200, json={"results": "no"}),
            "filamentcolors.xyz answered in another shape",
        ),
    ],
)
async def test_a_refusal_or_another_shape_fails_at_once(
    pg_pool: PgPool, answer: httpx.Response, error: str
) -> None:
    with respx.mock() as router:
        router.get(f"{FILAMENTCOLORS_API}/version/").respond(json={})
        pages = router.get(f"{FILAMENTCOLORS_API}/swatch/").mock(return_value=answer)
        done = await _run(pg_pool)
    assert (done.state, done.error, pages.call_count) == ("failed", error, 1)


@pg
def test_a_second_run_of_a_source_is_refused_while_one_runs(pg_pool: PgPool) -> None:
    syncs = ColourSyncs(pg_pool)
    syncs.start("filamentcolors")
    assert syncs.running("filamentcolors")
    with pytest.raises(ColourSyncRunningError):
        syncs.start("filamentcolors")
    syncs.start("bambu")
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_colour_syncs SET started_at = now() - interval '1 hour'")
    assert not syncs.running("filamentcolors")
    syncs.start("filamentcolors")
    states = sorted((s.source, s.state) for s in syncs.latest())
    assert states == [("bambu", "failed"), ("filamentcolors", "running")]


@pg
def test_a_retried_operation_run_ends_its_own_dead_row(pg_pool: PgPool) -> None:
    # The first attempt's worker died with its row `running`; the retry is the same
    # operation run, so it is not refused against that row. Another operation still is.
    syncs = ColourSyncs(pg_pool)
    dead = syncs.start("bambu", "operation-1")
    retry = syncs.start("bambu", "operation-1")
    assert syncs.get(dead).state == "failed"
    assert syncs.get(dead).error == "the run was interrupted"
    assert syncs.get(retry).state == "running"
    with pytest.raises(ColourSyncRunningError):
        syncs.start("bambu", "operation-2")


@pg
def test_a_stale_running_run_reads_as_interrupted(pg_pool: PgPool) -> None:
    # Its process died (a restart): no one else would ever end it, and the panel would
    # wait on it for good.
    syncs = ColourSyncs(pg_pool)
    stale = syncs.start("filamentcolors")
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_colour_syncs SET started_at = now() - interval '31 minutes'")
    syncs.start("bambu")
    runs = {run.source: run for run in syncs.latest()}
    assert (runs["filamentcolors"].id, runs["filamentcolors"].state) == (stale, "failed")
    assert runs["filamentcolors"].error == "the run was interrupted"
    assert runs["filamentcolors"].finished_at is not None
    assert runs["bambu"].state == "running"


# -- Bambu Studio's colour codes --------------------------------------------------------


def _profiles(tmp_path: Path) -> Path:
    (tmp_path / TABLE).parent.mkdir(parents=True)
    shutil.copy(FIXTURE, tmp_path / TABLE)
    return tmp_path


@pg
async def test_bambu_s_codes_are_bambu_lab_colours_with_exact_rgba(
    pg_pool: PgPool, tmp_path: Path
) -> None:
    syncs = ColourSyncs(pg_pool)
    done = await run_bambu(syncs, syncs.start("bambu"), _profiles(tmp_path))
    assert (done.state, done.added, done.total) == ("done", 4, 4)
    clear = _one(pg_pool, "Translucent Yellow")
    assert (clear.manufacturer, clear.rgba, clear.material_name) == (
        "Bambu Lab",
        "#F4EE2A80",
        "PLA Translucent",
    )
    dual = _one(pg_pool, "Dual Red Blue")
    assert (dual.rgba, dual.extra_colours) == ("#FF0000FF", ["#0000FFFF"])
    again = await run_bambu(syncs, syncs.start("bambu"), _profiles(tmp_path / "again"))
    assert (again.added, again.unchanged) == (0, 4)


@pg
async def test_a_bambu_code_with_no_material_is_added_without_one(
    pg_pool: PgPool, tmp_path: Path
) -> None:
    (tmp_path / TABLE).parent.mkdir(parents=True)
    table = json.loads(FIXTURE.read_text())
    table["data"] = [table["data"][0] | {"fila_type": " "}]
    (tmp_path / TABLE).write_text(json.dumps(table))
    syncs = ColourSyncs(pg_pool)
    done = await run_bambu(syncs, syncs.start("bambu"), tmp_path)
    assert (done.state, done.added) == ("done", 1)
    black = _one(pg_pool, "Black")
    assert (black.material_name, black.material_id) == (None, None)


@pg
async def test_a_missing_bambu_table_fails_the_run(pg_pool: PgPool, tmp_path: Path) -> None:
    syncs = ColourSyncs(pg_pool)
    done = await run_bambu(syncs, syncs.start("bambu"), tmp_path)
    assert done.state == "failed" and done.error is not None and "missing" in done.error


# -- the search -------------------------------------------------------------------------


@pg
def test_a_colour_search_matches_every_word_in_any_text(pg_pool: PgPool) -> None:
    store = CatalogStore(pg_pool)
    store.create(
        "colours", ColourCreate(name="Jade White", manufacturer="Bambu Lab", rgba="#FFFFFF")
    )
    store.create(
        "colours", ColourCreate(name="Galaxy Black", manufacturer="Polymaker", rgba="#101010")
    )
    assert [r.name for r in store.list("colours", q="bambu white")] == ["Jade White"]  # type: ignore[attr-defined]
    assert [r.name for r in store.list("colours", q="101010")] == ["Galaxy Black"]  # type: ignore[attr-defined]
    assert store.list("colours", q="100%") == []
    assert [r.name for r in store.list("materials", q="pla")] == ["PLA"]  # type: ignore[attr-defined]
