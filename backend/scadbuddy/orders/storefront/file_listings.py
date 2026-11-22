"""A storefront listing of a library file (#6710, spec 2026-11-21 §4-§5, §9.3): its publish,
its uploaded pictures and its plate pictures.

Publish keeps a copy of the file in the content store, ``listing-file-<sha256>``, held under
(`KEPT_HOLDER`, listing id), and that copy is the version the listing sells: a later change
of the library file changes nothing (§5.3). The pin (`ListingFile.pinned`) names it. Only a
first publish, a new file picked, or a ``repin`` request reads the library (§5.1 rows 1 to
4); any other publish reads the held copy. From the copy's bytes publish reads the file's
slots (the dialog's own reader), one held GLB of each plate with each part's slot
(`glb.SLOT_EXTRA`), and the pictures ``media_ids`` names: a plate's own picture
(``plate:<n>``) or one the shop uploaded to the listing (``upload:<sha256>``, §5.4).

Everything here runs in the CRUD service, which already reads library files for the back
office: no new database client or grant, and the 3MF is read as data (#5295).
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import tempfile
import zipfile
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import aclosing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, cast
from xml.etree import ElementTree

import trimesh
from fastapi import UploadFile, status

from scadbuddy.bambuddy.filaments3mf import UnreadableFilamentsError, read_filaments
from scadbuddy.bambuddy.models import LibraryFile
from scadbuddy.bambuddy.options import library_options_scope
from scadbuddy.bambuddy.print_source import MAX_DOWNLOAD_BYTES, ReadOutOfMemoryError, is_sliced
from scadbuddy.core.problems import ApiError
from scadbuddy.library.media import MAX_IMAGE_BYTES, SNIFF_BYTES, sniff_kind
from scadbuddy.orders.quote import file_colour
from scadbuddy.orders.storefront.inputs import effective_materials, material_key
from scadbuddy.orders.storefront.listings import (
    HOLDER,
    KEPT_HOLDER,
    ListingStore,
    check_materials,
    check_sliceable,
    file_copy_key,
    listing_not_found,
    picture_key,
    problem,
)
from scadbuddy.orders.storefront.wire import (
    FILE_PICTURE_ID,
    FileSlot,
    FileSnapshot,
    Listing,
    ListingMedia,
    is_support,
)
from scadbuddy.print_library.component import PRINT_LIBRARY
from scadbuddy.print_library.ids import is_native_id
from scadbuddy.render.bambu3mf import PartSettingsUnwritableError, part_settings_slots, plates_of
from scadbuddy.render.geometry import NoSuchPlateError
from scadbuddy.render.glb import write_glb
from scadbuddy.render.objects3mf import UnreadableObjectsError, read_plate_parts
from scadbuddy.render.paint import PaintCodeError
from scadbuddy.render.split import ColourPart, painted_pieces
from scadbuddy.store.content_models import BlobScope
from scadbuddy.store.refs import BlobRefs

if TYPE_CHECKING:
    from scadbuddy.api.deps import AppState
    from scadbuddy.orders.bambuddy import ServedLibrary
    from scadbuddy.store.content import ContentStore

logger = logging.getLogger(__name__)

#: A slot whose file names no colour (an STL, or a 3MF that names none), as the back
#: office's quote greys it (`quote.py` `library_colours`).
GREY: Final = "#808080"
#: The types a file listing sells (§5.1 row 2): never a sliced file, fixed for one
#: printer and one set of colours.
LISTED_TYPES: Final = frozenset({"3mf", "stl"})
#: The key prefix of a file listing's held plate preview.
PREVIEW_PREFIX: Final = "listing-preview-"


def _unprocessable(slug: str, detail: str, name: str, **extensions: Any) -> ApiError:
    return problem(status.HTTP_422_UNPROCESSABLE_CONTENT, slug, detail, name=name, **extensions)


def _unknown() -> ApiError:
    return _unprocessable(
        "listing_file_unknown",
        "the file is not in the library that serves; pick another file",
        "library_file",
    )


def _too_large() -> ApiError:
    return _unprocessable(
        "listing_file_too_large",
        f"the file is larger than {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB, which a print"
        " cannot read",
        "library_file",
    )


@dataclass(frozen=True)
class HeldBytes:
    """The listed file's bytes, as the held copy keeps them, and what names it."""

    filename: str
    file_type: Literal["3mf", "stl"]
    sha256: str
    payload: bytes


