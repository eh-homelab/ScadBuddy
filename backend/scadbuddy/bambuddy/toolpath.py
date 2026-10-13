"""The sliced plate's toolpath, for the print dialog's Speed, Movement and Layers views (#2072).

Read from the G-code the slicer wrote into the sliced 3MF (``Metadata/plate_N.gcode``):
the file the printer runs, so what is drawn is what prints. Only what lies between the
first layer change and the end of the print's own block is read, so the machine's start
and end code (homing, the purge line, parking) is not drawn.

The answer is binary, as a G-code file of tens of MB makes hundreds of thousands of
moves: a little-endian ``uint32`` header length, the header as UTF-8 JSON, padding to a
multiple of 4, then one record of eight ``float32`` per segment::

    x0, y0, z0, x1, y1, z1, speed (mm/s), kind

``kind`` is 0 for a travel move, else 1 + the index of the segment's feature in
``header.features``. Consecutive collinear segments of the same kind and speed are
merged, and past :data:`MAX_SEGMENTS` the layers above are left out (``truncated``).
"""

from __future__ import annotations

import io
import json
import math
import re
import struct
from array import array
from dataclasses import dataclass, field

#: Segments past this are not drawn: 32 bytes each, so about 13 MB at most.
MAX_SEGMENTS = 400_000
#: Layers past this are not drawn, segments or not: each is a header entry.
MAX_LAYERS = 20_000
#: Distinct features past this are drawn as one, "Other"; a name is cut at 64 characters.
MAX_FEATURES = 64
_FEATURE_NAME = 64
OTHER = "Other"
#: How far (degrees) an arc's chords may turn: a G2/G3 is drawn as chords this fine.
ARC_STEP = math.radians(15)
#: Two segments whose directions differ by less than this are merged (cos of ~0.8°).
_COLLINEAR = 0.9999
#: A move shorter than this (mm) is not drawn.
_EPSILON = 1e-4

_FEATURE = re.compile(r";\s*FEATURE:\s*(.+)")
_Z_HEIGHT = re.compile(r";\s*Z_HEIGHT:\s*([-\d.]+)")
_WORD = re.compile(r"([A-Z])([-+]?\d*\.?\d+)")

#: Bambu Studio's markers. Orca and PrusaSlicer write ``;LAYER_CHANGE``.
_LAYER_CHANGE = ("; CHANGE_LAYER", ";LAYER_CHANGE")
_PRINT_END = ("; EXECUTABLE_BLOCK_END",)

TRAVEL = 0


@dataclass
class Toolpath:
    """The segments, and what the header says about them."""

    segments: array[float] = field(default_factory=lambda: array("f"))
    features: list[str] = field(default_factory=list)
    #: Each layer's height and the index of its first segment.
    layers: list[tuple[float, int]] = field(default_factory=list)
    max_speed: float = 0.0
    truncated: bool = False

    @property
    def count(self) -> int:
        return len(self.segments) // 8

    def header(self) -> dict[str, object]:
        return {
            "version": 1,
            "segment_count": self.count,
            "features": self.features,
            "layers": [{"z": round(z, 4), "first_segment": first} for z, first in self.layers],
            "max_speed": round(self.max_speed, 2),
            "truncated": self.truncated,
        }

    def encode(self) -> bytes:
        head = json.dumps(self.header(), separators=(",", ":")).encode()
        head += b" " * (-len(head) % 4)
        segments = self.segments
        if segments.itemsize != 4 or struct.pack("=I", 1) != struct.pack("<I", 1):
            raise RuntimeError("float32 little-endian is assumed")  # pragma: no cover
        return struct.pack("<I", len(head)) + head + segments.tobytes()


def parse(gcode: bytes, *, max_segments: int = MAX_SEGMENTS) -> Toolpath:
    """The toolpath of one plate's G-code."""
    path = _Parser(max_segments)
    # Line by line, not split up front: a copy of every line would double the file.
    for raw in io.BytesIO(gcode):
        if not path.line(raw.decode("ascii", "replace")):
            break
    return path.result


