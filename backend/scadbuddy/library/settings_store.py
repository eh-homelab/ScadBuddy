"""The settings, in Postgres (``SCADBUDDY_DATABASE_URL``; formerly data/settings.json).

Three tables, all in ``migrations/20260928T0840Z_settings.sql``:

- ``settings``: one row per setting, ``name -> value`` (jsonb). No row is "never set":
  the environment's value for an :data:`ENV_SEEDED` field, else the default. A JSON
  ``null`` row is an env-seeded field the UI cleared, which must outlast the
  environment's value. Map-valued settings (the per-printer and per-model print
  options) change one key at a time inside that row's upsert.
- ``model_print_choices``: what the print dialog last chose, one row per model.
- ``printer_bed_types``: the plate last printed on, one row per printer.

The print dialog writes a model's choices and its printer's plate back to back on
every print, and FastAPI runs each on its own threadpool thread; each write is one
statement on its own row, so neither can drop the other's change.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field, field_validator

from scadbuddy.bambuddy.models import NozzleChoice, SlotChoice, Tier
from scadbuddy.bambuddy.options import OptionScope, PrintOptions
from scadbuddy.core.events import EventBus, SettingsChanged, SettingsSection, emit
from scadbuddy.core.settings import Settings
from scadbuddy.render.pg_store import migrate

logger = logging.getLogger(__name__)

#: How the UI shows lengths. Only a display choice: geometry, the plate table and every
#: API value stay in millimetres, which is what OpenSCAD and Bambu Studio work in.
DisplayUnit = Literal["mm", "in"]

#: The fields the environment seeds (``SCADBUDDY_<FIELD>``); see :class:`SettingsStore`.
ENV_SEEDED = (
    "bambuddy_url",
    "bambuddy_api_key",
    "public_url",
    "default_plate",
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
    bambuddy_api_key: str | None = None
    public_url: str | None = None
    #: The printer model the preview's plate falls back to when no printer is chosen
    #: or it is not one ScadBuddy knows (#81). ``None`` is the 256 mm fallback plate.
    default_plate: str | None = None
    #: The unit the UI shows dimensions in, for every model.
    display_unit: DisplayUnit = "mm"

    #: Model slug -> what the picker chose, set one model at a time
    #: (:meth:`SettingsStore.set_model_choices`).
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
    # map is keyed by a stringified Bambuddy printer id, the model map by ScadBuddy's
    # own model slug.
    print_options: PrintOptions = Field(default_factory=PrintOptions)
    printer_print_options: dict[str, PrintOptions] = Field(default_factory=dict)
    model_print_options: dict[str, PrintOptions] = Field(default_factory=dict)


class SettingsPatch(BaseModel):
    """An omitted field is left alone; an explicit ``null`` clears it.

    Keys it does not declare, such as an older client's ``pipeline_id``, are ignored.
    """

    bambuddy_url: str | None = None
    bambuddy_api_key: str | None = None
    public_url: str | None = None
    library_folder_id: int | None = None
    printer_id: int | None = None
    default_plate: str | None = None
    #: ``null`` puts it back to millimetres.
    display_unit: DisplayUnit | None = None


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
        self._pool: ConnectionPool[Connection[DictRow]] = ConnectionPool(
            defaults.database_url,
            min_size=1,
            max_size=defaults.database_pool_size,
            open=False,
            connection_class=Connection[DictRow],
            kwargs={"autocommit": True, "row_factory": dict_row},
            name="scadbuddy-settings",
        )

    def open(self) -> None:
        """Connect and bring the schema up to date. Fails the start when it cannot."""
        self._pool.open(wait=True, timeout=self.connect_timeout)
        with self._pool.connection() as conn:
            applied = migrate(conn)
        if applied:
            logger.info("applied database migrations", extra={"versions": applied})

    def close(self) -> None:
        self._pool.close()

    def _from_env(self) -> dict[str, Any]:
        return {name: getattr(self.defaults, name) for name in ENV_SEEDED}

    def load(self) -> StoredSettings:
        # One snapshot across the three tables, so a load never pairs a model's new
        # choices with a plate from before the same print.
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            rows = conn.execute("SELECT name, value FROM settings").fetchall()
            choices = conn.execute("SELECT model_id, choices FROM model_print_choices").fetchall()
            beds = conn.execute("SELECT printer_id, bed_type FROM printer_bed_types").fetchall()
        values = self._from_env()
        for row in rows:
            # A name this version does not know (a newer one wrote it) is left alone.
            if row["name"] in StoredSettings.model_fields and row["name"] not in OWN_TABLES:
                values[row["name"]] = row["value"]
        values["model_print_choices"] = {row["model_id"]: row["choices"] for row in choices}
        values["printer_bed_types"] = {str(row["printer_id"]): row["bed_type"] for row in beds}
        return StoredSettings.model_validate(values)

    def _written(self, section: SettingsSection) -> StoredSettings:
        """Announce a committed write and read the settings back."""
        emit(self.events, SettingsChanged(section=section))
        return self.load()

    def save(self, patch: SettingsPatch) -> StoredSettings:
        # exclude_unset, not exclude_none: an omitted key leaves the stored value
        # alone, while an explicit null clears it. Without that an id could be set
        # but never unset.
        changes = patch.model_dump(mode="json", exclude_unset=True)
        if changes.get("bambuddy_api_key") == "":
            changes["bambuddy_api_key"] = None
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("DELETE FROM settings WHERE name = ANY(%s)", (list(RETIRED),))
            for name, value in changes.items():
                if value is not None:
                    _put(conn, name, value)
                elif name in ENV_SEEDED:
                    # Cleared, which must beat the environment: a JSON null row.
                    _put(conn, name, None)
                else:
                    # Back to the default, or to following the environment.
                    conn.execute("DELETE FROM settings WHERE name = %s", (name,))
        return self._written("connection")

    def set_model_choices(self, slug: str, choices: ModelPrintChoices) -> StoredSettings:
        """Remember one model's printer and spools; an empty ``choices`` forgets them."""
        with self._pool.connection() as conn:
            if choices == ModelPrintChoices():
                conn.execute("DELETE FROM model_print_choices WHERE model_id = %s", (slug,))
            else:
                conn.execute(
                    "INSERT INTO model_print_choices (model_id, choices) VALUES (%s, %s)"
                    " ON CONFLICT (model_id) DO UPDATE"
                    " SET choices = EXCLUDED.choices, updated_at = now()",
                    (slug, Jsonb(choices.model_dump(mode="json"))),
                )
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