# -- the library's file (§5.1 rows 1 to 4) ------------------------------------------------


def _served(state: AppState) -> ServedLibrary:
    # Here, not at the top: the orders' Bambuddy module imports the storefront's.
    from scadbuddy.orders.bambuddy import ServedLibrary

    return ServedLibrary(state.settings_store, state.components.get(PRINT_LIBRARY).serving_native)


async def served_file(state: AppState, file_id: int) -> LibraryFile | None:
    """The file in the library that serves, or None when it is not there."""
    return await _served(state).file(file_id)


async def download(state: AppState, file_id: int) -> AsyncGenerator[bytes]:
    """The file's bytes, from the library that serves (`ServedLibrary.download`: only the
    orders' Bambuddy module reaches the client, #4253)."""
    async with aclosing(_served(state).download(file_id)) as chunks:
        async for chunk in chunks:
            yield chunk


def _type_of(file: LibraryFile) -> Literal["3mf", "stl"]:
    kind = (file.file_type or "").lower()
    if kind not in LISTED_TYPES:
        reason = "sliced" if is_sliced(kind) else "type"
        raise _unprocessable(
            "listing_file_unprintable",
            f"{file.filename} is a sliced file, fixed for one printer and one set of colours"
            if reason == "sliced"
            else f"{file.filename} is not a 3MF or an STL",
            "library_file",
            reason=reason,
        )
    return cast(Literal["3mf", "stl"], kind)


async def _from_library(state: AppState, listing: Listing, refs: BlobRefs) -> HeldBytes:
    """The library's file read now, checked (§5.1 rows 1 to 4), and kept: its copy put in
    the content store and held under (`KEPT_HOLDER`, listing id), its hold refreshed."""
    assert listing.library_file is not None
    asked = listing.library_file
    file = await served_file(state, asked.id)
    if file is None or ("native" if is_native_id(file.id) else "bambuddy") != asked.library:
        raise _unknown()
    file_type = _type_of(file)
    if file.file_size is not None and file.file_size > MAX_DOWNLOAD_BYTES:
        raise _too_large()
    digest = hashlib.sha256()
    data = bytearray()
    async with aclosing(download(state, asked.id)) as chunks:
        async for chunk in chunks:
            data += chunk
            if len(data) > MAX_DOWNLOAD_BYTES:
                raise _too_large()
            digest.update(chunk)
    sha256 = digest.hexdigest()
    if file.file_hash is not None and file.file_hash.lower() != sha256:
        raise problem(
            status.HTTP_409_CONFLICT,
            "listing_file_changed",
            f"{file.filename} changed while it was read; publish again",
            name="library_file",
        )
    content = _content(state)
    key = file_copy_key(sha256)
    await content.put(
        "media", bytes(data), name=file.filename, scope=BlobScope(title=listing.title), key=key
    )
    await asyncio.to_thread(refs.hold, key, KEPT_HOLDER, listing.id)
    return HeldBytes(file.filename, file_type, sha256, bytes(data))


def _held_by(refs: BlobRefs, key: str, listing_id: str) -> bool:
    with refs.pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM blob_refs WHERE key = %s AND holder_kind = %s AND holder_id = %s",
            (key, KEPT_HOLDER, listing_id),
        ).fetchone()
    return row is not None


async def read_held(content: ContentStore, key: str) -> bytes | None:
    """A held blob's bytes, or None when the store lost it."""
    stat = await content.stat(key)
    if stat is None:
        return None
    data = bytearray()
    async with aclosing(content.get(stat.ref)) as chunks:
        async for chunk in chunks:
            data += chunk
    return bytes(data)