class _Parser:
    def __init__(self, max_segments: int) -> None:
        self.result = Toolpath()
        self.max_segments = max_segments
        self.started = False
        self.absolute = True
        self.absolute_e = False
        self.x = self.y = self.z = 0.0
        self.e = 0.0
        self.feed = 0.0
        self.feature = 0
        self.feature_ids: dict[str, int] = {}
        # The last segment drawn, for merging: (kind, speed, dx, dy, dz) of unit length.
        self.last: tuple[int, float, float, float, float] | None = None

    def line(self, raw: str) -> bool:
        """Read one line; False once the print's own block has ended."""
        text = raw.strip()
        if not text:
            return True
        if text.startswith(";"):
            return self.comment(text)
        code, _, _ = text.partition(";")
        words = dict(_WORD.findall(code.upper()))
        if not all(math.isfinite(_number(value)) for value in words.values()):
            # A number past float range: not a move a printer would make.
            return True
        command = code.split(None, 1)[0].upper() if code.split() else ""
        if command in ("G0", "G1", "G2", "G3"):
            self.move(command, words)
        elif command == "G90":
            self.absolute = True
        elif command == "G91":
            self.absolute = False
        elif command == "M82":
            self.absolute_e = True
        elif command == "M83":
            self.absolute_e = False
        elif command == "G92":
            self.reset(words)
        return True

    def comment(self, text: str) -> bool:
        if text.startswith(_LAYER_CHANGE):
            self.started = True
            if len(self.result.layers) >= MAX_LAYERS:
                self.result.truncated = True
            if not self.result.truncated:
                self.result.layers.append((self.z, self.result.count))
            self.last = None
            return True
        if text.startswith(_PRINT_END) and self.started:
            return False
        if (match := _Z_HEIGHT.match(text)) and self.result.layers and not self.result.truncated:
            # The layer's own height; the Z move to it may come later.
            _, first = self.result.layers[-1]
            if math.isfinite(height := _number(match.group(1))):
                self.result.layers[-1] = (height, first)
            return True
        if match := _FEATURE.match(text):
            name = match.group(1).strip()[:_FEATURE_NAME]
            if name not in self.feature_ids and len(self.result.features) >= MAX_FEATURES:
                name = OTHER
            if name not in self.feature_ids:
                self.feature_ids[name] = len(self.result.features)
                self.result.features.append(name)
            self.feature = self.feature_ids[name] + 1
        return True

    def reset(self, words: dict[str, str]) -> None:
        if "X" in words:
            self.x = float(words["X"])
        if "Y" in words:
            self.y = float(words["Y"])
        if "Z" in words:
            self.z = float(words["Z"])
        if "E" in words:
            self.e = float(words["E"])

    def target(self, words: dict[str, str]) -> tuple[float, float, float]:
        def axis(name: str, now: float) -> float:
            if name not in words:
                return now
            value = float(words[name])
            return value if self.absolute else now + value

        return axis("X", self.x), axis("Y", self.y), axis("Z", self.z)

    def move(self, command: str, words: dict[str, str]) -> None:
        if "F" in words:
            self.feed = float(words["F"])
        x, y, z = self.target(words)
        extruding = False
        if "E" in words:
            e = float(words["E"])
            delta = e - self.e if self.absolute_e else e
            self.e = e if self.absolute_e else self.e + e
            extruding = delta > 0
        if self.started and not self.result.truncated:
            kind = self.feature if extruding and self.feature else TRAVEL
            if command in ("G2", "G3") and ("I" in words or "J" in words):
                self.arc(command == "G2", words, x, y, z, kind)
            else:
                self.segment(self.x, self.y, self.z, x, y, z, kind)
        self.x, self.y, self.z = x, y, z

    def arc(
        self, clockwise: bool, words: dict[str, str], x: float, y: float, z: float, kind: int
    ) -> None:
        cx = self.x + float(words.get("I", 0))
        cy = self.y + float(words.get("J", 0))
        radius = math.hypot(self.x - cx, self.y - cy)
        start = math.atan2(self.y - cy, self.x - cx)
        end = math.atan2(y - cy, x - cx)
        sweep = end - start
        if clockwise and sweep >= 0:
            sweep -= 2 * math.pi
        elif not clockwise and sweep <= 0:
            sweep += 2 * math.pi
        steps = max(1, math.ceil(abs(sweep) / ARC_STEP))
        px, py, pz = self.x, self.y, self.z
        for step in range(1, steps + 1):
            if step == steps:
                nx, ny = x, y
            else:
                angle = start + sweep * step / steps
                nx, ny = cx + radius * math.cos(angle), cy + radius * math.sin(angle)
            nz = self.z + (z - self.z) * step / steps
            self.segment(px, py, pz, nx, ny, nz, kind)
            px, py, pz = nx, ny, nz

    def segment(
        self, x0: float, y0: float, z0: float, x1: float, y1: float, z1: float, kind: int
    ) -> None:
        if self.result.truncated:
            return
        dx, dy, dz = x1 - x0, y1 - y0, z1 - z0
        length = math.sqrt(dx * dx + dy * dy + dz * dz)
        if length < _EPSILON:
            return
        speed = self.feed / 60
        unit = (dx / length, dy / length, dz / length)
        segments = self.result.segments
        last = self.last
        if (
            last is not None
            and last[0] == kind
            and last[1] == speed
            and last[2] * unit[0] + last[3] * unit[1] + last[4] * unit[2] > _COLLINEAR
            and segments[-5] == _f32(x0)
            and segments[-4] == _f32(y0)
            and segments[-3] == _f32(z0)
        ):
            segments[-5], segments[-4], segments[-3] = x1, y1, z1
            return
        if self.result.count >= self.max_segments:
            # Only whole layers are drawn: the one it was cut in goes too.
            self.result.truncated = True
            if self.result.layers:
                _, first = self.result.layers.pop()
                del segments[first * 8 :]
            return
        segments.extend((x0, y0, z0, x1, y1, z1, speed, float(kind)))
        if kind != TRAVEL:
            self.result.max_speed = max(self.result.max_speed, speed)
        self.last = (kind, speed, *unit)


def _number(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        return math.nan


def _f32(value: float) -> float:
    """``value`` as a float32 holds it, to compare with what the array stored."""
    return float(array("f", (value,))[0])
