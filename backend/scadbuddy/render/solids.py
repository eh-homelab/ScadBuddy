from __future__ import annotations

import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import trimesh

from scadbuddy.core.config import Config
from scadbuddy.render.runner import OpenSCADError, render_3mf
from scadbuddy.render.schema import CustomizerSchema, ParamValue
from scadbuddy.render.split import split_by_material

# OpenSCAD's own colour names, read back off `openscad -o x.3mf` for each one.
CSS_COLOURS: dict[str, str] = {
    "aliceblue": "#F0F8FF",
    "antiquewhite": "#FAEBD7",
    "aqua": "#00FFFF",
    "aquamarine": "#7FFFD4",
    "azure": "#F0FFFF",
    "beige": "#F5F5DC",
    "bisque": "#FFE4C4",
    "black": "#000000",
    "blanchedalmond": "#FFEBCD",
    "blue": "#0000FF",
    "blueviolet": "#8A2BE2",
    "brown": "#A52A2A",
    "burlywood": "#DEB887",
    "cadetblue": "#5F9EA0",
    "chartreuse": "#7FFF00",
    "chocolate": "#D2691E",
    "coral": "#FF7F50",
    "cornflowerblue": "#6495ED",
    "cornsilk": "#FFF8DC",
    "crimson": "#DC143C",
    "cyan": "#00FFFF",
    "darkblue": "#00008B",
    "darkcyan": "#008B8B",
    "darkgoldenrod": "#B8860B",
    "darkgray": "#A9A9A9",
    "darkgreen": "#006400",
    "darkgrey": "#A9A9A9",
    "darkkhaki": "#BDB76B",
    "darkmagenta": "#8B008B",
    "darkolivegreen": "#556B2F",
    "darkorange": "#FF8C00",
    "darkorchid": "#9932CC",
    "darkred": "#8B0000",
    "darksalmon": "#E9967A",
    "darkseagreen": "#8FBC8F",
    "darkslateblue": "#483D8B",
    "darkslategray": "#2F4F4F",
    "darkslategrey": "#2F4F4F",
    "darkturquoise": "#00CED1",
    "darkviolet": "#9400D3",
    "deeppink": "#FF1493",
    "deepskyblue": "#00BFFF",
    "dimgray": "#696969",
    "dimgrey": "#696969",
    "dodgerblue": "#1E90FF",
    "firebrick": "#B22222",
    "floralwhite": "#FFFAF0",
    "forestgreen": "#228B22",
    "fuchsia": "#FF00FF",
    "gainsboro": "#DCDCDC",
    "ghostwhite": "#F8F8FF",
    "gold": "#FFD700",
    "goldenrod": "#DAA520",
    "gray": "#808080",
    "green": "#008000",
    "greenyellow": "#ADFF2F",
    "grey": "#808080",
    "honeydew": "#F0FFF0",
    "hotpink": "#FF69B4",
    "indianred": "#CD5C5C",
    "indigo": "#4B0082",
    "ivory": "#FFFFF0",
    "khaki": "#F0E68C",
    "lavender": "#E6E6FA",
    "lavenderblush": "#FFF0F5",
    "lawngreen": "#7CFC00",
    "lemonchiffon": "#FFFACD",
    "lightblue": "#ADD8E6",
    "lightcoral": "#F08080",
    "lightcyan": "#E0FFFF",
    "lightgoldenrodyellow": "#FAFAD2",
    "lightgray": "#D3D3D3",
    "lightgreen": "#90EE90",
    "lightgrey": "#D3D3D3",
    "lightpink": "#FFB6C1",
    "lightsalmon": "#FFA07A",
    "lightseagreen": "#20B2AA",
    "lightskyblue": "#87CEFA",
    "lightslategray": "#778899",
    "lightslategrey": "#778899",
    "lightsteelblue": "#B0C4DE",
    "lightyellow": "#FFFFE0",
    "lime": "#00FF00",
    "limegreen": "#32CD32",
    "linen": "#FAF0E6",
    "magenta": "#FF00FF",
    "maroon": "#800000",
    "mediumaquamarine": "#66CDAA",
    "mediumblue": "#0000CD",
    "mediumorchid": "#BA55D3",
    "mediumpurple": "#9370DB",
    "mediumseagreen": "#3CB371",
    "mediumslateblue": "#7B68EE",
    "mediumspringgreen": "#00FA9A",
    "mediumturquoise": "#48D1CC",
    "mediumvioletred": "#C71585",
    "midnightblue": "#191970",
    "mintcream": "#F5FFFA",
    "mistyrose": "#FFE4E1",
    "moccasin": "#FFE4B5",
    "navajowhite": "#FFDEAD",
    "navy": "#000080",
    "oldlace": "#FDF5E6",
    "olive": "#808000",
    "olivedrab": "#6B8E23",
    "orange": "#FFA500",
    "orangered": "#FF4500",
    "orchid": "#DA70D6",
    "palegoldenrod": "#EEE8AA",
    "palegreen": "#98FB98",
    "paleturquoise": "#AFEEEE",
    "palevioletred": "#DB7093",
    "papayawhip": "#FFEFD5",
    "peachpuff": "#FFDAB9",
    "peru": "#CD853F",
    "pink": "#FFC0CB",
    "plum": "#DDA0DD",
    "powderblue": "#B0E0E6",
    "purple": "#800080",
    "red": "#FF0000",
    "rosybrown": "#BC8F8F",
    "royalblue": "#4169E1",
    "saddlebrown": "#8B4513",
    "salmon": "#FA8072",
    "sandybrown": "#F4A460",
    "seagreen": "#2E8B57",
    "seashell": "#FFF5EE",
    "sienna": "#A0522D",
    "silver": "#C0C0C0",
    "skyblue": "#87CEEB",
    "slateblue": "#6A5ACD",
    "slategray": "#708090",
    "slategrey": "#708090",
    "snow": "#FFFAFA",
    "springgreen": "#00FF7F",
    "steelblue": "#4682B4",
    "tan": "#D2B48C",
    "teal": "#008080",
    "thistle": "#D8BFD8",
    "tomato": "#FF6347",
    "turquoise": "#40E0D0",
    "violet": "#EE82EE",
    "wheat": "#F5DEB3",
    "white": "#FFFFFF",
    "whitesmoke": "#F5F5F5",
    "yellow": "#FFFF00",
    "yellowgreen": "#9ACD32",
}

