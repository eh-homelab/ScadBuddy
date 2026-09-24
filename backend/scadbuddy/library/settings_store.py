from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from scadbuddy.bambuddy.models import PresetRef
from scadbuddy.core.settings import Settings

SETTINGS_NAME = "settings.json"
KEY_FILE_MODE = 0o600


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


class SettingsStore:
    """``data/settings.json``, mode 0600 because it holds the API key.

    The environment seeds the initial values; once the file exists it wins, so the
    UI can change what a deployment shipped with.
    """

    def __init__(self, path: Path, defaults: Settings) -> None:
        self.path = path
        self.defaults = defaults

    def _from_env(self) -> StoredSettings:
        return StoredSettings(
            bambuddy_url=self.defaults.bambuddy_url,
            bambuddy_api_key=self.defaults.bambuddy_api_key,
            public_url=self.defaults.public_url,
        )

    def load(self) -> StoredSettings:
        stored = self._from_env()
        if not self.path.is_file():
            return stored
        on_disk = StoredSettings.model_validate_json(self.path.read_text(encoding="utf-8"))
        merged = stored.model_dump()
        merged.update(on_disk.model_dump(exclude_none=True))
        return StoredSettings.model_validate(merged)

    def save(self, patch: SettingsPatch) -> StoredSettings:
        current = self.load().model_dump()
        # exclude_unset, not exclude_none: an omitted key leaves the stored value
        # alone, while an explicit null clears it. Without that an id could be set
        # but never unset.
        changes = patch.model_dump(mode="json", exclude_unset=True)
        if changes.get("bambuddy_api_key") == "":
            changes["bambuddy_api_key"] = None
        current.update(changes)
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