async def _from_copy(state: AppState, listing: Listing, refs: BlobRefs) -> HeldBytes:
    """The pinned version, read from its held copy: the library is not read (§5.1). A copy
    the listing no longer holds is 422 ``listing_file_unknown``."""
    asked = listing.library_file
    assert asked is not None and asked.pinned is not None
    key = file_copy_key(asked.pinned)
    payload = None
    if await asyncio.to_thread(_held_by, refs, key, listing.id):
        payload = await read_held(_content(state), key)
    if payload is None:
        raise _unknown()
    filename = asked.filename or f"library file {asked.id}"
    file_type: Literal["3mf", "stl"] = "stl" if filename.lower().endswith(".stl") else "3mf"
    return HeldBytes(filename, file_type, asked.pinned, payload)


def _content(state: AppState) -> ContentStore:
    content = state.store.content
    assert content is not None
    return content


# -- what the bytes say (§5.2 steps 2 and 3) ------------------------------------------------


def read_slots(payload: bytes, file_type: str) -> list[FileSlot]:
    """The file's slots, from 0 (§5.2 step 2): each declared slot, its colour and its
    type, read as the dialog reads a file (`read_filaments`), ``used`` when a printed part
    names it (`part_settings_slots`, whose ids are from 1; none known, every slot is used).
    An STL has one slot, used and grey. Blocking: call it in a thread."""
    if file_type == "stl":
        return [FileSlot(slot=0, colour=GREY, material=None, used=True, support=False)]
    try:
        local = read_filaments(io.BytesIO(payload))
    except UnreadableFilamentsError as error:
        raise _unprocessable(
            "listing_file_unprintable",
            f"the file's filaments cannot be read: {error}",
            "library_file",
            reason="unreadable",
        ) from None
    try:
        printed = part_settings_slots(payload)
    except PartSettingsUnwritableError:
        printed = None
    slots: list[FileSlot] = []
    for need in sorted(local.project, key=lambda n: n.slot_id):
        if need.slot_id < 1:
            continue
        material = need.type or None
        slots.append(
            FileSlot(
                slot=need.slot_id - 1,
                colour=file_colour(need.color) or GREY,
                material=material,
                used=not printed or need.slot_id in printed,
                support=material is not None and is_support(material),
            )
        )
    return slots or [FileSlot(slot=0, colour=GREY, material=None, used=True, support=False)]


def read_plates(payload: bytes, file_type: str) -> list[int]:
    """The file's plates, in order; one for an STL, or a 3MF that lists none."""
    if file_type == "stl":
        return [1]
    try:
        found = [plate.index for plate in plates_of(io.BytesIO(payload))]
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError, ValueError):
        found = []
    return found or [1]


def check_offered(slots: Sequence[FileSlot]) -> None:
    """422 ``listing_file_no_parts`` when no slot is left to offer (§5.1 row 5)."""
    if not any(slot.used and not slot.support for slot in slots):
        raise _unprocessable(
            "listing_file_no_parts",
            "no part of the file prints in a slot a customer can colour",
            "library_file",
        )


def _plate_parts(payload: bytes, file_type: str, plate: int) -> list[ColourPart]:
    if file_type == "stl":
        mesh = trimesh.load(io.BytesIO(payload), file_type="stl", force="mesh")
        if not isinstance(mesh, trimesh.Trimesh) or mesh.is_empty:
            raise UnreadableObjectsError("the STL holds no mesh")
        return [ColourPart(1, "Part 1", GREY, mesh, slot=0)]
    return read_plate_parts(payload, plate).parts


def plate_preview(payload: bytes, file_type: str, plate: int, slots: Sequence[FileSlot]) -> bytes:
    """Plate ``plate`` as a GLB (§5.2 step 3, §8.2), each mesh with the slot it prints in
    (`glb.SLOT_EXTRA`). The reader joins a plate's parts by colour, so a mesh takes the
    first slot of its colour; a colour no slot names has no slot and keeps its colour.
    Blocking: call it in a thread."""
    by_colour: dict[str, int] = {}
    for slot in slots:
        by_colour.setdefault(slot.colour.casefold(), slot.slot)
    parts = [
        replace(part, slot=by_colour.get(part.colour[:7].casefold(), part.slot))
        for part in painted_pieces(_plate_parts(payload, file_type, plate))
    ]
    with tempfile.TemporaryDirectory() as scratch:
        out = Path(scratch) / "preview.glb"
        write_glb(parts, out)
        return out.read_bytes()


