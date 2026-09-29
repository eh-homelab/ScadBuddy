"""Named parameter sets per template: the configurations a template is usually
printed with, so a print only needs the one value that differs this time.

Two kinds, listed together:

- **template** presets are defined by the template itself, in the ``presets`` list of
  its ``model.json`` (#326). They are part of the template -- a built-in's come from
  the image -- and are read-only here; a template of mine edits them through its
  metadata. A legacy ``presets.json`` (:data:`LEGACY_PRESETS_NAME`) beside the source
  is still read, below ``model.json``.
- **mine** are the ones saved through the API, in Postgres (``saved_presets``, #332),
  for built-ins as much as for templates of mine. The server always has a database
  (#401); a store built without one reads only a template's own presets, as the
  bundled-template checks do, and refuses a save
  (:class:`SavedPresetsUnavailableError`).

A preset holds only the values it sets. Applying one starts from the template's
defaults, so a default the template changes later still reaches every preset that
never touched it.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, TypeVar

from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import (
    BaseModel,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from scadbuddy.core.paths import LEGACY_PRESETS_NAME, MODEL_META_NAME, DataPaths
from scadbuddy.library.slugs import MAX_SLUG_LENGTH, SLUG_PATTERN, InvalidSlugError, slugify
from scadbuddy.render.inputs import InputsError, legacy_inputs, normalize_inputs
from scadbuddy.render.pg_store import migrate
from scadbuddy.render.schema import ParamValue

logger = logging.getLogger(__name__)
T = TypeVar("T")

#: A preset name is a label in a picker, not prose.
MAX_PRESET_NAME = 80
#: Per template. Far past what a picker is usable with; it bounds the file a
#: template's presets live in, which is rewritten whole on every save.
MAX_PRESETS = 200
#: A preset's description and tags (#327): short enough that the most presets a
#: template can define still make a small model.json commit.
MAX_PRESET_DESCRIPTION = 2000
MAX_PRESET_TAGS = 20
MAX_PRESET_TAG = 40
#: A template preset's id is this plus its key (:func:`template_preset_keys`), so it can
#: never be taken for the id of a saved one (32 hex digits) and a write addressed to
#: it can be refused.
TEMPLATE_ID_PREFIX = "template-"
#: Prefixed to a template's id for its advisory lock's key, so no other lock hashed
#: from a slug is ever the same one.
PRESET_LOCK_PREFIX = "scadbuddy-presets:"
#: The key in ``model.json`` that holds a template's own presets.
PRESETS_KEY = "presets"

PresetOrigin = Literal["template", "mine"]


class PresetNotFoundError(KeyError):
    pass


class PresetExistsError(ValueError):
    """Another preset of the same template already has that name."""


class TooManyPresetsError(ValueError):
    pass


class SavedPresetsUnavailableError(RuntimeError):
    """Saved presets live in Postgres, and this server has no database."""


def _clean_name(name: str) -> str:
    cleaned = " ".join(name.split())
    if not cleaned:
        raise ValueError("a preset needs a name")
    return cleaned


class _PresetBody(BaseModel):
    """What a preset is, wherever it is written: in a template's ``presets.json`` or
    in the body of a save."""

    name: str = Field(min_length=1, max_length=MAX_PRESET_NAME)
    params: dict[str, ParamValue] = Field(default_factory=dict)
    #: Template inputs (spec 2026-09-27 §4.3). Given, they win and ``params`` is read
    #: from them; left out, ``params`` is read as ``{"params": …, "v": 0}``.
    inputs: dict[str, Any] | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, name: str) -> str:
        return _clean_name(name)

    @model_validator(mode="after")
    def _one_state(self) -> _PresetBody:
        try:
            self.inputs = normalize_inputs(self.inputs, self.params)
        except InputsError as error:
            raise ValueError(str(error)) from None
        self.params = self.inputs["params"]
        return self


class ParamPresetCreate(_PresetBody):
    pass


class ParamPresetUpdate(BaseModel):
    """A rename, a new set of values, or both. ``params`` or ``inputs`` replaces the
    old ones whole; ``params`` alone keeps the preset's other inputs keys."""

    name: str | None = Field(default=None, min_length=1, max_length=MAX_PRESET_NAME)
    params: dict[str, ParamValue] | None = None
    inputs: dict[str, Any] | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, name: str | None) -> str | None:
        return None if name is None else _clean_name(name)

    @model_validator(mode="after")
    def _one_state(self) -> ParamPresetUpdate:
        if self.inputs is not None:
            try:
                self.inputs = normalize_inputs(self.inputs, self.params)
            except InputsError as error:
                raise ValueError(str(error)) from None
            self.params = self.inputs["params"]
        return self


