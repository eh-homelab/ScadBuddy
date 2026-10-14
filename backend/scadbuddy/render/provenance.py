"""What produced a 3MF, stamped into the file itself.

The output directory already keeps ``meta.json`` and ``params.json``, but the 3MF
outlives it: it is uploaded to Bambuddy, downloaded, mailed around and re-opened in
Bambu Studio long after ScadBuddy has forgotten the output. Stamping the same facts
into the file is what lets "Edit in ScadBuddy" still resolve when the record is gone.

The stamp is a namespaced ``<metadata>`` element on the root model, the pattern
Bambu Studio itself uses for ``BambuStudio:3mfVersion`` — so Bambu Studio reads the
file exactly as before and every other part is left byte-for-byte alone.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import struct
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import BinaryIO
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from pydantic import BaseModel, Field, ValidationError

from scadbuddy.core.paths import MODEL_META_NAME, RENDERS_DIR_NAME
from scadbuddy.library.catalogue import README_NAME, THUMBNAIL_NAME
from scadbuddy.library.deeplink import EDIT_NOTE
from scadbuddy.library.media import MEDIA_DIR
from scadbuddy.render.bambu3mf import CORE_NS, PROJECT_SETTINGS_NAME, ZIP_TIMESTAMP
from scadbuddy.render.schema import ParamValue
from scadbuddy.render.solids import WRAPPER_PREFIX

SCADBUDDY_NS = "https://github.com/eh-homelab/ScadBuddy"
NS_PREFIX = "ScadBuddy"
PROVENANCE_KEY = f"{NS_PREFIX}:provenance"
DESIGNER = "ScadBuddy"
ROOT_MODEL = "3D/3dmodel.model"

#: Everything under a model's directory is hashed except these, because a model may
#: ``import()`` or ``include`` anything beside it: an STL swapped under an unchanged
#: .scad renders different geometry, and a glob of ``*.scad`` would call that the
#: same version. What is left out is what ScadBuddy itself puts there and what no
#: render reads — `model.json`, the schema cache keyed off the .scad already, plus
#: the catalogue's own presentation files, which would otherwise make uploading a
#: thumbnail look like a new version of the model.
NOT_SOURCE = frozenset({MODEL_META_NAME, THUMBNAIL_NAME, README_NAME})

_MODEL_TAG = re.compile(r"<model\b[^>]*>")
# The keys stamp() owns. Removing them first keeps a re-stamp idempotent; nothing
# but stamp() writes them, because ScadBuddy wrote the file it is stamping.
_OWNED = re.compile(
    rf'[ \t]*<metadata name="(?:Designer|Description|{PROVENANCE_KEY})"[^>]*>.*?</metadata>\n?',
    re.DOTALL,
)


class Provenance(BaseModel):
    """The model, the revision of it, and every parameter value behind one output."""

    #: The catalogue slug — ScadBuddy has no other model id.
    model: str
    #: What the model was when the output was rendered; see source_version.
    version: str
    output: str
    params: dict[str, ParamValue] = Field(default_factory=dict)
    #: Absolute ScadBuddy URL that reopens the customizer on these values.
    edit_url: str | None = None


def source_version(model_dir: Path) -> str:
    """A content hash over everything a model is made of, ordered by path.

    Deliberately a free-form string rather than a structured field: #90 turns the
    models directory into a git repository and puts the commit id here instead, and
    either shape fits without migrating the records already written.
    """
    digest = hashlib.sha256()
    for name, payload in sorted(
        (path.relative_to(model_dir).as_posix(), path.read_bytes())
        for path in model_dir.rglob("*")
        if path.is_file()
        and path.name not in NOT_SOURCE
        # The template's gallery (#274): presentation, like the thumbnail, and a
        # video there would be a gigabyte hashed on every render.
        and path.relative_to(model_dir).parts[0] != MEDIA_DIR
        and not path.name.startswith(WRAPPER_PREFIX)
        # The renders kept under the model are its output, not its source.
        and RENDERS_DIR_NAME not in path.relative_to(model_dir).parts
    ):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def _stamped_root_model(xml: str, provenance: Provenance) -> str:
    """Splice the stamp into the root model, leaving every other byte as written.

    Deliberately not an ElementTree round trip, though `read` parses with one: the
    3MF's production extension is read by prefix (`p:path`, `p:UUID` on every
    component and build item), and re-serializing is free to rename that prefix,
    reorder attributes and re-indent. Nothing downstream would notice until Bambu
    Studio refused the file. The cost of splicing is that it assumes the shape
    `bambu3mf.root_model` writes, so both assumptions are checked below.
    """
    tag = _MODEL_TAG.search(xml)
    if tag is None:
        raise ValueError("the 3MF root model has no <model> element")
    opening = tag.group(0)
    if opening.endswith("/>"):
        # The namespace is added by replacing the tag's final ">", which would eat
        # the slash here and leave XML nothing can parse. Nothing re-reads the file
        # on the write path, so refuse loudly rather than ship a broken 3MF.
        raise ValueError("the 3MF root model is self-closing; nothing to stamp onto")
    # Only the model's own metadata is ours to remove. 3MF allows <metadata> inside an
    # <object> as well, and everything from <resources> on belongs to whoever put it
    # there — an unscoped substitution would quietly take a per-object Designer with it.
    rest = xml[tag.end() :]
    head, marker, tail = rest.partition("<resources")
    rest = _OWNED.sub("", head) + marker + tail
    if f"xmlns:{NS_PREFIX}=" not in opening:
        opening = f'{opening[:-1]} xmlns:{NS_PREFIX}="{SCADBUDDY_NS}">'
    entries = [f' <metadata name="Designer">{DESIGNER}</metadata>']
    if provenance.edit_url:
        entries.append(
            f' <metadata name="Description">{EDIT_NOTE}{escape(provenance.edit_url)}</metadata>'
        )
    entries.append(
        f' <metadata name="{PROVENANCE_KEY}">'
        f"{escape(provenance.model_dump_json(exclude_none=True))}</metadata>"
    )
    return xml[: tag.start()] + opening + "\n" + "\n".join(entries) + rest


def with_print_settings(project_settings: bytes, print_settings: Mapping[str, str]) -> bytes:
    """``project_settings.config`` with a template's ``print_settings`` (#770) over the
    process, listed in ``different_settings_to_system`` (the process's edits first, then
    one entry per filament and one for the printer, as Bambu Studio saves a project), so
    Bambu Studio shows them as changes to the system preset. Every other key stays."""
    settings = json.loads(project_settings)
    settings.update(print_settings)
    filaments = len(settings.get("filament_settings_id") or [])
    settings["different_settings_to_system"] = [";".join(print_settings), *[""] * filaments, ""]
    return (json.dumps(settings, indent=4) + "\n").encode("utf-8")


#: A local file header's fixed part: signature to extra-field length (APPNOTE 4.3.7).
_LOCAL_HEADER = struct.Struct("<4s2B4HL2L2H")
_LOCAL_SIGNATURE = b"PK\x03\x04"
#: General purpose flag bit 0: the entry is encrypted, so its bytes are not copied as is.
_ENCRYPTED = 0x1


def _copy_raw(source: BinaryIO, info: zipfile.ZipInfo, target: zipfile.ZipFile) -> None:
    """Append ``info``'s entry to ``target`` as its compressed bytes stand (#2755).

    `zipfile` has no public way to move an entry without inflating and deflating it
    again, and a saved 3MF is mostly mesh objects the stamp never touches: rewriting
    them cost seconds of CPU per save (3.5 s for a 14 MB arranged 3MF in the API pod).
    The entry keeps its CRC and sizes, so a reader verifies it exactly as before. It
    writes through the `ZipFile`'s own file and bookkeeping, as `writestr` does."""
    source.seek(info.header_offset)
    header = _LOCAL_HEADER.unpack(source.read(_LOCAL_HEADER.size))
    if header[0] != _LOCAL_SIGNATURE:
        raise zipfile.BadZipFile(f"no local header for {info.filename}")
    source.seek(info.header_offset + _LOCAL_HEADER.size + header[-2] + header[-1])
    data = source.read(info.compress_size)
    entry = zipfile.ZipInfo(info.filename, date_time=ZIP_TIMESTAMP)
    entry.compress_type = info.compress_type
    entry.CRC = info.CRC
    entry.compress_size = info.compress_size
    entry.file_size = info.file_size
    fp = target.fp
    assert fp is not None
    entry.header_offset = fp.tell()
    fp.write(entry.FileHeader())
    fp.write(data)
    target.filelist.append(entry)
    target.NameToInfo[entry.filename] = entry
    target.start_dir = fp.tell()
    target._didModify = True  # type: ignore[attr-defined]  # what writestr sets


