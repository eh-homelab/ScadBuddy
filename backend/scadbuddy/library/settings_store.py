from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from scadbuddy.core.settings import Settings

SETTINGS_NAME = "settings.json"
KEY_FILE_MODE = 0o600


class StoredSettings(BaseModel):
    """Bambuddy connection details. The API key never leaves the server."""

    bambuddy_url: str | None = None
    bambuddy_api_key: str | None = None
    library_folder_id: str | None = None
    pipeline_id: str | None = None
    printer_id: str | None = None
    public_url: str | None = None


class SettingsPatch(BaseModel):
    bambuddy_url: str | None = None
    bambuddy_api_key: str | None = None
    library_folder_id: str | None = None
    pipeline_id: str | None = None
    printer_id: str | None = None
    public_url: str | None = None


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
        # An omitted key leaves it alone; an empty string clears it.
        changes = patch.model_dump(exclude_none=True)
        if changes.get("bambuddy_api_key") == "":
            changes["bambuddy_api_key"] = None
        current.update(changes)
        settings = StoredSettings.model_validate(current)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(settings.model_dump(), indent=2) + "\n", encoding="utf-8")
        self.path.chmod(KEY_FILE_MODE)
        return settings