class ParamPresetDuplicate(BaseModel):
    """The copy's name; its values are the original's."""

    name: str = Field(min_length=1, max_length=MAX_PRESET_NAME)

    @field_validator("name")
    @classmethod
    def _name(cls, name: str) -> str:
        return _clean_name(name)


class TemplatePreset(_PresetBody):
    """One preset a template defines in its ``model.json``.

    ``id`` is what keeps it the same preset when the list is reordered or it is
    renamed; without one, the key is derived from the name (:func:`template_preset_keys`).
    ``description`` and ``tags`` are carried for #327, which puts them in the API.
    """

    id: str | None = Field(default=None, pattern=SLUG_PATTERN, max_length=MAX_SLUG_LENGTH)
    # A factory, not `= ""`: a literal default makes the generated TypeScript type
    # require the field, which a client editing presets has no reason to send.
    description: str = Field(default_factory=str, max_length=MAX_PRESET_DESCRIPTION)
    tags: list[Annotated[str, StringConstraints(max_length=MAX_PRESET_TAG)]] = Field(
        default_factory=list, max_length=MAX_PRESET_TAGS
    )

    @model_validator(mode="after")
    def _one_state(self, info: ValidationInfo) -> TemplatePreset:
        # Read from a stored file (`STORED`), `params` wins over the `inputs` beside
        # them: a hand edit, or an older release, changes `params` only, and a
        # disagreement must never cost the template its whole list. A request body is
        # strict, as a saved preset's is: a client that disagrees with itself is a 422.
        inputs = self.inputs
        params: dict[str, ParamValue] | None = self.params
        stored = bool(info.context and info.context.get(STORED))
        if stored and inputs is not None and "params" in self.model_fields_set:
            inputs, params = {**inputs, "params": params}, None
        try:
            self.inputs = normalize_inputs(inputs, params)
        except InputsError as error:
            raise ValueError(str(error)) from None
        self.params = self.inputs["params"]
        return self


def for_model_json(preset: dict[str, Any]) -> dict[str, Any]:
    """A template preset as ``model.json`` keeps it: ``params``, and ``inputs`` beside
    them unless they are only the plain ``{"params": …, "v": 0}``, so a file without UI
    state or a version has one place to edit the values, and a version is never lost."""
    inputs, params = preset.get("inputs"), preset.get("params")
    if isinstance(params, dict) and inputs == legacy_inputs(params):
        return {key: value for key, value in preset.items() if key != "inputs"}
    return preset


def _checked(presets: list[TemplatePreset]) -> list[TemplatePreset]:
    """Names unique ignoring case, and explicit ids unique, as a picker needs them."""
    names: set[str] = set()
    ids: set[str] = set()
    for preset in presets:
        folded = preset.name.casefold()
        if folded in names:
            raise ValueError(f"two presets are named {preset.name!r}")
        names.add(folded)
        if preset.id is not None:
            if preset.id in ids:
                raise ValueError(f"two presets have the id {preset.id!r}")
            ids.add(preset.id)
    return presets


class TemplatePresets(BaseModel):
    """A template's whole ``presets`` list, checked as one."""

    presets: list[TemplatePreset] = Field(default_factory=list)

    @field_validator("presets")
    @classmethod
    def _unique(cls, presets: list[TemplatePreset]) -> list[TemplatePreset]:
        # The same bound as a template's saved presets: model.json is committed on
        # every edit, so an unbounded list is an unbounded commit.
        if len(presets) > MAX_PRESETS:
            raise ValueError(f"a template defines at most {MAX_PRESETS} presets")
        return _checked(presets)


_PRESET_LIST: TypeAdapter[list[TemplatePreset]] = TypeAdapter(list[TemplatePreset])
#: The validation context key for a preset read from a stored file (`TemplatePreset`).
STORED = "stored"


