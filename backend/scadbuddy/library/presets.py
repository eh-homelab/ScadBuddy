"""Named parameter sets per template: the configurations a template is usually
printed with, so a print only needs the one value that differs this time.

Two kinds, listed together:

- **template** presets are defined by the template itself, in the ``presets`` list of
  its ``model.json`` (#326). They are part of the template -- a built-in's come from
  the image -- and are read-only here; a template of mine edits them through its
  metadata. A legacy ``presets.json`` (:data:`LEGACY_PRESETS_NAME`) beside the source
  is still read, below ``model.json``.
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
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, TypeAdapter, ValidationError, field_validator

from scadbuddy.core.files import write_atomic
from scadbuddy.core.paths import LEGACY_PRESETS_NAME, MODEL_META_NAME, DataPaths
from scadbuddy.library.slugs import MAX_SLUG_LENGTH, SLUG_PATTERN, InvalidSlugError, slugify
from scadbuddy.render.schema import ParamValue

logger = logging.getLogger(__name__)

#: A preset name is a label in a picker, not prose.
MAX_PRESET_NAME = 80
#: Per template. Far past what a picker is usable with; it bounds the file a
#: template's presets live in, which is rewritten whole on every save.
MAX_PRESETS = 200
#: A template preset's id is this plus its key (:func:`template_preset_keys`), so it can
#: never be taken for the id of a saved one (32 hex digits) and a write addressed to
#: it can be refused.
TEMPLATE_ID_PREFIX = "template-"
#: The key in ``model.json`` that holds a template's own presets.
PRESETS_KEY = "presets"

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


class ParamPresetDuplicate(BaseModel):
    """The copy's name; its values are the original's."""

    name: str = Field(min_length=1, max_length=MAX_PRESET_NAME)

    @field_validator("name")
    @classmethod
    def _name(cls, name: str) -> str:
        return _clean_name(name)


class _StoredPreset(_PresetBody):
    id: str
    created_at: datetime
    updated_at: datetime


class _StoredPresets(BaseModel):
    presets: list[_StoredPreset] = Field(default_factory=list)


class TemplatePreset(_PresetBody):
    """One preset a template defines in its ``model.json``.

    ``id`` is what keeps it the same preset when the list is reordered or it is
    renamed; without one, the key is derived from the name (:func:`template_preset_keys`).
    ``description`` and ``tags`` are carried for #327, which puts them in the API.
    """

    id: str | None = Field(default=None, pattern=SLUG_PATTERN, max_length=MAX_SLUG_LENGTH)
    # A factory, not `= ""`: a literal default makes the generated TypeScript type
    # require the field, which a client editing presets has no reason to send.
    description: str = Field(default_factory=str)
    tags: list[str] = Field(default_factory=list)


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
        return _checked(presets)


_PRESET_LIST: TypeAdapter[list[TemplatePreset]] = TypeAdapter(list[TemplatePreset])


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
    origin: PresetOrigin = Field(
        description="`template`: defined by the template in its model.json, read-only. "
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

    def _defined(self, model_id: str, name: str, raw: Any) -> list[TemplatePreset]:
        """``raw`` as a checked preset list, or none when it is not one (logged)."""
        try:
            return _checked(_PRESET_LIST.validate_python(raw))
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
        return [
            ParamPreset(
                id=f"{TEMPLATE_ID_PREFIX}{key}",
                name=preset.name,
                params=preset.params,
                origin="template",
            )
            for preset, key in zip(defined, template_preset_keys(defined), strict=True)
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
        write_atomic(path, (json.dumps(payload, indent=2) + "\n").encode())

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

    def find(self, model_id: str, preset_id: str) -> ParamPreset:
        """One preset of the template, shipped or saved."""
        for preset in self.presets(model_id):
            if preset.id == preset_id:
                return preset
        raise PresetNotFoundError(preset_id)

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
