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
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from pydantic import BaseModel, Field

from scadbuddy.render.bambu3mf import CORE_NS, ZIP_TIMESTAMP
from scadbuddy.render.schema import ParamValue

SCADBUDDY_NS = "https://github.com/eh-homelab/ScadBuddy"
NS_PREFIX = "ScadBuddy"
PROVENANCE_KEY = f"{NS_PREFIX}:provenance"
DESIGNER = "ScadBuddy"
ROOT_MODEL = "3D/3dmodel.model"

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
    #: ``sha256:<hex>`` of ``model.scad`` as it was when the output was rendered.
    version: str
    output: str
    params: dict[str, ParamValue] = Field(default_factory=dict)
    #: Absolute ScadBuddy URL that reopens the customizer on these values.
    edit_url: str | None = None


def source_version(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _stamped_root_model(xml: str, provenance: Provenance) -> str:
    tag = _MODEL_TAG.search(xml)
    if tag is None:
        raise ValueError("the 3MF root model has no <model> element")
    opening = tag.group(0)
    rest = _OWNED.sub("", xml[tag.end() :])
    if f"xmlns:{NS_PREFIX}=" not in opening:
        opening = f'{opening[:-1]} xmlns:{NS_PREFIX}="{SCADBUDDY_NS}">'
    entries = [f' <metadata name="Designer">{DESIGNER}</metadata>']
    if provenance.edit_url:
        entries.append(
            f' <metadata name="Description">Edit in ScadBuddy: '
            f"{escape(provenance.edit_url)}</metadata>"
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
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in rewritten:
            info = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, payload)


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
            return Provenance.model_validate_json(element.text)
    return None