def template_preset_keys(presets: Sequence[TemplatePreset]) -> list[str]:
    """Each preset's key: its ``id``, else its name as a slug, else its position.

    A derived key that another preset already has gets ``-2``, ``-3`` and so on, in
    list order, so every key is unique however the ids were written.
    """
    taken = {preset.id for preset in presets if preset.id is not None}
    keys: list[str] = []
    for index, preset in enumerate(presets):
        if preset.id is not None:
            keys.append(preset.id)
            continue
        try:
            base = slugify(preset.name)
        except InvalidSlugError:
            base = f"preset-{index + 1}"
        key, suffix = base, 2
        while key in taken:
            key, suffix = f"{base}-{suffix}", suffix + 1
        taken.add(key)
        keys.append(key)
    return keys


def with_keys(presets: Sequence[TemplatePreset]) -> list[TemplatePreset]:
    """``presets`` with every ``id`` filled in, so the keys are written down and stay."""
    return [
        preset.model_copy(update={"id": key})
        for preset, key in zip(presets, template_preset_keys(presets), strict=True)
    ]


class ParamPreset(BaseModel):
    id: str
    name: str
    params: dict[str, ParamValue]
    #: The preset's template inputs (spec §4.3); ``params`` is their ``params``.
    inputs: dict[str, Any] = Field(default_factory=dict)
    origin: PresetOrigin = Field(
        description="`template`: defined by the template in its model.json, read-only. "
        "`mine`: saved here, editable -- on built-ins too."
    )
    updated_at: datetime | None = None


def _same_name(a: str, b: str) -> bool:
    return a.casefold() == b.casefold()


