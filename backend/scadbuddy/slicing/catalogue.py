"""The preset catalogue a native run offers: the image's own BBL profiles (#3818 tier A
Task 2, spec 2026-10-16 §5.3), in the shape Bambuddy's catalogue has, so the resolver
and the dialog read either one the same way and a native run asks Bambuddy nothing.

What it holds, measured 2026-10-18 on the v02.08.02.61 tree (``/opt/slicer-profiles``
copied out of the Dockerfile's ``slicer-dist`` stage, every file of each kind read):

- Every preset ``BBL.json`` lists whose ``instantiation`` is not ``"false"``: 56 of 148
  machine, 233 of 278 process and 1928 of 2050 filament presets (none has an empty
  ``instantiation``). Bambu Studio loads an ``instantiation: "false"`` preset only as a
  parent: ``PresetBundle::load_vendor_configs_from_json``'s ``parse_subfile`` keeps it in
  ``config_maps`` and returns before it becomes a preset
  (https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/libslic3r/PresetBundle.cpp#L5159-L5170).
  A preset :func:`~scadbuddy.slicing.profiles.flatten` refuses (a missing parent, a
  loop, ``includes``, a short per-filament list) is not offered either, since the native
  slicer would refuse it; none of the tree's is.
- Each row's fields come from the preset flattened: ``compatible_printers`` (every
  instantiable filament and process preset has one; no machine preset does),
  ``filament_type`` and ``default_filament_colour`` (the first value of each; no BBL
  filament sets a colour, so ``filament_colour`` is ``None``, as Bambuddy's rows have it
  in ``tests/bambuddy/recordings/slicer-presets-h2c.json``) and ``filament_id``, which
  every instantiable filament inherits.
- ``ref`` is ``PresetRef(source="standard", id=<name>)``, which is what Bambuddy's
  ``standard`` ids are (``slicing/profiles.py``'s docstring), so a remembered standard ref
  resolves unchanged. A ``cloud`` ref is the preset's ``setting_id``
  (:func:`native_ref`); no ``setting_id`` and no name is shared between two kinds.
- An AMS tray's ``tray_info_idx`` is the BBL filament's ``filament_id``: of the indexes in
  ``tests/bambuddy/recordings/printer-status*.json``, ``GFA00``, ``GFA05``, ``GFG00``,
  ``GFL99`` and ``GFS05`` are each the ``filament_id`` of 24 to 42 presets (one per printer
  and nozzle), exactly one of them compatible with ``Bambu Lab H2C 0.4 nozzle``
  (``GFA00``: ``Bambu PLA Basic @BBL H2C``). ``OFDSrzZ8`` is in none: a user's own
  preset, which a native run cannot slice (D5).
- Building it reads each listed file once and flattens every instantiable preset: 4.7 s
  on a loaded WSL2 machine (2.9 s of it the filaments' flattening), so it is built in a
  thread and kept for the life of the process per tree and version. The tree is fixed in
  an image; nothing is written to disk.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

from scadbuddy.bambuddy.catalogue import Catalogue, PresetChoice, _catalogue
from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.models import PresetRef
from scadbuddy.slicing.profiles import (
    Flat,
    Kind,
    ProfileError,
    ProfileImageError,
    Profiles,
    flatten,
)

logger = logging.getLogger(__name__)

#: The catalogue's field for each of the tree's kinds.
_FIELDS: dict[Kind, str] = {"machine": "printer", "process": "process", "filament": "filament"}

#: Built catalogues, by the tree's root and version.
_built: dict[tuple[Path, str], Catalogue] = {}
#: A build that failed, by the same key: when, and the error, answered again until
#: :data:`FAILED_RETRY` has passed, so a broken tree is not rebuilt on every open.
_failed: dict[tuple[Path, str], tuple[float, ProfileImageError]] = {}
_lock = threading.Lock()

#: How long a failed build is answered from memory before it is tried again.
FAILED_RETRY = 60.0
#: The clock :data:`FAILED_RETRY` is measured on; the catalogue's own, so a test can
#: move it without moving every other clock in the process.
_now = time.monotonic


def native_catalogue(profiles: Profiles) -> Catalogue:
    """Every preset of the image's tree a native slice can read, as :class:`Catalogue`
    rows (the module's docstring). File I/O and seconds of work on its first call per tree
    and version: an async caller runs it in a thread (:func:`catalogue_for`), and the app
    and the print worker start it at start (:func:`warm_catalogue`). A tree that cannot be
    read raises :class:`ProfileImageError`, the same one for :data:`FAILED_RETRY`."""
    key = (profiles.root, profiles.version)
    with _lock:
        built = _built.get(key)
        if built is not None:
            return built
        failed = _failed.get(key)
        if failed is not None and _now() - failed[0] < FAILED_RETRY:
            raise ProfileImageError(str(failed[1]))
        try:
            built = _built[key] = _build(profiles)
        except ProfileImageError as error:
            _failed[key] = (_now(), error)
            raise
        _failed.pop(key, None)
    return built


def warm_catalogue(
    profiles: Profiles | None, native: Callable[[], bool]
) -> asyncio.Task[None] | None:
    """Build the image's catalogue in the background, so the first native dialog does
    not wait the seconds it takes; ``None`` without a tree. Only when ``native`` (a
    blocking read of ``slicer_backend``, run in a thread) says the slices are native:
    otherwise nothing reads it, and the build would hold the GIL for seconds while the
    first requests are served (#4348). Never raises: a failure is logged once, by type,
    and the first open that needs the catalogue raises it again. The caller keeps the
    task and cancels it at shutdown, which stops waiting on the build, not the build's
    thread (it finishes on its own)."""
    if profiles is None:
        return None
    return asyncio.create_task(_warm(profiles, native), name="native-catalogue-warm")


async def _warm(profiles: Profiles, native: Callable[[], bool]) -> None:
    try:
        if not await asyncio.to_thread(native):
            return
        await asyncio.to_thread(native_catalogue, profiles)
    except Exception as error:
        # By type only: the message names files under the image's tree.
        logger.warning(
            "could not build the native preset catalogue", extra={"error": type(error).__name__}
        )


def _build(profiles: Profiles) -> Catalogue:
    rows: dict[Kind, list[PresetChoice]] = {}
    setting_ids: dict[str, dict[str, str]] = {}
    for kind, field in _FIELDS.items():
        docs = profiles.documents(kind)
        rows[kind] = []
        setting_ids[field] = {}
        refused = 0
        for name, doc in docs.items():
            if doc.get("instantiation") == "false":
                continue
            try:
                flat = flatten(name, docs.get)
            except ProfileError:
                refused += 1
                continue
            rows[kind].append(_row(name, flat))
            setting_id = flat.get("setting_id")
            if isinstance(setting_id, str) and setting_id:
                setting_ids[field][setting_id] = name
        if refused:
            logger.warning(
                "presets the native slicer cannot read are not offered",
                extra={"kind": kind, "count": refused},
            )
    return Catalogue(
        printer=rows["machine"],
        process=rows["process"],
        filament=rows["filament"],
        native_setting_ids=setting_ids,
    )


def _first(value: str | list[str] | None) -> str | None:
    if isinstance(value, list):
        value = value[0] if value else None
    return value or None


def _row(name: str, flat: Flat) -> PresetChoice:
    printers = flat.get("compatible_printers")
    return PresetChoice(
        ref=PresetRef(source="standard", id=name),
        name=name,
        filament_type=_first(flat.get("filament_type")),
        filament_colour=_first(flat.get("default_filament_colour")),
        compatible_printers=list(printers) if isinstance(printers, list) else [],
        filament_id=_first(flat.get("filament_id")),
    )


def native_ref(profiles: Profiles, ref: PresetRef) -> PresetRef | None:
    """:meth:`Catalogue.native_ref` on the image's catalogue. It builds the catalogue on
    a first call (seconds of file I/O), so never call it on the event loop: an async
    caller holds the catalogue from :func:`catalogue_for` and asks it."""
    return native_catalogue(profiles).native_ref(ref)


async def catalogue_for(client: BambuddyClient | None, *, native: Profiles | None) -> Catalogue:
    """The catalogue a run resolves against: the image's BBL profiles when ``native`` is
    given (no Bambuddy call, #3818 tier A), else Bambuddy's, read fresh as
    :func:`~scadbuddy.bambuddy.catalogue.read_catalogue` reads it."""
    if native is not None:
        return await asyncio.to_thread(native_catalogue, native)
    if client is None:
        raise ValueError("the preset catalogue needs a Bambuddy client or the native profiles")
    return await _catalogue(client)
