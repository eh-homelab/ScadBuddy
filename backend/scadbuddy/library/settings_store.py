"""The settings, in Postgres (``SCADBUDDY_DATABASE_URL``; formerly data/settings.json).

Three tables, all in ``migrations/20260928T0840Z_settings.sql``:

- ``settings``: one row per setting, ``name -> value`` (jsonb). No row is "never set":
  the environment's value for an :data:`ENV_SEEDED` field, else the default. A JSON
  ``null`` row is an env-seeded field the UI cleared, which must outlast the
  environment's value; a reset (#322) deletes the row, so the field follows the
  environment again. Every runtime field of ``Settings`` is env-seeded, so the row's
  presence alone is each field's source (:data:`SettingSource`). Map-valued settings
  (the per-printer and per-model print options) change one key at a time inside that
  row's upsert.
- ``model_print_choices``: what the print dialog last chose, one row per options scope
  (:func:`~scadbuddy.bambuddy.options.options_scope`): a model's slug, or a Bambuddy
  library file's ``library:<file id>`` (#313, #1754).
- ``printer_bed_types``: the plate last printed on, one row per printer.

``library_print_choices`` (#313) held a library file's choices until #1754 copied them
into ``model_print_choices`` (``migrations/20261009T0541Z_print_choices_by_subject.sql``);
it is no longer read or written.

The print dialog writes a model's choices and its printer's plate back to back on
every print, and FastAPI runs each on its own threadpool thread; each write is one
statement on its own row, so neither can drop the other's change.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import json
import logging
import time
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, LiteralString, Self, Union, get_args, get_origin

from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from scadbuddy.bambuddy.models import (
    DEFAULT_ALGORITHM,
    NozzleChoice,
    RackAlgorithm,
    SlotChoice,
    Tier,
)
from scadbuddy.bambuddy.options import OptionScope, PrintOptions, library_options_scope
from scadbuddy.core.config import StoreBackend
from scadbuddy.core.events import EventBus, SettingsChanged, SettingsSection, emit
from scadbuddy.core.pg_keepalive import TCP_KEEPALIVE
from scadbuddy.core.secrets import (
    Envelope,
    Kek,
    SealError,
    load_kek,
    open_secret,
    seal_secret,
)
from scadbuddy.core.settings import ENV_SEEDED, SECRET_FIELDS, Settings, check_value, env_var
from scadbuddy.library.asset_fetch import DEFAULT_ASSET_FETCH_DOMAINS, normalise_domain
from scadbuddy.render.pg_store import MIGRATION_LOCK, migrate

logger = logging.getLogger(__name__)

#: How the UI shows lengths. Only a display choice: geometry, the plate table and every
#: API value stay in millimetres, which is what OpenSCAD and Bambu Studio work in.
DisplayUnit = Literal["mm", "in"]

#: Where an env-seeded field's value comes from (#322): a value saved in the UI, the
#: deployment's ``SCADBUDDY_<FIELD>``, the built-in default, or a clear saved in the UI,
#: which the environment's value does not override.
SettingSource = Literal["stored", "env", "default", "cleared"]

#: The fields a render worker reads (spec §9): the store and the key it uses. The full
#: key only when no render key is stored, as the fallback.
RENDER_FIELDS = (
    "store_backend",
    "bambuddy_url",
    "bambuddy_render_api_key",
    "bambuddy_api_key",
    "library_folder_id",
)
#: The fields kept in tables of their own rather than as ``settings`` rows.
OWN_TABLES = frozenset({"model_print_choices", "printer_bed_types"})
#: Settings rows #312 retired with the slicer pipeline. A load already ignores them, as
#: it does any name it does not know; :meth:`SettingsStore.save` deletes them, so they do
#: not outlive the first write.
RETIRED = (
    "pipeline_id",
    "printer_preset",
    "process_preset",
    "filament_presets",
    "bed_type",
    "model_pipelines",
)

#: The ``settings`` rows that are remembered choices (#322), which "Forget all" drops.
REMEMBERED_ROWS = (
    "print_options",
    "printer_print_options",
    "model_print_options",
    "printer_rack_algorithms",
    "printer_rack_algorithm_versions",
)


def _nullable(name: str) -> bool:
    annotation = Settings.model_fields[name].annotation
    return get_origin(annotation) in (Union, types.UnionType) and type(None) in get_args(annotation)


#: The env-seeded fields a clear can hold (a JSON ``null`` row). The rest are numbers,
#: switches or a level, which only a reset puts back.
NULLABLE = frozenset(name for name in ENV_SEEDED if _nullable(name))


#: How long a printer's rack-algorithm save may wait for a connection, and then how
#: long its write may run (#1129). Together well under the print dialog's 25 s give-up
#: (``rackAlgorithmSave`` in ``frontend/src/api/client.ts``): the dialog then sends its
#: next choice, and a save it gave up on must not commit after that one. This bounds
#: only the server's database work, not time a request spends before it reaches the
#: store (a worker thread, a proxy): the save's version orders those (#1216).
RACK_ALGORITHM_WRITE_TIMEOUT = 5.0
#: The ``settings`` row holding each printer's last stored rack-algorithm save version
#: (#1216), beside ``printer_rack_algorithms`` rather than inside it, so that setting's
#: shape is unchanged. Not a ``StoredSettings`` field: a load skips names it does not know.
RACK_ALGORITHM_VERSIONS = "printer_rack_algorithm_versions"

#: The settings `StoreNotReadyError` is decided from.
STORE_READINESS = frozenset({"store_backend", "bambuddy_url", "library_folder_id"})
#: `pg_advisory_xact_lock` key (hashed) under which a save checks and writes them.
STORE_READINESS_LOCK = "scadbuddy:settings:store-readiness"


class RackAlgorithmSupersededError(Exception):
    """A rack-algorithm save older than the one the printer stores (#1216): refused, so
    the newer choice stays whatever order the two arrived in."""


class StoreNotReadyError(ValueError):
    """`store_backend = bambuddy` without the Bambuddy URL and inbox folder it needs."""


class BambuddyIds(BaseModel):
    """The Bambuddy object ids ScadBuddy points at.

    Integers, because that is what Bambuddy's own OpenAPI declares for ``folder_id``
    and ``printer_id`` — an earlier draft of this file typed them as strings.
    """

    library_folder_id: int | None = None
    printer_id: int | None = None


class ModelPrintChoices(BaseModel):
    """What the print dialog last chose for one model (#78). ``printer_id`` is the
    printer it printed on. ``filament_plan`` is only a plan the user moved off the
    auto-match: a spool no longer in the inventory is dropped by the picker, which then
    falls back to the auto-match for that slot.

    ``nozzles``, ``tier`` and ``process_name`` are the spool-first dialog's own choices
    (spec 2026-09-27 §7). All default to "nothing remembered", so a settings file
    written before them still loads; an empty ``nozzles`` means the dialog's default.
    """

    printer_id: int | None = None
    filament_plan: list[SlotChoice] = Field(default_factory=list)
    nozzles: list[NozzleChoice] = Field(default_factory=list, max_length=2)
    tier: Tier | None = None
    process_name: str | None = Field(default=None, max_length=200)

    @field_validator("nozzles")
    @classmethod
    def _both_sides(cls, nozzles: list[NozzleChoice]) -> list[NozzleChoice]:
        """None or two: the dialog has two sides, so one entry means it on both."""
        return [nozzles[0], nozzles[0].model_copy()] if len(nozzles) == 1 else nozzles


class StoredSettings(BambuddyIds):
    """Bambuddy connection details. The API key never leaves the server.

    A store from before #312 may still hold ``pipeline_id``, the four raw-preset keys or
    ``model_pipelines`` (:data:`RETIRED`); a load ignores them, and the next
    :meth:`SettingsStore.save` drops them.
    """

    bambuddy_url: str | None = None
    bambuddy_api_key: str | None = Field(default=None, repr=False)
    bambuddy_web_urls: str | None = None
    #: The Manage-Library-only key render workers hold (spec 2026-09-27 §9). Stored
    #: exactly as `bambuddy_api_key` is (sealed under SCADBUDDY_SECRET_KEY_FILE when it
    #: is set, #602), written from Settings, never sent to the browser.
    bambuddy_render_api_key: str | None = Field(default=None, repr=False)
    #: Where blobs live. Read at process start by the API and every worker.
    store_backend: StoreBackend = "local"
    public_url: str | None = None
    #: The printer model the preview's plate falls back to when no printer is chosen
    #: or it is not one ScadBuddy knows (#81). ``None`` is the 256 mm fallback plate.
    default_plate: str | None = None
    #: The unit the UI shows dimensions in, for every model.
    display_unit: DisplayUnit = "mm"
    #: How long a finished print run's row is kept (#1052, spec 2026-10-01 §5.4).
    #: ``None`` keeps every one: the rows are the start of print history.
    print_run_retention_seconds: float | None = None
    #: How long a finished operation's row is kept (#1053, spec 2026-10-01 §4.2).
    #: ``None`` keeps every one. Keep it at least the Temporal namespace's retention: a
    #: retry whose row is gone while its closed execution is not answers 409 "may have
    #: been done" rather than its outcome (review #1063 8).
    operation_retention_seconds: float | None = None
    #: The domains `POST /models/{slug}/assets/fetch` may fetch from (#844), each with
    #: its subdomains. ``None`` is :data:`DEFAULT_ASSET_FETCH_DOMAINS`; ``[]`` is none.
    asset_fetch_domains: list[str] | None = None

    #: Options scope (a model slug, or ``library:<file id>``, #1754) -> what the picker
    #: chose, set one at a time (:meth:`SettingsStore.set_model_choices`,
    #: :meth:`SettingsStore.set_library_choices`).
    model_print_choices: dict[str, ModelPrintChoices] = Field(default_factory=dict)
    #: Stringified Bambuddy printer id -> the plate type last printed on it (#83). Per
    #: printer, not per model: the plate is a property of the machine. Set one printer at
    #: a time (:meth:`SettingsStore.set_printer_bed_type`).
    printer_bed_types: dict[str, str] = Field(default_factory=dict)

    #: The Bambuddy project the last send went to (#79), and nothing more. A project
    #: is Bambuddy's grouping, not a second one kept here, so ScadBuddy remembers only
    #: enough to open the picker where it was left rather than modelling which models
    #: belong to which project — that question is Bambuddy's to answer.
    last_project_id: int | None = None

    # #88 — remembered print options, least to most specific. All three start empty, so
    # a ScadBuddy that has never been told otherwise queues with Bambuddy's own
    # defaults. The dict keys are strings because JSON has no integer keys: the printer
    # map is keyed by a stringified Bambuddy printer id, the model map by the print's
    # options scope: ScadBuddy's own model slug, or a library file's ``library:<file
    # id>`` (#1754, ``options.options_scope``).
    print_options: PrintOptions = Field(default_factory=PrintOptions)
    printer_print_options: dict[str, PrintOptions] = Field(default_factory=dict)
    model_print_options: dict[str, PrintOptions] = Field(default_factory=dict)
    #: Stringified Bambuddy printer id -> how ScadBuddy picks that printer's rack nozzle
    #: (#836). Stored like ``printer_print_options``, one key at a time in the jsonb
    #: ``settings`` row: no table of its own, and not in ``OWN_TABLES``.
    printer_rack_algorithms: dict[str, RackAlgorithm] = Field(default_factory=dict)

    @field_validator("printer_rack_algorithms", mode="before")
    @classmethod
    def _known_rack_algorithms(cls, value: Any) -> Any:
        """A value this version does not know (a newer one wrote it) is dropped, so it
        cannot stop the settings loading."""
        if not isinstance(value, dict):
            return {}
        known = get_args(RackAlgorithm)
        return {key: algorithm for key, algorithm in value.items() if algorithm in known}

    @field_validator("model_print_choices", mode="before")
    @classmethod
    def _readable_choices(cls, value: Any) -> Any:
        """A row this version cannot read is nothing remembered for its scope, rather
        than settings that will not load: the migration of ``library_print_choices``
        (#1754) copies rows verbatim, and the per-file read always tolerated them."""
        if not isinstance(value, dict):
            return {}
        kept: dict[str, ModelPrintChoices] = {}
        for scope, choices in value.items():
            try:
                kept[scope] = ModelPrintChoices.model_validate(choices)
            except ValidationError:
                logger.warning("unreadable print choices", extra={"options_scope": scope})
        return kept

    def rack_algorithm(self, printer_id: int | None) -> RackAlgorithm:
        """The printer's remembered rack algorithm, else Least used (spec §4)."""
        if printer_id is None:
            return DEFAULT_ALGORITHM
        return self.printer_rack_algorithms.get(str(printer_id), DEFAULT_ALGORITHM)

    @field_validator("asset_fetch_domains")
    @classmethod
    def _normalised_domains(cls, domains: list[str] | None) -> list[str] | None:
        """`host_allowed` compares against normalised entries. A save already
        normalises them (`SettingsPatch`); a row written any other way is normalised
        here, and an entry that is not a domain is dropped rather than failing the
        load of every setting."""
        if domains is None:
            return None
        kept: list[str] = []
        for domain in domains:
            try:
                kept.append(normalise_domain(domain))
            except ValueError:
                logger.warning("ignoring a stored asset domain that is not a domain")
        return list(dict.fromkeys(kept))

    def allowed_asset_domains(self) -> tuple[str, ...]:
        if self.asset_fetch_domains is None:
            return DEFAULT_ASSET_FETCH_DOMAINS
        return tuple(self.asset_fetch_domains)

    def render_bambuddy_key(self) -> tuple[str | None, bool]:
        """The key render workers use, and whether it is the full key by fallback. With
        no key at all there is nothing to fall back to: ``(None, False)``, as the view's
        ``render_key_fallback``."""
        if self.bambuddy_render_api_key:
            return self.bambuddy_render_api_key, False
        return self.bambuddy_api_key, self.bambuddy_api_key is not None


class SettingsPatch(BaseModel):
    """An omitted field is left alone; an explicit ``null`` clears it; ``reset`` names
    env-seeded fields to put back on the deployment's value (#322).

    A clear and a reset differ only for an env-seeded field: a clear is stored and beats
    ``SCADBUDDY_<FIELD>``, a reset deletes what is stored so the environment, then the
    default, applies again. Numbers and switches cannot be cleared, only reset. Every
    value is checked against the bounds a ``SCADBUDDY_<FIELD>`` meets
    (:func:`~scadbuddy.core.settings.check_value`), so a bad one is a 422 naming it. An
    unknown field, such as a bootstrap one, is refused rather than ignored; only a
    :data:`RETIRED` one, such as an older client's ``pipeline_id``, is dropped instead.
    """

    model_config = ConfigDict(extra="forbid")

    bambuddy_url: str | None = None
    bambuddy_api_key: str | None = None
    bambuddy_web_urls: str | None = None
    bambuddy_render_api_key: str | None = None
    public_url: str | None = None
    library_folder_id: int | None = None
    printer_id: int | None = None
    default_plate: str | None = None
    #: ``null`` puts it back to millimetres.
    display_unit: DisplayUnit | None = None
    #: At least a day, past the repeat window: a pruned row would turn a retry of a print
    #: (or an operation) that succeeded into a second one (review #1061).
    print_run_retention_seconds: float | None = Field(default=None, ge=86400)
    operation_retention_seconds: float | None = Field(default=None, ge=86400)
    #: The project a send without one goes to, and where the project picker opens.
    last_project_id: int | None = None
    #: The asset allowlist (#844); ``null`` puts the defaults back. Only the user sets
    #: it: no agent tool writes settings.
    asset_fetch_domains: list[str] | None = Field(default=None, max_length=200)

    # -- the runtime settings (#322), each env-seeded ---------------------------------
    render_timeout: float | None = None
    template_activity_max_timeout: float | None = None
    render_concurrency: int | None = None
    solid_concurrency: int | None = None
    render_queue_max: int | None = None
    render_queue_depth_slo: int | None = None
    render_latency_slo: float | None = None
    check_concurrency: int | None = None
    job_ttl: float | None = None
    preview_renders: bool | None = None
    lsp_sessions: int | None = None
    realtime_sockets: int | None = None
    library_max_bytes: int | None = None
    asset_max_total_bytes: int | None = None
    asset_max_count: int | None = None
    asset_sweep_grace: float | None = None
    asset_sweep_interval: float | None = None
    duplicate_staging_max_age: float | None = None
    media_upload_max_bytes: int | None = None
    read_max_objects: int | None = None
    read_max_visits: int | None = None
    read_max_triangles: int | None = None
    read_max_paint_digits: int | None = None
    #: Write-only, like the Bambuddy key; ``""`` clears it.
    google_fonts_api_key: str | None = None
    fonts_catalogue_ttl: float | None = None
    event_log_retention_seconds: float | None = None
    event_log_retention_rows: int | None = None
    log_level: str | None = None
    temporal_ui_url: str | None = None

    #: Where blobs live (spec 2026-09-27 §6.2) and the store's caps: read at start, so a
    #: change applies at the next one; a reset puts one back on the deployment's value.
    store_backend: StoreBackend | None = None
    store_max_total_bytes: int | None = None
    store_max_count: int | None = None
    worker_cache_max_bytes: int | None = None

    #: Env-seeded fields to put back on the deployment's value.
    reset: list[str] = Field(default_factory=list)

    @field_validator(*ENV_SEEDED, mode="after")
    @classmethod
    def _in_bounds(cls, value: Any, info: ValidationInfo) -> Any:
        name = info.field_name or ""
        if value is None:
            if name in NULLABLE:
                return None
            raise ValueError(
                f"{env_var(name)} cannot be cleared; reset it to follow the deployment's value"
            )
        return check_value(name, value)

    @field_validator("asset_fetch_domains")
    @classmethod
    def _domains(cls, domains: list[str] | None) -> list[str] | None:
        if domains is None:
            return None
        return list(dict.fromkeys(normalise_domain(domain) for domain in domains))

    @field_validator("reset")
    @classmethod
    def _resettable(cls, names: list[str]) -> list[str]:
        unknown = [name for name in names if name not in ENV_SEEDED]
        if unknown:
            raise ValueError(
                f"{', '.join(unknown)}: not an env-seeded setting, so nothing to reset"
            )
        return list(dict.fromkeys(names))

    @model_validator(mode="before")
    @classmethod
    def _drop_retired(cls, data: Any) -> Any:
        """An older client may still send a field #312 retired; that is not a mistake
        worth a 422, so it is dropped rather than refused."""
        if isinstance(data, dict):
            return {key: value for key, value in data.items() if key not in RETIRED}
        return data

    @model_validator(mode="after")
    def _set_or_reset(self) -> Self:
        both = sorted(set(self.reset) & (self.model_fields_set - {"reset"}))
        if both:
            raise ValueError(f"{', '.join(both)}: both set and reset in one save")
        return self


@dataclass(frozen=True)
class SettingsSnapshot:
    """One read of every setting (:meth:`SettingsStore.snapshot`)."""

    stored: StoredSettings
    #: Every field of :class:`Settings` as the store resolves it: the stored value, else
    #: the environment's, else the default. Bootstrap fields are the process's own.
    runtime: Settings
    #: Each env-seeded field's source.
    sources: dict[str, SettingSource]


class SettingsStore:
    """The settings, one row each, in Postgres.

    The environment seeds the initial values; once a value is stored it wins, so the
    UI can change what a deployment shipped with. For an :data:`ENV_SEEDED` field that
    means: a stored value wins; a field the UI explicitly cleared stays cleared (#81 — a
    Settings page option that clears ``default_plate`` has to beat
    ``SCADBUDDY_DEFAULT_PLATE``); and a field never stored still follows the
    environment, so a variable added to a deployment later is honoured.

    Every write commits before ``settings.changed`` is published, so a listener that
    re-reads sees it.
    """

    def __init__(
        self,
        defaults: Settings,
        *,
        events: EventBus | None = None,
        connect_timeout: float = 30.0,
    ) -> None:
        self.defaults = defaults
        #: Told of every write, as ``settings.changed`` with the section it touched.
        self.events = events
        self.connect_timeout = connect_timeout
        #: Seals the `SECRET_FIELDS` at rest (#602); None keeps them plaintext.
        self._kek = settings_kek(defaults.secret_key_file)
        self._pool: ConnectionPool[Connection[DictRow]] = ConnectionPool(
            defaults.database_url,
            min_size=1,
            max_size=defaults.database_pool_size,
            open=False,
            connection_class=Connection[DictRow],
            kwargs={"autocommit": True, "row_factory": dict_row, **TCP_KEEPALIVE},
            name="scadbuddy-settings",
        )

    def open(self) -> None:
        """Connect and bring the schema up to date. Fails the start when it cannot."""
        self._pool.open(wait=True, timeout=self.connect_timeout)
        with self._pool.connection() as conn:
            applied = migrate(conn)
            if applied:
                logger.info("applied database migrations", extra={"versions": applied})
            if self._kek is None:
                logger.warning(
                    "SCADBUDDY_SECRET_KEY_FILE is not set: the stored API keys are kept in"
                    " plaintext"
                )
            else:
                sealed = _seal_plaintext_rows(conn, self._kek)
                if sealed:
                    logger.info("sealed plaintext API keys", extra={"settings": sealed})

    def close(self) -> None:
        self._pool.close()

    def _source(self, name: str) -> SettingSource:
        return "env" if name in self.defaults.model_fields_set else "default"

    @property
    def pool(self) -> ConnectionPool[Connection[DictRow]]:
        return self._pool

    def snapshot(self, timeout: float | None = None) -> SettingsSnapshot:
        """``timeout``, in seconds, is this read's whole budget (#1111): the wait for a
        connection and every statement after it share one deadline. A pool wait that runs
        out raises ``PoolTimeout``; a statement that does raises ``QueryCanceled``.

        It bounds a read Postgres is slow to answer (a held lock, a slow plan), since
        ``statement_timeout`` is enforced by the server. The bound starts at the first
        ``set_config``, so the ``BEGIN`` and ``SET TRANSACTION`` before it are not bounded
        by it. A connection that gets no reply
        at all (a half-open socket) is not bounded here: that is #1226. It bounds this
        read only: a pool-wide statement timeout would also cut short the saves'
        deliberate lock waits and the migration."""
        deadline = None if timeout is None else time.monotonic() + timeout
        # One snapshot across the three tables, so a load never pairs a model's new
        # choices with a plate from before the same print.
        with self._pool.connection(timeout=timeout) as conn, conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")

            def read(query: LiteralString) -> list[DictRow]:
                if deadline is not None:
                    # statement_timeout bounds each statement on its own, so each one
                    # gets what is left of the read's budget.
                    left = max(1, int((deadline - time.monotonic()) * 1000))
                    conn.execute("SELECT set_config('statement_timeout', %s, true)", [str(left)])
                return conn.execute(query).fetchall()

            rows = read("SELECT name, value FROM settings")
            choices = read("SELECT model_id, choices FROM model_print_choices")
            beds = read("SELECT printer_id, bed_type FROM printer_bed_types")
        stored_rows = _opened(self._kek, {row["name"]: row["value"] for row in rows})
        runtime = self.defaults.model_copy()
        sources: dict[str, SettingSource] = {}
        for name in ENV_SEEDED:
            if name not in stored_rows:
                sources[name] = self._source(name)
                continue
            value = stored_rows[name]
            try:
                coerced = check_value(name, value)
            except ValueError:
                # Written by a version with other bounds, or by hand: the environment's
                # value is a better answer than a process that will not start.
                logger.warning(
                    "ignoring a stored setting this version refuses", extra={"setting": name}
                )
                sources[name] = self._source(name)
                continue
            Settings.__pydantic_validator__.validate_assignment(runtime, name, coerced)
            sources[name] = "cleared" if value is None else "stored"
        values: dict[str, Any] = {
            name: getattr(runtime, name)
            for name in ENV_SEEDED
            if name in StoredSettings.model_fields
        }
        for name, value in stored_rows.items():
            # A name this version does not know (a newer one wrote it) is left alone.
            if (
                name in StoredSettings.model_fields
                and name not in OWN_TABLES
                and name not in ENV_SEEDED
            ):
                values[name] = value
        values["model_print_choices"] = {row["model_id"]: row["choices"] for row in choices}
        values["printer_bed_types"] = {str(row["printer_id"]): row["bed_type"] for row in beds}
        return SettingsSnapshot(
            stored=StoredSettings.model_validate(values), runtime=runtime, sources=sources
        )

    def load(self, timeout: float | None = None) -> StoredSettings:
        return self.snapshot(timeout).stored

    def _written(self, section: SettingsSection) -> StoredSettings:
        """Announce a committed write and read the settings back."""
        emit(self.events, SettingsChanged(section=section))
        return self.load()

    def save(self, patch: SettingsPatch) -> StoredSettings:
        # exclude_unset, not exclude_none: an omitted key leaves the stored value
        # alone, while an explicit null clears it. Without that an id could be set
        # but never unset.
        changes = patch.model_dump(mode="json", exclude_unset=True)
        reset = changes.pop("reset", [])
        for secret in SECRET_FIELDS:
            if changes.get(secret) == "":
                changes[secret] = None
        if changes.get("store_backend") == "bambuddy":
            current = self.load()
            url = changes.get("bambuddy_url", current.bambuddy_url)
            inbox = changes.get("library_folder_id", current.library_folder_id)
            if not url or inbox is None:
                raise StoreNotReadyError(
                    "the Bambuddy store needs a Bambuddy URL and a library folder (its inbox)"
                    " saved first"
                )
        with self._pool.connection() as conn, conn.transaction():
            if (changes.keys() | set(reset)) & STORE_READINESS:
                self._check_store_ready(conn, changes, reset)
            conn.execute("DELETE FROM settings WHERE name = ANY(%s)", (list(RETIRED),))
            for name in reset:
                # Back to following the environment, then the default.
                conn.execute("DELETE FROM settings WHERE name = %s", (name,))
            for name, value in changes.items():
                if value is not None:
                    _put(conn, name, _sealed(self._kek, name, value))
                elif name in ENV_SEEDED:
                    # Cleared, which must beat the environment: a JSON null row.
                    _put(conn, name, None)
                else:
                    # Back to the default.
                    conn.execute("DELETE FROM settings WHERE name = %s", (name,))
        return self._written("connection")

    def _check_store_ready(
        self, conn: Connection[DictRow], changes: dict[str, Any], reset: list[str]
    ) -> None:
        """The merged result, not the patch: clearing the URL or the inbox while on the
        Bambuddy store would leave a store the next start refuses (`build_store`). A
        reset name is part of it: its row goes, and the value is the deployment's.

        Read under `STORE_READINESS_LOCK`, in the write's own transaction: two saves each
        safe alone (one clears the URL, one switches to Bambuddy) serialize, and the
        second sees the first. A row lock would not do: a value that follows the
        environment has no row to lock."""
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (STORE_READINESS_LOCK,)
        )
        rows = conn.execute(
            "SELECT name, value FROM settings WHERE name = ANY(%s)", (list(STORE_READINESS),)
        ).fetchall()
        stored = {row["name"]: row["value"] for row in rows}

        def deployment(name: str) -> Any:
            # The environment's value, else the default. The inbox is not env-seeded,
            # so the deployment's `Settings` has no such attribute (#1253).
            if name in ENV_SEEDED:
                return getattr(self.defaults, name)
            return StoredSettings.model_fields[name].get_default(call_default_factory=True)

        def merged(name: str) -> Any:
            if name in changes:
                return changes[name]
            if name in reset or name not in stored:
                return deployment(name)
            if name not in ENV_SEEDED:
                # Read as `snapshot` reads it: as stored, with no env check to pass.
                return stored[name]
            try:
                return check_value(name, stored[name])
            except ValueError:
                # As `snapshot` reads it: a refused row follows the environment.
                return deployment(name)

        backend = merged("store_backend") or "local"
        if backend == "bambuddy" and (
            not merged("bambuddy_url") or merged("library_folder_id") is None
        ):
            raise StoreNotReadyError(
                "the Bambuddy store needs a Bambuddy URL and a library folder (its inbox)"
                " saved first"
            )

    def _put_choices(self, scope: str, choices: ModelPrintChoices) -> None:
        """Remember one options scope's choices (#1754); an empty ``choices`` forgets."""
        with self._pool.connection() as conn:
            if choices == ModelPrintChoices():
                conn.execute("DELETE FROM model_print_choices WHERE model_id = %s", (scope,))
            else:
                conn.execute(
                    "INSERT INTO model_print_choices (model_id, choices) VALUES (%s, %s)"
                    " ON CONFLICT (model_id) DO UPDATE"
                    " SET choices = EXCLUDED.choices, updated_at = now()",
                    (scope, Jsonb(choices.model_dump(mode="json"))),
                )

    def set_model_choices(self, slug: str, choices: ModelPrintChoices) -> StoredSettings:
        """Remember one model's printer and spools; an empty ``choices`` forgets them."""
        self._put_choices(slug, choices)
        return self._written("model_choices")

    def set_printer_bed_type(self, printer_id: int, bed_type: str | None) -> StoredSettings:
        """Remember the plate on one printer; ``None`` forgets it."""
        with self._pool.connection() as conn:
            if bed_type is None:
                conn.execute("DELETE FROM printer_bed_types WHERE printer_id = %s", (printer_id,))
            else:
                conn.execute(
                    "INSERT INTO printer_bed_types (printer_id, bed_type) VALUES (%s, %s)"
                    " ON CONFLICT (printer_id) DO UPDATE"
                    " SET bed_type = EXCLUDED.bed_type, updated_at = now()",
                    (printer_id, bed_type),
                )
        return self._written("printer_bed_type")

    def set_printer_rack_algorithm(
        self, printer_id: int, algorithm: RackAlgorithm | None, version: int | None = None
    ) -> RackAlgorithm:
        """Remember how one printer's rack nozzle is picked (#836); ``None`` forgets it.
        Returns the printer's algorithm now.

        With a ``version`` (#1216), compare and set: a save older than the printer's
        stored version raises :class:`RackAlgorithmSupersededError` and changes nothing,
        whenever it arrives. The same version is accepted again, so a save that failed
        with nothing known can be resent as it was. Without one, the save is unordered
        (an older client) and leaves the stored version alone.

        Bounded by ``RACK_ALGORITHM_WRITE_TIMEOUT`` for the pool wait and again for the
        write, so it commits well inside the print dialog's give-up or not at all
        (#1129). Nothing is read back afterwards: an unbounded read of every setting
        could outlast the give-up on a save that has already landed."""
        bound_ms = int(RACK_ALGORITHM_WRITE_TIMEOUT * 1000)
        key = str(printer_id)
        with (
            self._pool.connection(timeout=RACK_ALGORITHM_WRITE_TIMEOUT) as conn,
            conn.transaction(),
        ):
            conn.execute(f"SET LOCAL statement_timeout = {bound_ms}")
            if version is not None:
                # The versions row's lock, held to the commit, orders two saves of one
                # printer; a refused upsert updates and returns nothing.
                taken = conn.execute(
                    "INSERT INTO settings (name, value)"
                    " VALUES (%s, jsonb_build_object(%s::text, %s::bigint))"
                    " ON CONFLICT (name) DO UPDATE"
                    " SET value = settings.value || EXCLUDED.value, updated_at = now()"
                    " WHERE coalesce((settings.value ->> %s)::bigint, -1) <= %s"
                    " RETURNING 1",
                    (RACK_ALGORITHM_VERSIONS, key, version, key, version),
                ).fetchone()
                if taken is None:
                    raise RackAlgorithmSupersededError(
                        f"a newer rack-algorithm save is stored for printer {printer_id}"
                    )
            _put_entry(conn, "printer_rack_algorithms", key, algorithm)
        emit(self.events, SettingsChanged(section="printer_rack_algorithm"))
        return algorithm or DEFAULT_ALGORITHM

    def library_choices(self, file_id: int) -> ModelPrintChoices:
        """What the dialog last chose for one Bambuddy library file (#313); nothing
        remembered is the empty choice. A row this version cannot read is nothing
        remembered too, rather than a dialog that will not open.

        In the one store, under the file's options scope (#1754): one row is read,
        not every setting."""
        scope = library_options_scope(file_id)
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT choices FROM model_print_choices WHERE model_id = %s", (scope,)
            ).fetchone()
        if row is None:
            return ModelPrintChoices()
        try:
            return ModelPrintChoices.model_validate(row["choices"])
        except ValidationError:
            logger.warning("unreadable library print choices", extra={"file_id": file_id})
            return ModelPrintChoices()

    def set_library_choices(self, file_id: int, choices: ModelPrintChoices) -> ModelPrintChoices:
        """Remember one library file's choices; an empty ``choices`` forgets them."""
        self._put_choices(library_options_scope(file_id), choices)
        # The dialog's remembered choices, as for a model: no new section is needed.
        emit(self.events, SettingsChanged(section="model_choices"))
        return self.library_choices(file_id)

    def remember_project(self, project_id: int | None) -> StoredSettings:
        """Remember the project the last send went to, so the picker opens on it."""
        with self._pool.connection() as conn:
            if project_id is None:
                conn.execute("DELETE FROM settings WHERE name = 'last_project_id'")
            else:
                _put(conn, "last_project_id", project_id)
        return self._written("last_project")

    def save_print_options(
        self, scope: OptionScope, key: str | None, options: PrintOptions
    ) -> StoredSettings:
        """Replace one scope's print-option overrides.

        Deliberately not part of :class:`SettingsPatch`, for the same reason
        :meth:`set_model_choices` is not: a patch replaces a whole value, so the browser
        would have to send every printer and model back and would lose any it had not
        loaded. An all-unset ``options`` **removes** the scope rather than storing an
        empty object, so the settings do not accumulate an entry per printer someone
        once opened the disclosure for.
        """
        value = None if options.is_empty() else options.model_dump(mode="json")
        with self._pool.connection() as conn:
            if scope == "global":
                if value is None:
                    conn.execute("DELETE FROM settings WHERE name = 'print_options'")
                else:
                    _put(conn, "print_options", value)
            else:
                if not key:  # pragma: no cover - the route validates this first
                    raise ValueError(f"the {scope!r} scope needs a key")
                field = "printer_print_options" if scope == "printer" else "model_print_options"
                _put_entry(conn, field, key, value)
        return self._written("print_options")

    def forget_remembered(self) -> StoredSettings:
        """Forget every remembered choice (#322): the per-model and per-library-file
        (#313) print-dialog choices, the per-printer plates, and the print options at
        every scope. The settings themselves are left alone."""
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("DELETE FROM model_print_choices")
            conn.execute("DELETE FROM printer_bed_types")
            conn.execute("DELETE FROM settings WHERE name = ANY(%s)", (list(REMEMBERED_ROWS),))
        return self._written("remembered")


#: The jsonb a sealed secret is stored as: ``{"sealed": {"secret", "dek", "kek_id"}}``,
#: the first two base64. A plaintext secret is a JSON string, as before #602.
SEALED = "sealed"


@functools.cache
def settings_kek(path: Path | None) -> Kek | None:
    """The key the secret settings are sealed with, read once per process. A key file
    that is set but unreadable or malformed raises `SecretKeyError`, failing the start."""
    return None if path is None else load_kek(path)


def _aad(name: str) -> str:
    """Binds a sealed value to its setting: copied into another row, it does not open."""
    return f"settings:{name}"


def _sealed(kek: Kek | None, name: str, value: object) -> object:
    """``value`` as stored: sealed when it is a secret and there is a key."""
    if kek is None or name not in SECRET_FIELDS or not isinstance(value, str):
        return value
    envelope = seal_secret(kek, value, _aad(name))
    return {
        SEALED: {
            "secret": base64.b64encode(envelope.secret_sealed).decode(),
            "dek": base64.b64encode(envelope.dek_sealed).decode(),
            "kek_id": envelope.kek_id,
        }
    }


#: The sealed values `_opened` has already warned about, by setting and a digest. At
#: most one per secret field per value stored while this process runs: no bound needed.
_UNOPENED: set[tuple[str, str]] = set()


def _opened(kek: Kek | None, rows: dict[str, Any]) -> dict[str, Any]:
    """``rows`` with each sealed secret opened. One this process cannot open (no key,
    another key, altered bytes) is dropped with a warning, so the field follows the
    environment, as a row this version refuses does."""
    out = dict(rows)
    for name in SECRET_FIELDS & rows.keys():
        value = rows[name]
        if not (isinstance(value, dict) and SEALED in value):
            continue
        del out[name]
        try:
            if kek is None:
                raise SealError("SCADBUDDY_SECRET_KEY_FILE is not set")
            sealed = value[SEALED]
            envelope = Envelope(
                secret_sealed=base64.b64decode(sealed["secret"]),
                dek_sealed=base64.b64decode(sealed["dek"]),
                kek_id=str(sealed["kek_id"]),
            )
            out[name] = open_secret(kek, envelope, _aad(name))
        except (SealError, KeyError, TypeError, ValueError) as error:
            # Once per stored value: this runs on every read of the settings.
            seen = (name, hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest())
            if seen in _UNOPENED:
                continue
            _UNOPENED.add(seen)
            # A SealError names key ids only; the others are not formatted at all.
            reason = str(error) if isinstance(error, SealError) else "malformed"
            logger.warning(
                "ignoring a stored secret this process cannot open: %s",
                reason,
                extra={"setting": name},
            )
    return out


def _seal_plaintext_rows(conn: Connection[DictRow], kek: Kek) -> list[str]:
    """Seal every secret row still in plaintext (#602), under the migration lock, so
    replicas starting together seal each row once. A sealed or cleared row is left
    alone, so a second run changes nothing."""
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK,))
        rows = conn.execute(
            "SELECT name, value FROM settings"
            " WHERE name = ANY(%s) AND jsonb_typeof(value) = 'string' FOR UPDATE",
            (sorted(SECRET_FIELDS),),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE settings SET value = %s, updated_at = now() WHERE name = %s",
                (Jsonb(_sealed(kek, row["name"], row["value"])), row["name"]),
            )
    return sorted(row["name"] for row in rows)


