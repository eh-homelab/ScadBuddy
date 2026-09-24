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
import os
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from pydantic import BaseModel, Field, ValidationError

from scadbuddy.core.paths import MODEL_META_NAME
from scadbuddy.library.catalogue import README_NAME, THUMBNAIL_NAME
from scadbuddy.library.deeplink import EDIT_NOTE
from scadbuddy.render.bambu3mf import CORE_NS, ZIP_TIMESTAMP
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
        and not path.name.startswith(WRAPPER_PREFIX)
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
    rest = _OWNED.sub("", xml[tag.end() :])
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


def stamp(path: Path, provenance: Provenance) -> None:
    """Rewrite ``path`` with the provenance on its root model, leaving the rest alone."""
    with zipfile.ZipFile(path) as archive:
        entries = [(info.filename, archive.read(info.filename)) for info in archive.infolist()]
    rewritten = [
        (
            name,
            _stamped_root_model(payload.decode("utf-8"), provenance).encode("utf-8")
            if name == ROOT_MODEL
            else payload,
        )
        for name, payload in entries
    ]
    # Written beside the original and moved over it, never truncated in place: the 3MF
    # is the deliverable, and a rewrite that dies halfway would otherwise leave a
    # partial file where a whole one used to be.
    temporary = path.with_name(path.name + ".stamping")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, payload in rewritten:
                info = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


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