class PresetStore:
    """Reads a template's presets and writes the saved ones.

    The saved ones are rows of ``saved_presets``. Every write to a template's presets,
    saved or its own (:meth:`with_names_free`), runs in a transaction holding that
    template's advisory lock, so the checks that span both kinds -- one name per
    preset, the count -- hold across every process sharing the database.
    """

    def __init__(
        self,
        paths: DataPaths,
        conninfo: str | None = None,
        *,
        pool_size: int = 4,
        connect_timeout: float = 30.0,
    ) -> None:
        self.paths = paths
        self.connect_timeout = connect_timeout
        self._pool: ConnectionPool[Connection[DictRow]] | None = (
            ConnectionPool(
                conninfo,
                min_size=1,
                max_size=pool_size,
                open=False,
                connection_class=Connection[DictRow],
                kwargs={"autocommit": True, "row_factory": dict_row},
                name="scadbuddy-presets",
            )
            if conninfo
            else None
        )

    def open(self) -> None:
        """Connect, and apply the migrations (`pg_store`'s list, which is the backend's
        one list) if nothing has yet."""
        if self._pool is None:
            return
        self._pool.open(wait=True, timeout=self.connect_timeout)
        with self._pool.connection() as conn:
            migrate(conn)

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()

    def _defined(self, model_id: str, name: str, raw: Any) -> list[TemplatePreset]:
        """``raw`` as a checked preset list, or none when it is not one (logged)."""
        try:
            return _checked(_PRESET_LIST.validate_python(raw, context={STORED: True}))
        except (ValidationError, ValueError, RecursionError) as error:
            logger.warning(
                "ignored a template's presets in %s: %s", name, error, extra={"slug": model_id}
            )
            return []

    def _read_json(self, model_id: str, name: str) -> Any:
        """A JSON file of the template's, or None when it is missing or unreadable."""
        path = self.paths.model_dir(model_id) / name
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, ValueError, RecursionError):
            logger.warning("could not read %s", name, extra={"slug": model_id})
            return None

    def template_presets(self, model_id: str) -> list[ParamPreset]:
        """The template's own: its ``model.json`` list, then any from a legacy
        ``presets.json`` whose key or name ``model.json`` does not already have.

        A list that is not a valid one is logged and treated as no presets: it costs
        the picker that template's presets, never the page or the model.
        """
        meta = self._read_json(model_id, MODEL_META_NAME)
        defined: list[TemplatePreset] = []
        if isinstance(meta, dict) and meta.get(PRESETS_KEY) is not None:
            defined = self._defined(model_id, MODEL_META_NAME, meta[PRESETS_KEY])
        legacy = self._read_json(model_id, LEGACY_PRESETS_NAME)
        if isinstance(legacy, dict) and legacy.get(PRESETS_KEY) is not None:
            keys = set(template_preset_keys(defined))
            names = {preset.name.casefold() for preset in defined}
            for preset in with_keys(
                self._defined(model_id, LEGACY_PRESETS_NAME, legacy[PRESETS_KEY])
            ):
                if preset.id not in keys and preset.name.casefold() not in names:
                    defined.append(preset)
        # Keyed again over the merged list, not with the keys above: those only decide
        # which legacy entries are new. The legacy ones arrive with their keys written
        # in as ids (`with_keys`), so this second pass keeps them, and keeps every
        # model.json key, and is what guarantees the final ids are unique together.
        return [
            ParamPreset(
                id=f"{TEMPLATE_ID_PREFIX}{key}",
                name=preset.name,
                params=preset.params,
                inputs=preset.inputs or legacy_inputs(preset.params),
                origin="template",
            )
            for preset, key in zip(defined, template_preset_keys(defined), strict=True)
        ]

    @contextmanager
    def _locked(self, model_id: str) -> Iterator[Connection[DictRow]]:
        """A transaction holding ``model_id``'s preset lock, released at its end."""
        if self._pool is None:
            raise SavedPresetsUnavailableError(
                "saved presets need a database: set SCADBUDDY_DATABASE_URL"
            )
        with self._pool.connection() as conn, conn.transaction():
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"{PRESET_LOCK_PREFIX}{model_id}",),
            )
            yield conn

    @staticmethod
    def _inputs(row: DictRow) -> dict[str, Any]:
        """A row's inputs with its ``params`` column authoritative for their ``params``:
        an older release (a rollback) updates ``params`` only. A row saved before
        inputs (``'{}'``) reads as ``{"params": …, "v": 0}``."""
        if not row["inputs"]:
            return legacy_inputs(row["params"])
        return {**row["inputs"], "params": row["params"]}

    @classmethod
    def _view(cls, row: DictRow) -> ParamPreset:
        return ParamPreset(
            id=row["id"],
            name=row["name"],
            params=row["params"],
            inputs=cls._inputs(row),
            origin="mine",
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _saved(conn: Connection[DictRow], model_id: str) -> list[DictRow]:
        return conn.execute(
            "SELECT * FROM saved_presets WHERE model_id = %s ORDER BY position",
            (model_id,),
        ).fetchall()

    def saved_presets(self, model_id: str) -> list[ParamPreset]:
        if self._pool is None:
            return []
        with self._pool.connection() as conn:
            return [self._view(row) for row in self._saved(conn, model_id)]

    def saved_params(self) -> list[dict[str, ParamValue]]:
        """Every saved preset's values, of every template: references the upload sweep
        keeps. Raises when the database cannot be read, so that sweep removes nothing."""
        if self._pool is None:
            return []
        with self._pool.connection() as conn:
            return [row["params"] for row in conn.execute("SELECT params FROM saved_presets")]

    def presets(self, model_id: str) -> list[ParamPreset]:
        """The template's presets, then the saved ones, each in their own order."""
        return self.template_presets(model_id) + self.saved_presets(model_id)

    def find(self, model_id: str, preset_id: str) -> ParamPreset:
        """One preset of the template, shipped or saved."""
        for preset in self.presets(model_id):
            if preset.id == preset_id:
                return preset
        raise PresetNotFoundError(preset_id)

    def with_names_free(self, model_id: str, names: Iterable[str], write: Callable[[], T]) -> T:
        """Run ``write`` -- a change to the template's own presets -- once none of
        ``names`` is a saved preset's, ignoring case: the other direction of
        :meth:`_require_free`. Under the template's preset lock, as a save is, so a
        save and the template's list can never each pass their check before the
        other lands. Without a database there are no saved presets to clash with."""
        if self._pool is None:
            return write()
        with self._locked(model_id) as conn:
            saved = [row["name"] for row in self._saved(conn, model_id)]
            for name in names:
                if any(_same_name(name, other) for other in saved):
                    raise PresetExistsError(name)
            return write()

    def _require_free(self, model_id: str, saved: list[DictRow], name: str, own: str) -> None:
        """A name is one preset's in the picker: none of the template's, nor another saved one."""
        taken = [row["name"] for row in saved if row["id"] != own]
        taken += [p.name for p in self.template_presets(model_id)]
        if any(_same_name(name, other) for other in taken):
            raise PresetExistsError(name)

    def create(self, model_id: str, body: ParamPresetCreate) -> ParamPreset:
        with self._locked(model_id) as conn:
            saved = self._saved(conn, model_id)
            if len(saved) >= MAX_PRESETS:
                raise TooManyPresetsError(f"a template keeps at most {MAX_PRESETS} presets")
            self._require_free(model_id, saved, body.name, own="")
            now = datetime.now(UTC)
            row = conn.execute(
                "INSERT INTO saved_presets"
                " (model_id, id, name, params, inputs, created_at, updated_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *",
                (
                    model_id,
                    uuid.uuid4().hex,
                    body.name,
                    Jsonb(body.params),
                    Jsonb(body.inputs),
                    now,
                    now,
                ),
            ).fetchone()
        assert row is not None
        return self._view(row)

    def update(self, model_id: str, preset_id: str, patch: ParamPresetUpdate) -> ParamPreset:
        with self._locked(model_id) as conn:
            saved = self._saved(conn, model_id)
            current = next((row for row in saved if row["id"] == preset_id), None)
            if current is None:
                raise PresetNotFoundError(preset_id)
            if patch.name is not None:
                self._require_free(model_id, saved, patch.name, own=preset_id)
            current_inputs = self._inputs(current)
            if patch.inputs is not None:
                inputs = patch.inputs
            elif patch.params is not None:
                # Checked as a whole again: the new values count toward the size cap.
                inputs = normalize_inputs({**current_inputs, "params": patch.params}, None)
            else:
                inputs = current_inputs
            row = conn.execute(
                "UPDATE saved_presets SET name = %s, params = %s, inputs = %s, updated_at = %s"
                " WHERE model_id = %s AND id = %s RETURNING *",
                (
                    patch.name if patch.name is not None else current["name"],
                    Jsonb(inputs["params"]),
                    Jsonb(inputs),
                    datetime.now(UTC),
                    model_id,
                    preset_id,
                ),
            ).fetchone()
        assert row is not None
        return self._view(row)

    def delete(self, model_id: str, preset_id: str) -> None:
        with self._locked(model_id) as conn:
            deleted = conn.execute(
                "DELETE FROM saved_presets WHERE model_id = %s AND id = %s",
                (model_id, preset_id),
            )
            if deleted.rowcount == 0:
                raise PresetNotFoundError(preset_id)

    def copy(self, source_id: str, target_id: str) -> None:
        """Give a duplicate the presets saved on the template it was copied from.

        The duplicate's own template presets came with its directory; these are the
        ones kept beside it. Fresh ids, so the two sets are edited independently, in
        the original's order. Nothing to copy without a database.
        """
        if self._pool is None:
            return
        with self._locked(target_id) as conn:
            for row in self._saved(conn, source_id):
                conn.execute(
                    "INSERT INTO saved_presets"
                    " (model_id, id, name, params, inputs, created_at, updated_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        target_id,
                        uuid.uuid4().hex,
                        row["name"],
                        Jsonb(row["params"]),
                        Jsonb(self._inputs(row)),
                        row["created_at"],
                        row["updated_at"],
                    ),
                )

    def forget(self, model_id: str) -> None:
        """Drop a template's saved presets: it is gone, or its slug is being reused."""
        if self._pool is None:
            return
        with self._locked(model_id) as conn:
            conn.execute("DELETE FROM saved_presets WHERE model_id = %s", (model_id,))

    def sweep_orphans(self, is_live: Callable[[str], bool]) -> list[str]:
        """Forget the saved presets of every template ``is_live`` says is gone: a
        delete's own cleanup can fail, as the catalogue's orphan sweep explains."""
        if self._pool is None:
            return []
        with self._pool.connection() as conn:
            model_ids = [
                row["model_id"]
                for row in conn.execute("SELECT DISTINCT model_id FROM saved_presets")
            ]
        gone = [model_id for model_id in sorted(model_ids) if not is_live(model_id)]
        for model_id in gone:
            self.forget(model_id)
        return gone