async def _previews(
    state: AppState,
    refs: BlobRefs,
    listing: Listing,
    held: HeldBytes,
    plates: Sequence[int],
    slots: Sequence[FileSlot],
) -> list[str]:
    """One held GLB per plate; none for a file no mesh can be read from (logged)."""
    content = _content(state)
    keys: list[str] = []
    for plate in plates:
        try:
            glb = await asyncio.to_thread(plate_preview, held.payload, held.file_type, plate, slots)
        except (
            UnreadableObjectsError,
            NoSuchPlateError,
            PaintCodeError,
            ValueError,
            MemoryError,
            ReadOutOfMemoryError,
        ) as error:
            logger.info(
                "a file listing has no preview: %s",
                error,
                extra={"listing_id": listing.id, "sha256": held.sha256},
            )
            return []
        key = f"{PREVIEW_PREFIX}{hashlib.sha256(glb).hexdigest()}"
        await content.put(
            "media", glb, name=f"plate-{plate}.glb", scope=BlobScope(title=listing.title), key=key
        )
        await asyncio.to_thread(refs.add, key, HOLDER, listing.id)
        keys.append(key)
    return keys


# -- the pictures (§5.2 step 4, §5.4) ----------------------------------------------------------


def plate_image(payload: bytes, plate: int) -> bytes | None:
    """``Metadata/plate_<plate>.png`` of a 3MF, as a sliced file's is read (`quote.py`
    `plate_image`), or None. Blocking: call it in a thread."""
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            info = archive.getinfo(f"Metadata/plate_{plate}.png")
            if info.file_size > MAX_IMAGE_BYTES:
                return None
            return archive.read(info)
    except (KeyError, zipfile.BadZipFile):
        return None


def _media_unknown(media_id: str) -> ApiError:
    return _unprocessable(
        "listing_media_unknown",
        f"{media_id!r} names no plate picture of the file and no picture of the listing",
        media_id,
    )


@dataclass(frozen=True)
class _Picture:
    media_id: str
    key: str
    media_type: str
    #: A plate picture's bytes, put at ``key`` when publish copies it; None for an upload,
    #: which is there already.
    data: bytes | None


async def _checked_pictures(
    state: AppState, refs: BlobRefs, listing: Listing, held: HeldBytes, plates: Sequence[int]
) -> list[_Picture]:
    """Each id of ``media_ids`` as the picture it names, or 422 ``listing_media_unknown``
    (with the id as ``name``) for one that names no plate picture of the file and no
    picture held for the listing (§5.1)."""
    content = _content(state)
    found: list[_Picture] = []
    for media_id in listing.media_ids:
        match = FILE_PICTURE_ID.fullmatch(media_id)
        if match is None:
            raise _media_unknown(media_id)
        if match.group(1) is not None:
            plate = int(match.group(1))
            image = (
                None
                if plate not in plates or held.file_type != "3mf"
                else await asyncio.to_thread(plate_image, held.payload, plate)
            )
            if image is None:
                raise _media_unknown(media_id)
            key = picture_key(hashlib.sha256(image).hexdigest())
            found.append(_Picture(media_id, key, "image/png", image))
            continue
        key = picture_key(match.group(2))
        stat = (
            await content.stat(key)
            if await asyncio.to_thread(_held_by, refs, key, listing.id)
            else None
        )
        if stat is None:
            raise _media_unknown(media_id)
        found.append(_Picture(media_id, key, str(stat.meta.get("media_type", "image/png")), None))
    return found