def stamp(
    path: Path,
    provenance: Provenance,
    print_settings: Mapping[str, str] | None = None,
    *,
    source: Path | None = None,
) -> None:
    """Rewrite ``path`` with the provenance on its root model and the template's
    ``print_settings`` in its project settings, leaving the rest alone. Written when the
    output is saved, so the saved file is what a download serves (#2652). With
    ``source``, the 3MF is read from there and written to ``path``: a save stamps the
    job's file as it copies it, rather than copying it and then rewriting the copy.
    Only the entries it changes are read and written again; every other one is copied
    as its compressed bytes (#2755)."""
    origin = path if source is None else source
    # Written beside the original and moved over it, never truncated in place: the 3MF
    # is the deliverable, and a rewrite that dies halfway would otherwise leave a
    # partial file where a whole one used to be.
    temporary = path.with_name(path.name + ".stamping")
    try:
        with (
            origin.open("rb") as raw,
            zipfile.ZipFile(raw) as archive,
            zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as written,
        ):
            for info in archive.infolist():
                settings = info.filename == PROJECT_SETTINGS_NAME and bool(print_settings)
                changed = info.filename == ROOT_MODEL or settings
                if not changed and not info.flag_bits & _ENCRYPTED:
                    _copy_raw(raw, info, written)
                    continue
                payload = archive.read(info.filename)
                if info.filename == ROOT_MODEL:
                    payload = _stamped_root_model(payload.decode("utf-8"), provenance).encode(
                        "utf-8"
                    )
                elif settings:
                    assert print_settings is not None
                    payload = with_print_settings(payload, print_settings)
                entry = zipfile.ZipInfo(info.filename, date_time=ZIP_TIMESTAMP)
                # Rewritten the way it was written: the 3MF writer stores the cover PNGs
                # uncompressed on purpose (#107).
                entry.compress_type = info.compress_type
                written.writestr(entry, payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _unstamped(xml: str) -> str:
    """The root model without what :func:`stamp` writes onto it."""
    tag = _MODEL_TAG.search(xml)
    if tag is None:
        return xml
    head, marker, tail = xml[tag.end() :].partition("<resources")
    return xml[: tag.end()] + _OWNED.sub("", head) + marker + tail


def content_key(payload: bytes) -> str:
    """What a 3MF holds, less the stamp naming the output it was saved as (#2476): two
    outputs of one render, saved twice, have the same key. Every entry by name and
    bytes, the root model's with the stamp taken out. Bytes that are no archive are
    keyed as they are."""
    digest = hashlib.sha256()
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile:
        return "sha256:" + hashlib.sha256(payload).hexdigest()
    with archive:
        for name in sorted(archive.namelist()):
            data = archive.read(name)
            if name == ROOT_MODEL:
                data = _unstamped(data.decode("utf-8")).encode("utf-8")
            digest.update(name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(data)
            digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def read(path: Path) -> Provenance | None:
    """The provenance stamped into ``path``, or None when it carries none."""
    try:
        with zipfile.ZipFile(path) as archive:
            xml = archive.read(ROOT_MODEL).decode("utf-8")
    except (OSError, KeyError, zipfile.BadZipFile, UnicodeDecodeError):
        return None
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    for element in root.findall(f"{{{CORE_NS}}}metadata"):
        if element.get("name") == PROVENANCE_KEY and element.text:
            try:
                return Provenance.model_validate_json(element.text)
            except ValidationError:
                # A stamp this version cannot read — corrupted, hand-edited, or
                # written by a later ScadBuddy — is a miss like any other, so the
                # caller 404s instead of surfacing a ValidationError as a 500.
                return None
    return None
