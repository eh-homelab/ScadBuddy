"""#7204 Task B1: copy and edit a ScadBuddy filament preset, as activities on the
``library`` worker. Each writes a new preset or a new version under the presets' lock,
once per idempotency key, and then sends it to Bambuddy (respx) after the commit."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import respx
from pydantic import ValidationError
from temporalio.exceptions import ApplicationError

from scadbuddy import main
from scadbuddy.filament.preset_writes import (
    COPY_ACTIVITY,
    EDIT_ACTIVITY,
    MAX_EDIT_KEYS,
    PRESET_WRITE_ACTIVITIES,
    PresetCopyInput,
    PresetEditInput,
    PresetWritten,
    copy_preset,
    edit_preset,
    preset_write_activities,
)
from scadbuddy.filament.sb_presets import (
    FilamentPresetStore,
    create_on,
    document_bytes,
    keep_document,
)
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.store.local import LocalBlobStore
from tests.conftest import PgPool
from tests.filament.test_sb_presets import _type

pytestmark = pytest.mark.requires_postgres

BASE = "https://bambuddy.test"
API = f"{BASE}/api/v1"
OFF = StoredSettings()
ON = StoredSettings(bambuddy_url=BASE, bambuddy_api_key="k")


class Writes:
    """The two writes over one pool, blob store and settings."""

    def __init__(self, pool: PgPool, blobs: LocalBlobStore, settings: StoredSettings) -> None:
        self.pool, self.blobs, self.settings = pool, blobs, settings

    async def copy(self, preset_id: int, name: str, key: str = "k1") -> PresetWritten:
        body = PresetCopyInput(preset_id=preset_id, name=name, actor="ann", idempotency_key=key)
        return await copy_preset(self.pool, self.blobs, self.settings, body)

    async def edit(self, preset_id: int, key: str = "e1", **fields: Any) -> PresetWritten:
        body = PresetEditInput.model_validate(
            {"preset_id": preset_id, "actor": "ann", "idempotency_key": key} | fields
        )
        return await edit_preset(self.pool, self.blobs, self.settings, body)


async def _make(
    pool: PgPool, blobs: LocalBlobStore, doc: dict[str, Any], *, source: str = "manual", **cols: Any
) -> int:
    key, sha256 = await keep_document(blobs, document_bytes(doc))
    with pool.connection() as conn, conn.transaction():
        return create_on(
            conn,
            name=doc["name"],
            source=source,  # type: ignore[arg-type]
            key=key,
            sha256=sha256,
            doc=doc,
            made_by="t",
            **cols,
        )


def _row(pool: PgPool, preset_id: int) -> dict[str, Any]:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT name, source, version, blob_key, type_id, printer_model, nozzle_diameter,"
            " tdfp_id, bb_preset_id, mirrored_sha256 FROM filament_presets WHERE id = %s",
            (preset_id,),
        ).fetchone()
    assert row is not None
    return dict(row)


def _count(pool: PgPool) -> int:
    with pool.connection() as conn:
        row = conn.execute("SELECT count(*) AS n FROM filament_presets").fetchone()
    assert row is not None
    return int(row["n"])


async def _doc(pool: PgPool, blobs: LocalBlobStore, preset_id: int) -> dict[str, Any] | None:
    return await FilamentPresetStore(pool, lambda: blobs).read(_row(pool, preset_id)["blob_key"])


async def _refused(write: Any) -> ApplicationError:
    with pytest.raises(ApplicationError) as caught:
        await write
    assert caught.value.non_retryable is True
    return caught.value


async def test_a_copy_is_a_manual_preset_with_the_source_document_and_the_new_name(
    pg_pool: PgPool, tmp_path: Path
) -> None:
    blobs = LocalBlobStore(tmp_path / "blobs")
    type_id = _type(pg_pool, "PLA-COPY")
    doc = {"name": "Silk @H2C", "filament_type": ["PLA"], "nozzle_temperature": ["220"]}
    source = await _make(
        pg_pool,
        blobs,
        doc,
        source="3dfp",
        type_id=type_id,
        printer_model="H2C",
        nozzle_diameter="0.4",
        tdfp_id=30173,
        bb_preset_id=12,
    )

    made = await Writes(pg_pool, blobs, OFF).copy(source, "  My silk  ")

    assert (made.version, made.changed, made.mirror) == (1, True, "off")
    assert made.preset_id != source
    row = _row(pg_pool, made.preset_id)
    assert (row["name"], row["source"], row["version"]) == ("My silk", "manual", 1)
    assert (row["type_id"], row["printer_model"], row["nozzle_diameter"]) == (type_id, "H2C", "0.4")
    assert (row["tdfp_id"], row["bb_preset_id"], row["mirrored_sha256"]) == (None, None, None)
    assert await _doc(pg_pool, blobs, made.preset_id) == {**doc, "name": "My silk"}
    with pg_pool.connection() as conn:
        held = conn.execute(
            "SELECT holder_id FROM blob_refs WHERE key = %s AND holder_kind = 'filament_preset'",
            (row["blob_key"],),
        ).fetchall()
    assert [h["holder_id"] for h in held] == [f"{made.preset_id}:1"]


async def test_an_edit_sets_and_unsets_keys_as_a_new_version(
    pg_pool: PgPool, tmp_path: Path
) -> None:
    blobs = LocalBlobStore(tmp_path / "blobs")
    made = await _make(pg_pool, blobs, {"name": "Mine", "a": "1", "b": ["2"], "c": "3"})

    done = await Writes(pg_pool, blobs, OFF).edit(
        made, expected_version=1, set={"a": "10", "d": ["4", "5"]}, unset=["c"]
    )

    assert (done.preset_id, done.version, done.changed) == (made, 2, True)
    assert _row(pg_pool, made)["version"] == 2
    assert await _doc(pg_pool, blobs, made) == {
        "name": "Mine",
        "a": "10",
        "b": ["2"],
        "d": ["4", "5"],
    }


async def test_a_rename_alone_is_a_new_version(pg_pool: PgPool, tmp_path: Path) -> None:
    blobs = LocalBlobStore(tmp_path / "blobs")
    made = await _make(pg_pool, blobs, {"name": "Old", "a": "1"})

    done = await Writes(pg_pool, blobs, OFF).edit(made, expected_version=1, name="New")

    assert (done.version, done.changed) == (2, True)
    assert _row(pg_pool, made)["name"] == "New"
    assert await _doc(pg_pool, blobs, made) == {"name": "New", "a": "1"}


async def test_an_edit_that_changes_nothing_writes_no_version(
    pg_pool: PgPool, tmp_path: Path
) -> None:
    blobs = LocalBlobStore(tmp_path / "blobs")
    made = await _make(pg_pool, blobs, {"name": "Same", "a": "1"})

    done = await Writes(pg_pool, blobs, OFF).edit(made, expected_version=1, set={"a": "1"})

    assert (done.version, done.changed) == (1, False)
    assert _row(pg_pool, made)["version"] == 1


async def test_a_stale_edit_is_refused(pg_pool: PgPool, tmp_path: Path) -> None:
    blobs = LocalBlobStore(tmp_path / "blobs")
    made = await _make(pg_pool, blobs, {"name": "Shared", "a": "1"})
    writes = Writes(pg_pool, blobs, OFF)
    await writes.edit(made, "first", expected_version=1, set={"a": "2"})

    error = await _refused(writes.edit(made, "second", expected_version=1, set={"b": "3"}))

    assert error.type == "version_moved"
    assert error.details == (2,)
    assert await _doc(pg_pool, blobs, made) == {"name": "Shared", "a": "2"}


@pytest.mark.parametrize("source", ["3dfp", "bambuddy"])
async def test_only_a_manual_preset_is_edited(pg_pool: PgPool, tmp_path: Path, source: str) -> None:
    blobs = LocalBlobStore(tmp_path / "blobs")
    made = await _make(pg_pool, blobs, {"name": "Not mine"}, source=source)

    error = await _refused(Writes(pg_pool, blobs, OFF).edit(made, expected_version=1, name="X"))

    assert error.type == "not_manual"
    assert _row(pg_pool, made)["version"] == 1


async def test_a_missing_preset_is_refused(pg_pool: PgPool, tmp_path: Path) -> None:
    writes = Writes(pg_pool, LocalBlobStore(tmp_path / "blobs"), OFF)
    assert (await _refused(writes.copy(999_999, "X"))).type == "preset_not_found"
    error = await _refused(writes.edit(999_999, expected_version=1, name="X"))
    assert error.type == "preset_not_found"


async def test_a_lost_document_is_refused(pg_pool: PgPool, tmp_path: Path) -> None:
    blobs = LocalBlobStore(tmp_path / "blobs")
    made = await _make(pg_pool, blobs, {"name": "Lost"})
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE filament_presets SET blob_key = 'attachment-' || repeat('0', 64) WHERE id = %s",
            (made,),
        )
    writes = Writes(pg_pool, blobs, OFF)
    assert (await _refused(writes.copy(made, "X"))).type == "document_missing"
    error = await _refused(writes.edit(made, expected_version=1, name="X"))
    assert error.type == "document_missing"


_MANY = {f"k{i}": "1" for i in range(MAX_EDIT_KEYS + 1)}


@pytest.mark.parametrize(
    "fields",
    [
        {"set": {"name": "X"}},
        {"set": {"printhost_apikey": "s"}},
        {"unset": ["my_token"]},
        {"set": {"bad key": "1"}},
        {"set": {"": "1"}},
        {"set": {"a": "1"}, "unset": ["a"]},
        {"set": _MANY},
        {},
    ],
    ids=["name", "printhost_apikey", "my_token", "bad key", "empty", "both", "501", "empty edit"],
)
def test_refused_keys(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PresetEditInput.model_validate(
            {"preset_id": 1, "expected_version": 1, "actor": "ann", "idempotency_key": "k"} | fields
        )


def test_a_copy_name_is_one_to_two_hundred_characters() -> None:
    for name in ("", "   ", "x" * 201):
        with pytest.raises(ValidationError):
            PresetCopyInput(preset_id=1, name=name, actor="ann", idempotency_key="k")


async def test_a_repeated_copy_key_makes_one_preset(pg_pool: PgPool, tmp_path: Path) -> None:
    blobs = LocalBlobStore(tmp_path / "blobs")
    source = await _make(pg_pool, blobs, {"name": "Once"})
    writes = Writes(pg_pool, blobs, OFF)
    before = _count(pg_pool)

    first = await writes.copy(source, "Copy", "same")
    again = await writes.copy(source, "Copy", "same")

    assert again.preset_id == first.preset_id
    assert _count(pg_pool) == before + 1


def _preset_json(preset_id: int, name: str) -> dict[str, Any]:
    return {"id": preset_id, "name": name, "preset_type": "filament", "source": "local"}


@respx.mock
async def test_a_copy_and_an_edit_are_sent_to_bambuddy_after_the_commit(
    pg_pool: PgPool, tmp_path: Path
) -> None:
    blobs = LocalBlobStore(tmp_path / "blobs")
    source = await _make(pg_pool, blobs, {"name": "Base", "a": "1"}, source="3dfp")
    respx.get(f"{API}/local-presets/").mock(
        return_value=httpx.Response(200, json={"filament": [], "printer": [], "process": []})
    )
    create = respx.post(f"{API}/local-presets/").mock(
        return_value=httpx.Response(200, json=_preset_json(41, "Sent"))
    )
    put = respx.put(f"{API}/local-presets/41").mock(
        return_value=httpx.Response(200, json=_preset_json(41, "Sent"))
    )
    writes = Writes(pg_pool, blobs, ON)

    made = await writes.copy(source, "Sent")

    assert made.mirror == "sent"
    assert json.loads(create.calls[0].request.content)["setting"] == {"name": "Sent", "a": "1"}
    assert _row(pg_pool, made.preset_id)["bb_preset_id"] == 41

    edited = await writes.edit(made.preset_id, expected_version=1, set={"a": "2"})

    assert edited.mirror == "sent"
    assert put.called
    assert _row(pg_pool, source)["bb_preset_id"] is None


@respx.mock
async def test_a_failed_send_never_fails_the_edit(pg_pool: PgPool, tmp_path: Path) -> None:
    blobs = LocalBlobStore(tmp_path / "blobs")
    made = await _make(pg_pool, blobs, {"name": "Down", "a": "1"})
    respx.get(f"{API}/local-presets/").mock(return_value=httpx.Response(500))
    respx.post(f"{API}/local-presets/").mock(return_value=httpx.Response(500))

    done = await Writes(pg_pool, blobs, ON).edit(made, expected_version=1, set={"a": "2"})

    assert (done.version, done.changed, done.mirror) == (2, True, "failed")


def test_the_library_worker_serves_the_preset_activities(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[dict[str, Any]] = []

    def kept(*args: Any) -> dict[str, Any]:
        made.append(preset_write_activities(*args))
        return made[-1]

    monkeypatch.setattr(main, "preset_write_activities", kept)

    served = main._library_activities(MagicMock(), MagicMock(), MagicMock(), [])

    assert list(made[0]) == list(PRESET_WRITE_ACTIVITIES) == [COPY_ACTIVITY, EDIT_ACTIVITY]
    assert all(fn in served for fn in made[0].values())


def _unused() -> Any:
    raise AssertionError("a refused input reads nothing")


_ACTIVITIES = preset_write_activities(_unused, _unused, _unused)
_EDIT = {"preset_id": 1, "expected_version": 1, "actor": "ann", "idempotency_key": "k"}


@pytest.mark.parametrize(
    ("fields", "key"),
    [
        ({"set": {"printhost_apikey": "s"}}, "printhost_apikey"),
        ({"set": {"a": "1"}, "unset": ["a"]}, "a"),
        # Refused before the type check, so the value is never in a failure.
        ({"set": {"printhost_apikey": 12345}}, "printhost_apikey"),
        ({"unset": ["my_token", 7]}, "my_token"),
    ],
    ids=["credential", "both", "credential not a string", "unset not all strings"],
)
async def test_an_edit_activity_refuses_a_refused_key_once(
    fields: dict[str, Any], key: str
) -> None:
    error = await _refused(_ACTIVITIES[EDIT_ACTIVITY](_EDIT | fields))
    assert error.type == "key_refused"
    assert error.details == (key,)


@pytest.mark.parametrize(
    ("name", "raw", "loc"),
    [
        (
            COPY_ACTIVITY,
            {"preset_id": "x", "name": "A", "actor": "ann", "idempotency_key": "k"},
            ["preset_id"],
        ),
        (EDIT_ACTIVITY, _EDIT | {"expected_version": 0, "name": "A"}, ["expected_version"]),
    ],
    ids=["copy", "edit"],
)
async def test_an_activity_refuses_a_bad_input_once(
    name: str, raw: dict[str, Any], loc: list[str]
) -> None:
    error = await _refused(_ACTIVITIES[name](raw))
    assert error.type == "invalid_input"
    assert [e["loc"] for e in error.details[0]] == [loc]


#: A value that must never be in an activity failure.
SENTINEL = "sentinel-s3cret-value"


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        (
            COPY_ACTIVITY,
            {"preset_id": SENTINEL, "name": "A", "actor": "ann", "idempotency_key": "k"},
        ),
        (EDIT_ACTIVITY, _EDIT | {"set": {"a": {"nested": SENTINEL}}}),
        (EDIT_ACTIVITY, _EDIT | {"name": SENTINEL * 20}),
    ],
    ids=["copy id", "edit set value", "edit name"],
)
async def test_a_refused_input_never_echoes_its_values(name: str, raw: dict[str, Any]) -> None:
    error = await _refused(_ACTIVITIES[name](raw))
    assert error.type == "invalid_input"
    assert SENTINEL not in error.message
    assert SENTINEL not in json.dumps(error.details)


async def test_a_replay_gets_the_first_answer_after_the_document_is_lost(
    pg_pool: PgPool, tmp_path: Path
) -> None:
    blobs = LocalBlobStore(tmp_path / "blobs")
    made = await _make(pg_pool, blobs, {"name": "Gone", "a": "1"})
    writes = Writes(pg_pool, blobs, OFF)
    copied = await writes.copy(made, "Copy", "c1")
    edited = await writes.edit(made, "e1", expected_version=1, set={"a": "2"})
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE filament_presets SET blob_key = 'attachment-' || repeat('0', 64) WHERE id = %s",
            (made,),
        )

    assert await writes.copy(made, "Copy", "c1") == copied
    assert await writes.edit(made, "e1", expected_version=1, set={"a": "2"}) == edited


async def test_a_future_version_is_refused_before_a_document_is_kept(
    pg_pool: PgPool, tmp_path: Path
) -> None:
    blobs = LocalBlobStore(tmp_path / "blobs")
    made = await _make(pg_pool, blobs, {"name": "Guess", "a": "1"})
    before = blobs.keys()

    error = await _refused(
        Writes(pg_pool, blobs, OFF).edit(made, expected_version=2, set={"a": "2"})
    )

    assert (error.type, error.details) == ("version_moved", (1,))
    assert blobs.keys() == before