async def _copy_pictures(
    state: AppState, refs: BlobRefs, listing: Listing, pictures: Sequence[_Picture]
) -> list[ListingMedia]:
    """Each picture held under (`HOLDER`, listing id), a plate picture put in the content
    store first, as `ListingMedia` of kind ``image`` with its id as ``media_id``."""
    content = _content(state)
    media: list[ListingMedia] = []
    for picture in pictures:
        if picture.data is not None:
            await content.put(
                "media",
                picture.data,
                name=f"{picture.media_id.replace(':', '-')}.png",
                scope=BlobScope(title=listing.title),
                key=picture.key,
                meta={"media_type": picture.media_type},
            )
        await asyncio.to_thread(refs.add, picture.key, HOLDER, listing.id)
        media.append(
            ListingMedia(
                blob_key=picture.key,
                kind="image",
                media_type=picture.media_type,
                caption="",
                media_id=picture.media_id,
            )
        )
    return media


async def upload_picture(
    state: AppState, store: ListingStore, listing_id: str, upload: UploadFile
) -> str:
    """A picture the shop uploads to a file listing (§5.4), checked as a template's media
    upload is (an image by its bytes, at most `MAX_IMAGE_BYTES`; 415 and 413 otherwise),
    put at ``listing-media-<sha256>`` and held under (`KEPT_HOLDER`, listing id) with a
    fresh ``created_at`` (`BlobRefs.hold`), so a save within the hour spares it. Answers
    its id, ``upload:<sha256>``. 404 ``listing_not_found``; 422 ``listing_source_invalid``
    for a template listing, whose pictures are its template's media."""
    listing = await asyncio.to_thread(store.get, listing_id)
    if listing is None:
        raise listing_not_found(listing_id)
    if listing.source != "library_file":
        raise _unprocessable(
            "listing_source_invalid",
            "a template listing's pictures are its template's media; upload them there",
            "source",
        )

    def read() -> bytes:
        upload.file.seek(0)
        data = upload.file.read(MAX_IMAGE_BYTES + 1)
        sniffed = sniff_kind(data[:SNIFF_BYTES])
        if sniffed is None or sniffed[0] != "image":
            raise ApiError(
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "a picture is a PNG, JPEG or WebP image"
            )
        if len(data) > MAX_IMAGE_BYTES:
            raise ApiError(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"a picture is at most {MAX_IMAGE_BYTES // (1024 * 1024)} MB",
            )
        return data

    data = await asyncio.to_thread(read)
    sniffed = sniff_kind(data[:SNIFF_BYTES])
    assert sniffed is not None
    sha256 = hashlib.sha256(data).hexdigest()
    key = picture_key(sha256)
    await _content(state).put(
        "media",
        data,
        name=f"upload.{sniffed[1]}",
        scope=BlobScope(title=listing.title),
        key=key,
        meta={"media_type": sniffed[2]},
    )
    await asyncio.to_thread(BlobRefs(state.projection.pool).hold, key, KEPT_HOLDER, listing.id)
    return f"upload:{sha256}"


async def pinned_plate_image(
    state: AppState, store: ListingStore, listing_id: str, plate: int
) -> bytes | None:
    """Plate ``plate``'s picture in the held copy a file listing pins (§9.3), or None: no
    such listing, no pin, a copy gone or a plate with no picture. The library is not read,
    so a file gone from it still shows its pictures."""
    listing = await asyncio.to_thread(store.get, listing_id)
    pinned = (
        None if listing is None or listing.library_file is None else listing.library_file.pinned
    )
    if pinned is None:
        return None
    payload = await read_held(_content(state), file_copy_key(pinned))
    return None if payload is None else await asyncio.to_thread(plate_image, payload, plate)


# -- the checks of the listing's colours (§5.1 rows 6 to 9) ------------------------------------


def support_locks(
    slots: Sequence[FileSlot], colours: Sequence[str], materials: Mapping[str, str]
) -> dict[int, str]:
    """Each used support slot's filament: the first of the listing's support filaments of
    the slot's material, else its first support filament. 422 ``listing_file_support_unset``
    (with ``slots``) when the listing has none (§5.1 row 8)."""
    supports = [f for f in colours if f in materials and is_support(materials[f])]
    wanted = [slot for slot in slots if slot.used and slot.support]
    if wanted and not supports:
        raise _unprocessable(
            "listing_file_support_unset",
            "the file prints support in a slot; add a support filament to the colours on offer",
            "colours",
            slots=[slot.slot for slot in wanted],
        )
    locks: dict[int, str] = {}
    for slot in wanted:
        same = [
            f
            for f in supports
            if slot.material is not None
            and material_key(materials[f]) == material_key(slot.material)
        ]
        locks[slot.slot] = (same or supports)[0]
    return locks