#: The wrapper has to live beside the model so its ``include <>`` resolves, which
#: puts a transient .scad inside the directory `provenance.source_version` hashes —
#: it skips anything with this prefix, so the two must stay in step. Since #90 that
#: directory is also a git repository, so `library/history.py` writes this same
#: prefix into its .gitignore: three places, one constant.
WRAPPER_PREFIX = "_scadbuddy_solid_"

# A user-defined `color` module shadows the builtin, so a wrapper that keeps only the
# children whose innermost color() matches a target renders that colour on its own --
# as one closed solid, instead of the open shell a material split leaves behind.
WRAPPER_PRELUDE = """// Generated by ScadBuddy. Renders one colour of the model as a closed solid.
_sb_targets = [];
function _sb_up(s) =
  chr([for (i = [0:len(s) - 1]) let (o = ord(s[i])) (o >= 97 && o <= 122) ? o - 32 : o]);
function _sb_low(s) =
  chr([for (i = [0:len(s) - 1]) let (o = ord(s[i])) (o >= 65 && o <= 90) ? o + 32 : o]);
function _sb_h2(v) =
  let (n = round(v * 255), d = "0123456789ABCDEF") str(d[floor(n / 16)], d[n % 16]);
function _sb_hex(c) =
  is_string(c) ? (c[0] == "#" ? _sb_up(c) : str("name:", _sb_low(c)))
               : str("#", _sb_h2(c[0]), _sb_h2(c[1]), _sb_h2(c[2]));
function _sb_match(h) = len([for (t = _sb_targets) if (t == h) 1]) > 0;
module color(c, alpha = 1) { if (_sb_match(_sb_hex(c))) children(); }
"""


@dataclass
class SolidRender:
    meshes: dict[str, trimesh.Trimesh] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def wrapper_source(model_name: str) -> str:
    return f"{WRAPPER_PRELUDE}include <{model_name}>\n"


def targets_for(colour: str) -> list[str]:
    """Every literal a model could have written to produce this material colour."""
    upper = "#" + colour.lstrip("#").upper()
    names = [f"name:{name}" for name, value in CSS_COLOURS.items() if value == upper]
    return [upper, *names]


def _vector_literal(values: Sequence[str]) -> str:
    return "[" + ", ".join(f'"{value}"' for value in values) + "]"


async def render_solids(
    scad_path: Path,
    schema: CustomizerSchema,
    params: Mapping[str, ParamValue],
    colours: Sequence[str],
    work_dir: Path,
    *,
    config: Config,
) -> SolidRender:
    result = SolidRender()
    wrapper = scad_path.parent / f"{WRAPPER_PREFIX}{secrets.token_hex(8)}.scad"
    wrapper.write_text(wrapper_source(scad_path.name), encoding="utf-8")
    try:
        for index, colour in enumerate(colours, start=1):
            out_path = work_dir / f"solid_{index}.3mf"
            targets = _vector_literal(targets_for(colour))
            try:
                await render_3mf(
                    wrapper,
                    schema,
                    params,
                    out_path,
                    config=config,
                    extra_defines=["-D", f"_sb_targets={targets}"],
                )
                parts = split_by_material(out_path)
            except OpenSCADError as error:
                result.warnings.append(f"{colour}: no closed solid ({error}); used the split mesh")
                continue
            if not parts:
                result.warnings.append(f"{colour}: the solid render was empty; used the split mesh")
                continue
            meshes = [part.mesh for part in parts]
            result.meshes[colour] = (
                meshes[0]
                if len(meshes) == 1
                else cast(trimesh.Trimesh, trimesh.util.concatenate(meshes))
            )
    finally:
        wrapper.unlink(missing_ok=True)
    return result
