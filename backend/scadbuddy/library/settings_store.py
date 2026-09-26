from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from scadbuddy.bambuddy.models import PresetRef
from scadbuddy.bambuddy.options import OptionScope, PrintOptions
from scadbuddy.core.settings import Settings

SETTINGS_NAME = "settings.json"
KEY_FILE_MODE = 0o600

#: The fields the environment seeds (``SCADBUDDY_<FIELD>``); see :class:`SettingsStore`.
ENV_SEEDED = ("bambuddy_url", "bambuddy_api_key", "public_url", "default_plate")


class BambuddyIds(BaseModel):
    """The Bambuddy object ids ScadBuddy points at.

    Integers, because that is what Bambuddy's own OpenAPI declares for
    ``folder_id``, ``pipeline_id`` and ``printer_id`` — an earlier draft of this
    file typed them as strings.
    """

    library_folder_id: int | None = None
    pipeline_id: int | None = None
    printer_id: int | None = None


class StoredSettings(BambuddyIds):
    """Bambuddy connection details. The API key never leaves the server."""

    bambuddy_url: str | None = None
    bambuddy_api_key: str | None = None
    public_url: str | None = None
    #: The printer model the preview's plate falls back to when no printer is chosen
    #: or it is not one ScadBuddy knows (#81). ``None`` is the 256 mm fallback plate.
    default_plate: str | None = None

    # Used by "Slice and queue" when no pipeline is configured.
    printer_preset: PresetRef | None = None
    process_preset: PresetRef | None = None
    filament_presets: list[PresetRef] = Field(default_factory=list)
    bed_type: str | None = None

    #: Model slug -> the pipeline that model prints with, which wins over
    #: ``pipeline_id``. Deliberately not part of :class:`SettingsPatch`: a patch
    #: replaces a whole value, while a per-model default has to be settable one model
    #: at a time, so :meth:`SettingsStore.set_model_pipeline` is the only way in.
    model_pipelines: dict[str, int] = Field(default_factory=dict)

    #: The Bambuddy project the last send went to (#79), and nothing more. A project
    #: is Bambuddy's grouping, not a second one kept here, so ScadBuddy remembers only
    #: enough to open the picker where it was left rather than modelling which models
    #: belong to which project — that question is Bambuddy's to answer.
    last_project_id: int | None = None

    def pipeline_for(self, slug: str) -> int | None:
        """This model's own pipeline, else the global fallback (#86)."""
        return self.model_pipelines.get(slug, self.pipeline_id)

    # #88 — remembered print options, least to most specific. All three start empty, so
    # a ScadBuddy that has never been told otherwise queues with Bambuddy's own
    # defaults. The dict keys are strings because JSON has no integer keys: the printer
    # map is keyed by a stringified Bambuddy printer id, the model map by ScadBuddy's
    # own model slug.
    print_options: PrintOptions = Field(default_factory=PrintOptions)
    printer_print_options: dict[str, PrintOptions] = Field(default_factory=dict)
    model_print_options: dict[str, PrintOptions] = Field(default_factory=dict)

    #: The :data:`ENV_SEEDED` fields a ``PUT /settings`` explicitly cleared. The file
    #: stores every field, so a ``null`` in it cannot tell "cleared" from "never set";
    #: this list can, and it is what lets a clear outlast the environment's value.
    cleared: list[str] = Field(default_factory=list)


class SettingsPatch(BaseModel):
    """An omitted field is left alone; an explicit ``null`` clears it."""

    bambuddy_url: str | None = None
    bambuddy_api_key: str | None = None
    public_url: str | None = None
    library_folder_id: int | None = None
    pipeline_id: int | None = None
    printer_id: int | None = None
    printer_preset: PresetRef | None = None
    process_preset: PresetRef | None = None
    filament_presets: list[PresetRef] | None = None
    bed_type: str | None = None
    default_plate: str | None = None