def check_printable(listing: Listing, materials: Mapping[str, str], allowed: Sequence[str]) -> None:
    """422 ``listing_file_slots_unprintable`` when no allowed material offers a colour for
    the offered slots (§5.1 row 7). Each offered slot takes any colour of the print's one
    material, so a material is printable when the listing offers a filament of it."""
    wanted = {material_key(m) for m in allowed}
    if not any(
        f in materials and not is_support(materials[f]) and material_key(materials[f]) in wanted
        for f in listing.colours
    ):
        raise _unprocessable(
            "listing_file_slots_unprintable",
            "no allowed material offers a colour for the file's parts; add a colour in an"
            " allowed material",
            "colours",
        )


# -- publish ----------------------------------------------------------------------------------


async def publish_file(state: AppState, store: ListingStore, listing: Listing) -> Listing:
    """A file listing checked and published (§5): the checks of §5.1 in order, the first
    fault winning, then the copy, the slots, the previews, the pictures and the snapshot.
    The pin is written with the snapshot (`ListingStore.mark_published`). A failure after
    the first hold drops what the row does not name (`ListingStore.release_unheld`)."""
    asked = listing.library_file
    assert asked is not None
    refs = BlobRefs(state.projection.pool)
    try:
        if asked.pinned is None or asked.repin:
            held = await _from_library(state, listing, refs)
        else:
            held = await _from_copy(state, listing, refs)
        slots = await asyncio.to_thread(read_slots, held.payload, held.file_type)
        check_offered(slots)
        plates = await asyncio.to_thread(read_plates, held.payload, held.file_type)
        pictures = await _checked_pictures(state, refs, listing, held, plates)
        colours = await asyncio.to_thread(store.filament_colours)
        for filament_id in listing.colours:
            if filament_id not in colours:
                raise _unprocessable(
                    "listing_colour_unknown",
                    f"{filament_id!r} is not a filament of the price book",
                    "colours",
                )
        materials = await asyncio.to_thread(store.filament_materials)
        shop = (await asyncio.to_thread(state.settings_store.snapshot)).runtime
        allowed = effective_materials(listing, shop.storefront_materials)
        check_materials(listing, materials, allowed)
        check_printable(listing, materials, allowed)
        support = support_locks(slots, listing.colours, materials)
        settings = await asyncio.to_thread(state.settings_store.load)
        check_sliceable(settings.storefront_slice_profile, listing, materials)
        previews = await _previews(state, refs, listing, held, plates, slots)
        media = await _copy_pictures(state, refs, listing, pictures)
        remembered = settings.model_print_choices.get(library_options_scope(asked.id))
        snapshot = FileSnapshot(
            file_id=asked.id,
            library=asked.library,
            filename=held.filename,
            file_type=held.file_type,
            sha256=held.sha256,
            size=len(held.payload),
            copy_key=file_copy_key(held.sha256),
            slots=slots,
            plates=list(plates),
            preview_keys=previews,
            support=support,
            remembered=None if remembered is None else remembered.model_dump(mode="json"),
        )
        return await asyncio.to_thread(
            store.mark_published,
            listing.id,
            snapshot=snapshot,
            media=media,
            expected_updated_at=listing.updated_at,
        )
    except BaseException:
        await asyncio.to_thread(store.release_unheld, listing.id)
        raise


def check_file_pictures(media_ids: Sequence[str]) -> None:
    """``PUT …/media`` of a file listing (#6710): each id a `FILE_PICTURE_ID`, once. What
    each names is checked when the listing is published (`_checked_pictures`)."""
    seen: set[str] = set()
    for media_id in media_ids:
        if media_id in seen:
            raise _unprocessable(
                "listing_media_duplicate", f"media {media_id!r} is named twice", media_id
            )
        seen.add(media_id)
        if FILE_PICTURE_ID.fullmatch(media_id) is None:
            raise _media_unknown(media_id)
