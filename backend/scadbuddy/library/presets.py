"""Named parameter sets per template: the configurations a template is usually
printed with, so a print only needs the one value that differs this time.

Two kinds, listed together:

- **template** presets ship inside the template's directory as ``presets.json``
  (:data:`TEMPLATE_PRESETS_NAME`). They are part of the template -- a built-in's come
  from the image -- and are read-only here.
- **mine** are the ones saved through the API, one file per template under
  ``data/presets/`` (:meth:`DataPaths.model_presets`), for built-ins as much as for
  templates of mine.

A preset holds only the values it sets. Applying one starts from the template's
defaults, so a default the template changes later still reaches every preset that
never touched it.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from scadbuddy.core.paths import TEMPLATE_PRESETS_NAME, DataPaths
from scadbuddy.library.catalogue import _write_atomic
from scadbuddy.render.schema import ParamValue

logger = logging.getLogger(__name__)

#: A preset name is a label in a picker, not prose.
MAX_PRESET_NAME = 80
#: Per template. Far past what a picker is usable with; it bounds the file a
#: template's presets live in, which is rewritten whole on every save.
MAX_PRESETS = 200
#: A template preset's id is this plus its position, so it can never be taken for the
#: id of a saved one (32 hex digits) and a write addressed to it can be refused.
TEMPLATE_ID_PREFIX = "template-"

PresetOrigin = Literal["template", "mine"]


class PresetNotFoundError(KeyError):
    pass


class PresetExistsError(ValueError):
    """Another preset of the same template already has that name."""


class TooManyPresetsError(ValueError):
    pass


class InvalidPresetsFileError(ValueError):
    """The saved presets file on disk is not one this store wrote."""


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

    @field_validator("name")
    @classmethod
    def _name(cls, name: str) -> str:
        return _clean_name(name)


class ParamPresetCreate(_PresetBody):
    pass


class ParamPresetUpdate(BaseModel):
    """A rename, a new set of values, or both. ``params`` replaces the old ones whole."""

    name: str | None = Field(default=None, min_length=1, max_length=MAX_PRESET_NAME)
    params: dict[str, ParamValue] | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, name: str | None) -> str | None:
        return None if name is None else _clean_name(name)


class _StoredPreset(_PresetBody):
    id: str
    created_at: datetime
    updated_at: datetime


class _StoredPresets(BaseModel):
    presets: list[_StoredPreset] = Field(default_factory=list)


class _TemplatePresets(BaseModel):
    presets: list[_PresetBody] = Field(default_factory=list)


class ParamPreset(BaseModel):
    id: str
    name: str
    params: dict[str, ParamValue]
    origin: PresetOrigin = Field(
        description="`template`: shipped in the template's presets.json, read-only. "
        "`mine`: saved here, editable -- on built-ins too."
    )
    updated_at: datetime | None = None


def _same_name(a: str, b: str) -> bool:
    return a.casefold() == b.casefold()


class PresetStore:
    """Reads a template's presets and writes the saved ones.

    One lock for the store: every write is a read-modify-write of one small file, and
    this process is the only writer.
    """

    def __init__(self, paths: DataPaths) -> None:
        self.paths = paths
        self._lock = threading.Lock()

    def template_presets(self, model_id: str) -> list[ParamPreset]:
        """The template's own, in the order its ``presets.json`` lists them.

        A file that cannot be read is logged and treated as no presets: it costs the
        picker its template presets, never the page.
        """
        path = self.paths.model_dir(model_id) / TEMPLATE_PRESETS_NAME
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except (OSError, UnicodeDecodeError):
            logger.exception("could not read template presets", extra={"slug": model_id})
            return []
        try:
            loaded = _TemplatePresets.model_validate_json(raw)
        except (ValidationError, RecursionError) as error:
            logger.warning("ignored a template's presets.json: %s", error, extra={"slug": model_id})
            return []
        return [
            ParamPreset(
                id=f"{TEMPLATE_ID_PREFIX}{index}",
                name=preset.name,
                params=preset.params,
                origin="template",
            )
            for index, preset in enumerate(loaded.presets)
        ]

    def _read(self, model_id: str) -> _StoredPresets:
        path = self.paths.model_presets(model_id)
        try:
            return _StoredPresets.model_validate_json(path.read_bytes())
        except FileNotFoundError:
            return _StoredPresets()
        except (ValidationError, RecursionError) as error:
            # Left where it is, not overwritten: the next save would otherwise
            # replace every preset in it with the one being saved.
            raise InvalidPresetsFileError(
                f"presets/{path.name} is not a valid presets file: {error}"
            ) from None

    def _write(self, model_id: str, stored: _StoredPresets) -> None:
        path = self.paths.model_presets(model_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not stored.presets:
            path.unlink(missing_ok=True)
            return
        payload: dict[str, Any] = stored.model_dump(mode="json")
        _write_atomic(path, (json.dumps(payload, indent=2) + "\n").encode())

    @staticmethod
    def _view(preset: _StoredPreset) -> ParamPreset:
        return ParamPreset(
            id=preset.id,
            name=preset.name,
            params=preset.params,
            origin="mine",
            updated_at=preset.updated_at,
        )

    def saved_presets(self, model_id: str) -> list[ParamPreset]:
        return [self._view(preset) for preset in self._read(model_id).presets]

    def presets(self, model_id: str) -> list[ParamPreset]:
        """The template's presets, then the saved ones, each in their own order."""
        return self.template_presets(model_id) + self.saved_presets(model_id)

    def _require_free(self, model_id: str, stored: _StoredPresets, name: str, own: str) -> None:
        """A name is one preset's in the picker: none of the template's, nor another saved one."""
        taken = [p.name for p in stored.presets if p.id != own]
        taken += [p.name for p in self.template_presets(model_id)]
        if any(_same_name(name, other) for other in taken):
            raise PresetExistsError(name)

    def create(self, model_id: str, body: ParamPresetCreate) -> ParamPreset:
        now = datetime.now(UTC)
        with self._lock:
            stored = self._read(model_id)
            if len(stored.presets) >= MAX_PRESETS:
                raise TooManyPresetsError(f"a template keeps at most {MAX_PRESETS} presets")
            self._require_free(model_id, stored, body.name, own="")
            preset = _StoredPreset(
                id=uuid.uuid4().hex,
                name=body.name,
                params=body.params,
                created_at=now,
                updated_at=now,
            )
            stored.presets.append(preset)
            self._write(model_id, stored)
        return self._view(preset)

    def update(self, model_id: str, preset_id: str, patch: ParamPresetUpdate) -> ParamPreset:
        with self._lock:
            stored = self._read(model_id)
            preset = next((p for p in stored.presets if p.id == preset_id), None)
            if preset is None:
                raise PresetNotFoundError(preset_id)
            if patch.name is not None:
                self._require_free(model_id, stored, patch.name, own=preset_id)
                preset.name = patch.name
            if patch.params is not None:
                preset.params = patch.params
            preset.updated_at = datetime.now(UTC)
            self._write(model_id, stored)
        return self._view(preset)

    def delete(self, model_id: str, preset_id: str) -> None:
        with self._lock:
            stored = self._read(model_id)
            kept = [p for p in stored.presets if p.id != preset_id]
            if len(kept) == len(stored.presets):
                raise PresetNotFoundError(preset_id)
            self._write(model_id, _StoredPresets(presets=kept))

    def copy(self, source_id: str, target_id: str) -> None:
        """Give a duplicate the presets saved on the template it was copied from.

        The duplicate's own template presets came with its directory; these are the
        ones kept beside it. Fresh ids, so the two sets are edited independently.
        """
        with self._lock:
            source = self._read(source_id)
            if not source.presets:
                return
            copies = [
                preset.model_copy(update={"id": uuid.uuid4().hex}) for preset in source.presets
            ]
            self._write(target_id, _StoredPresets(presets=copies))