class SettingsStore:
    """``data/settings.json``, mode 0600 because it holds the API key.

    The environment seeds the initial values; once the file exists it wins, so the
    UI can change what a deployment shipped with. For an :data:`ENV_SEEDED` field that
    means: a value in the file wins; a field the UI explicitly cleared stays cleared
    (#81 — a Settings page option that clears ``default_plate`` has to beat
    ``SCADBUDDY_DEFAULT_PLATE``); and a field the file has never held a value for still
    follows the environment, so a variable added to a deployment later is honoured.
    """

    def __init__(self, path: Path, defaults: Settings) -> None:
        self.path = path
        self.defaults = defaults

    def _from_env(self) -> StoredSettings:
        return StoredSettings(
            bambuddy_url=self.defaults.bambuddy_url,
            bambuddy_api_key=self.defaults.bambuddy_api_key,
            public_url=self.defaults.public_url,
            default_plate=self.defaults.default_plate,
        )

    def load(self) -> StoredSettings:
        stored = self._from_env()
        if not self.path.is_file():
            return stored
        on_disk = StoredSettings.model_validate_json(self.path.read_text(encoding="utf-8"))
        merged = stored.model_dump()
        merged.update(on_disk.model_dump(exclude_none=True))
        for name in on_disk.cleared:
            merged[name] = None
        return StoredSettings.model_validate(merged)

    def save(self, patch: SettingsPatch) -> StoredSettings:
        current = self.load().model_dump()
        # exclude_unset, not exclude_none: an omitted key leaves the stored value
        # alone, while an explicit null clears it. Without that an id could be set
        # but never unset.
        changes = patch.model_dump(mode="json", exclude_unset=True)
        if changes.get("bambuddy_api_key") == "":
            changes["bambuddy_api_key"] = None
        cleared = set(current["cleared"])
        for name in ENV_SEEDED:
            if name not in changes:
                continue
            if changes[name] is None:
                cleared.add(name)
            else:
                cleared.discard(name)
        current.update(changes, cleared=sorted(cleared))
        return self._write(StoredSettings.model_validate(current))

    def set_model_pipeline(self, slug: str, pipeline_id: int | None) -> StoredSettings:
        """Point one model at a pipeline, or clear it back to the global fallback.

        One slug at a time rather than through :class:`SettingsPatch`, which would make
        the browser send the whole map back and lose any entry it had not loaded.
        """
        settings = self.load()
        pipelines = dict(settings.model_pipelines)
        if pipeline_id is None:
            pipelines.pop(slug, None)
        else:
            pipelines[slug] = pipeline_id
        return self._write(settings.model_copy(update={"model_pipelines": pipelines}))

    def remember_project(self, project_id: int | None) -> StoredSettings:
        """Remember the project the last send went to, so the picker opens on it."""
        return self._write(self.load().model_copy(update={"last_project_id": project_id}))

    def _write(self, settings: StoredSettings) -> StoredSettings:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(settings.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
        )
        self.path.chmod(KEY_FILE_MODE)
        return settings

    def save_print_options(
        self, scope: OptionScope, key: str | None, options: PrintOptions
    ) -> StoredSettings:
        """Replace one scope's print-option overrides.

        Deliberately not part of :class:`SettingsPatch`, for the same reason
        :meth:`set_model_pipeline` is not: a patch replaces a whole value, so the browser
        would have to send every printer and model back and would lose any it had not
        loaded. An all-unset ``options`` **removes** the scope rather than storing an
        empty object, so the file does not accumulate a row per printer someone once
        opened the disclosure for.
        """
        settings = self.load()
        if scope == "global":
            return self._write(settings.model_copy(update={"print_options": options}))
        if not key:  # pragma: no cover - the route validates this first
            raise ValueError(f"the {scope!r} scope needs a key")
        field = "printer_print_options" if scope == "printer" else "model_print_options"
        mapping = dict(getattr(settings, field))
        if options.is_empty():
            mapping.pop(key, None)
        else:
            mapping[key] = options
        return self._write(settings.model_copy(update={field: mapping}))