def _put(conn: Connection[DictRow], name: str, value: object) -> None:
    """Store one setting's value; ``None`` stores a JSON ``null`` (cleared)."""
    conn.execute(
        "INSERT INTO settings (name, value) VALUES (%s, %s)"
        " ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (name, Jsonb(value)),
    )


def _put_entry(conn: Connection[DictRow], name: str, key: str, value: object) -> None:
    """Set (or, for ``None``, remove) one key of a map-valued setting.

    Merged inside the upsert, under the row's lock, so two writers changing different
    keys at once both land.
    """
    if value is None:
        conn.execute(
            "UPDATE settings SET value = value - %s, updated_at = now() WHERE name = %s",
            (key, name),
        )
        return
    conn.execute(
        "INSERT INTO settings (name, value) VALUES (%s, jsonb_build_object(%s::text, %s::jsonb))"
        " ON CONFLICT (name) DO UPDATE"
        " SET value = settings.value || EXCLUDED.value, updated_at = now()",
        (name, key, Jsonb(value)),
    )


class RenderStoreSettings(BaseModel):
    """What a render worker knows of the settings, and nothing more (spec §9)."""

    store_backend: StoreBackend = "local"
    bambuddy_url: str | None = None
    api_key: str | None = Field(default=None, repr=False)
    #: True when `api_key` is the full key because no render key is stored.
    key_is_fallback: bool = False
    library_folder_id: int | None = None


def load_render_store_settings(
    pool: ConnectionPool[Connection[DictRow]], defaults: Settings
) -> RenderStoreSettings:
    """Read only `RENDER_FIELDS`: a worker holds no settings store (spec §9). Through
    the `render_settings` view, all of the settings the render worker's own role may
    read (#601); the full key is in it only while no render key is stored."""
    kek = settings_kek(defaults.secret_key_file)
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT name, value FROM render_settings WHERE name = ANY(%s)",
            (list(RENDER_FIELDS),),
        ).fetchall()
    values: dict[str, Any] = {
        name: getattr(defaults, name) for name in ENV_SEEDED if name in RENDER_FIELDS
    }
    # A JSON null is a field cleared in Settings: it beats the environment's seed.
    values.update(_opened(kek, {row["name"]: row["value"] for row in rows}))
    stored = StoredSettings.model_validate(values)
    key, fallback = stored.render_bambuddy_key()
    return RenderStoreSettings(
        store_backend=stored.store_backend,
        bambuddy_url=stored.bambuddy_url,
        api_key=key,
        key_is_fallback=fallback,
        library_folder_id=stored.library_folder_id,
    )
