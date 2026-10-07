#!/usr/bin/env bash
# Render models/dollhouse-kit: every piece with its defaults, the main style
# variations, every piece at its largest settings, and "max colour" cases with
# all 32 colours distinct. Then check concrete facts:
#   * colour parts: exactly the colours the piece should use, none uncoloured
#   * no two colours overlap: the per-colour closed solids (rendered the way
#     ScadBuddy renders them) add up to the volume of the whole piece
#   * bed fit: every piece at max settings fits 300 x 300 x 300 mm and sits on z=0
#   * grid: walls, railings, floor tiles are exactly units x module - 0.4 mm,
#     courses exactly course_height, corner arms half a module - 0.4
#   * fit: mated pieces (in line, corner, course on course, wall on floor,
#     tile to tile, stair halves, door leaves) do not intersect; a key in its
#     pocket does not intersect, and the same key moved clearance + 0.05 mm
#     does, so the play is the clip_clearance parameter
#   * the door opening is at least 320 mm clear across its two courses; the
#     default window spans a 30 cm doll's eye height
#   * each piece renders in under 10 s
#
# The checking runs on the host with python3 and the standard library only:
# the OpenSCAD image has no Python.
set -euo pipefail

cd "$(dirname "$0")"

BASE_IMAGE="${SCADBUDDY_OPENSCAD_IMAGE:-openscad/openscad:dev}"
IMAGE="${SCADBUDDY_FONTS_IMAGE:-scadbuddy-verify:local}"
OUT="${OUT_DIR:-.verify}"

mkdir -p "$OUT"

# Render in the image with the ScadBuddy font packages (same recipe as
# models/name-keychain); this model uses no text, so any build of it will do.
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "==> $IMAGE missing; building it from $BASE_IMAGE with the image's font packages"
    docker build -q -t "$IMAGE" - <<DOCKERFILE
FROM $BASE_IMAGE
RUN apt-get update \\
 && apt-get install -y --no-install-recommends \\
      fonts-lobster fonts-lobstertwo fonts-dejavu fonts-noto-core \\
 && fc-cache -f \\
 && rm -rf /var/lib/apt/lists/*
DOCKERFILE
fi
echo "==> rendering with $IMAGE"

IMAGE="$IMAGE" OUT="$OUT" python3 - <<'PY'
import math, os, re, shlex, struct, subprocess, sys, zipfile
import xml.etree.ElementTree as ET
from collections import Counter

IMAGE, OUT = os.environ["IMAGE"], os.environ["OUT"]
NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
TOL = 0.01
BED = 300.0

# ---- colour parameters, in extruder order, with their defaults (mirrors model.scad)
COLOURS = [
    ("wall_color", "#F8BBD0"), ("exterior_color", "#F48FB1"), ("mortar_color", "#FFFFFF"),
    ("trim_color", "#FFFFFF"), ("wall_inside_color", "#FFF8E1"), ("wallpaper_color", "#F06292"),
    ("wallpaper_color_2", "#CE93D8"), ("wainscot_color", "#FFFFFF"), ("baseboard_color", "#FFFFFF"),
    ("crown_color", "#FFFFFF"), ("window_frame_color", "#FFFFFF"), ("window_pane_color", "#B3E5FC"),
    ("shutter_color", "#BA68C8"), ("flower_box_color", "#A1887F"), ("flower_color", "#FF4081"),
    ("flower_color_2", "#FFEB3B"), ("leaf_color", "#66BB6A"), ("door_frame_color", "#FFFFFF"),
    ("door_color", "#9575CD"), ("door_knob_color", "#FFD54F"), ("floor_color", "#D7A86E"),
    ("floor_accent_color", "#8D6E63"), ("rug_color", "#80DEEA"), ("rug_border_color", "#FFFFFF"),
    ("roof_color", "#7E57C2"), ("roof_accent_color", "#B39DDB"), ("stair_tread_color", "#C8945A"),
    ("stair_riser_color", "#FFFFFF"), ("railing_color", "#FFFFFF"), ("spindle_color", "#F06292"),
    ("clip_color", "#FFFFFF"),
]
DEFAULT_COL = dict(COLOURS)
# every colour different: 31 distinct greys-and-hues, deterministic
DISTINCT = {n: "#%02X%02X%02X" % (40 + 7 * i, 200 - 5 * i, (97 * i) % 256) for i, (n, _) in enumerate(COLOURS)}

# ---- geometry constants (mirror model.scad)
D = dict(module_size=150, width_units=1, depth_units=2, course_height=210, wall_thickness=6,
         floor_thickness=6, clip_clearance=0.2, window_sill=40, window_width=70, window_height=120,
         door_width=100, door_height=330, stair_width=100, tread_depth=24, railing_height=150)
G, S, REL, FR, FBD = 0.4, 0.6, 1.2, 1.6, 10
KA, KN, KH, PD = 7, 6, 20, 12
THR, TAB = 5, 8

WALLS = ["wall", "wall_window", "wall_door_lower", "wall_door_upper"]
MAXSET = dict(module_size=150, width_units=2, depth_units=2, course_height=240, wall_thickness=10,
              floor_thickness=10, texture_size=20, window_width=180, window_height=200, window_sill=120,
              door_width=140, door_height=400, stair_width=140, tread_depth=32, railing_height=180,
              connector_count=30, exterior="siding", wallpaper="hearts", flower_box=True, shutters=True,
              glass=True, trim=True)

# (name, overrides, colour params the piece must use, expected bbox extents or None)
W_IN_UP = ["wall_color", "wall_inside_color", "wallpaper_color", "wallpaper_color_2", "crown_color"]
W_IN_LO = ["wall_color", "wall_inside_color", "wallpaper_color", "wallpaper_color_2", "wainscot_color", "baseboard_color"]
WIN = ["window_frame_color", "window_pane_color", "flower_box_color", "flower_color", "flower_color_2", "leaf_color"]
CASES = [
    ("defaults", {}, W_IN_UP + WIN + ["shutter_color", "trim_color"]),
    ("wall-brick-lower", dict(piece="wall", course="lower", exterior="brick", wallpaper="diamonds"),
     W_IN_LO + ["exterior_color", "mortar_color"]),
    ("wall-half-plain", dict(piece="wall", width_units=0.5, wallpaper="none", crown_moulding=False),
     ["wall_color", "wall_inside_color"]),
    ("window-arched-siding", dict(window_style="arched", width_units=2, exterior="siding",
                                  window_width=120, window_height=160, wallpaper="stars"),
     W_IN_UP + WIN + ["shutter_color", "trim_color", "exterior_color"]),
    ("window-round-stone", dict(window_style="round", width_units=1.5, exterior="stone",
                                course="lower", wallpaper="polka_dots"),
     W_IN_LO + WIN + ["exterior_color", "mortar_color"]),
    ("window-bare", dict(glass=False, shutters=False, flower_box=False, trim=False, wallpaper="none"),
     ["wall_color", "wall_inside_color", "crown_color", "window_frame_color"]),
    ("door-lower", dict(piece="wall_door_lower", exterior="stucco"),
     W_IN_LO + ["exterior_color", "door_frame_color", "trim_color"]),
    ("door-upper-arched", dict(piece="wall_door_upper", door_style="arched", wallpaper="hearts"),
     W_IN_UP + ["door_frame_color"]),
    ("door-lower-french-notrim", dict(piece="wall_door_lower", door_style="french", width_units=1.5,
                                      door_width=140, trim=False, wallpaper="stars"), W_IN_LO),
    # #415: a pattern edge crossing a curved opening's lining near one of its vertices
    # left a sliver between the inside and wallpaper colours, and the 3MF export
    # failed ("Can't add triangle"). These two did before the fix.
    ("arch-stars-sliver-415", dict(piece="wall_door_upper", door_style="arched", module_size=100, width_units=0.5,
                                   wallpaper="stars", pattern_repeats=12, door_height=320, clip_clearance=0.5),
     W_IN_UP + ["door_frame_color"]),
    ("round-dots-sliver-415", dict(window_style="round", module_size=60, width_units=1.5, wallpaper="polka_dots",
                                   pattern_repeats=12, clip_clearance=0.3), None),
    ("door-upper-french", dict(piece="wall_door_upper", door_style="french", width_units=1.5, door_width=140),
     W_IN_UP + ["door_frame_color"]),
    ("leaf-lower", dict(piece="door_leaf_lower"), ["door_color", "door_knob_color"]),
    ("leaf-upper-arched", dict(piece="door_leaf_upper", door_style="arched"), ["door_color"]),
    ("leaf-lower-french", dict(piece="door_leaf_lower", door_style="french", width_units=1.5, door_width=140),
     ["door_color", "window_pane_color", "door_knob_color"]),
    ("leaf-upper-french", dict(piece="door_leaf_upper", door_style="french", width_units=1.5, door_width=140),
     ["door_color", "window_pane_color"]),
    ("corner-plain-upper", dict(piece="corner_post"), ["wall_color", "wall_inside_color", "crown_color"]),
    ("corner-brick-lower", dict(piece="corner_post", course="lower", exterior="brick"),
     ["exterior_color", "mortar_color", "wall_inside_color", "wainscot_color", "baseboard_color"]),
    ("corner-siding", dict(piece="corner_post", exterior="siding"), ["exterior_color", "wall_inside_color", "crown_color"]),
    ("floor-planks", dict(piece="floor_tile"), ["floor_color", "floor_accent_color"]),
    ("floor-herringbone-rug", dict(piece="floor_tile", width_units=2, floor_texture="herringbone", rug="oval"),
     ["floor_color", "floor_accent_color", "rug_color", "rug_border_color"]),
    ("floor-stairwell", dict(piece="floor_tile", width_units=1.5, floor_texture="checkerboard", stairwell=True),
     ["floor_color", "floor_accent_color"]),
    ("floor-tiles-rect-rug", dict(piece="floor_tile", floor_texture="tiles", rug="rectangle"),
     ["floor_color", "floor_accent_color", "rug_color", "rug_border_color"]),
    ("floor-plain", dict(piece="floor_tile", floor_texture="plain", depth_units=0.5), ["floor_color"]),
    # Too small for a rug: it is left off instead of drawn with a negative size.
    ("floor-rug-no-room", dict(piece="floor_tile", module_size=100, width_units=0.5, depth_units=0.5,
                               wall_thickness=7, floor_texture="plain", rug="rectangle"), ["floor_color"]),
    # Second audit: pieces that cannot be what was asked for say so with a NOTE.
    ("note-window-shrunk", dict(width_units=0.5, window_width=180, window_height=200, course_height=180), None),
    ("note-door-shrunk", dict(piece="wall_door_lower", width_units=0.5, door_width=140, door_height=400,
                              course_height=180), None),
    # arched / round windows narrowed by their height, not the wall: the NOTE names that
    ("note-window-arch-short", dict(window_style="arched", width_units=2, window_width=80, window_height=40), None),
    ("note-window-arch-high-sill", dict(window_style="arched", width_units=2, window_width=180, window_sill=120), None),
    ("note-window-round-high-sill", dict(window_style="round", width_units=2, window_width=180, window_sill=120), None),
    ("note-stairwell-narrow", dict(piece="floor_tile", width_units=0.5, stairwell=True), None),
    ("roof-shingles", dict(piece="roof_panel"), ["roof_color", "roof_accent_color"]),
    ("roof-tiles", dict(piece="roof_panel", roof_style="tiles", width_units=2), ["roof_color", "roof_accent_color"]),
    ("roof-flat", dict(piece="roof_panel", roof_style="flat"), ["roof_color", "roof_accent_color"]),
    ("stairs-lower", dict(piece="stairs_lower"), ["stair_tread_color", "stair_riser_color"]),
    ("stairs-upper", dict(piece="stairs_upper"), ["stair_tread_color", "stair_riser_color"]),
    ("railing", dict(piece="railing", width_units=1.5), ["railing_color", "spindle_color"]),
    ("keys", dict(piece="connectors"), ["clip_color"]),
    ("pegs", dict(piece="connectors", connector_type="pegs"), ["clip_color"]),
    ("hinge-pins", dict(piece="connectors", connector_type="hinge_pins", connector_count=2), ["clip_color"]),
    ("thin-walls", dict(wall_thickness=4, floor_thickness=5, course_height=180, window_style="arched"),
     W_IN_UP + WIN + ["shutter_color", "trim_color"]),
    ("tight-window", dict(course_height=180, window_sill=120, window_style="arched", window_width=180,
                          window_height=200), W_IN_UP + WIN + ["shutter_color", "trim_color"]),
    # every colour distinct: the part count must equal the colours the piece uses
    ("maxcol-window", dict(course="lower", exterior="brick", wallpaper="hearts", width_units=2, **DISTINCT),
     W_IN_LO + WIN + ["shutter_color", "trim_color", "exterior_color", "mortar_color"]),
    ("maxcol-door-french", dict(piece="wall_door_lower", door_style="french", width_units=2, door_width=140,
                                exterior="stucco", wallpaper="stripes", **DISTINCT),
     W_IN_LO + ["exterior_color", "door_frame_color", "trim_color"]),
    ("maxcol-leaf-french", dict(piece="door_leaf_lower", door_style="french", width_units=2, door_width=140, **DISTINCT),
     ["door_color", "window_pane_color", "door_knob_color"]),
    ("maxcol-corner", dict(piece="corner_post", course="lower", exterior="stone", **DISTINCT),
     ["exterior_color", "mortar_color", "wall_inside_color", "wainscot_color", "baseboard_color"]),
    ("maxcol-floor", dict(piece="floor_tile", floor_texture="herringbone", rug="rectangle", width_units=2, **DISTINCT),
     ["floor_color", "floor_accent_color", "rug_color", "rug_border_color"]),
    ("maxcol-roof", dict(piece="roof_panel", **DISTINCT), ["roof_color", "roof_accent_color"]),
    ("maxcol-stairs", dict(piece="stairs_lower", **DISTINCT), ["stair_tread_color", "stair_riser_color"]),
    ("maxcol-railing", dict(piece="railing", **DISTINCT), ["railing_color", "spindle_color"]),
]
PIECES = ["wall", "wall_window", "wall_door_lower", "wall_door_upper", "door_leaf_lower", "door_leaf_upper",
          "corner_post", "floor_tile", "roof_panel", "stairs_lower", "stairs_upper", "railing", "connectors"]
for pc in PIECES:
    extra = [dict(door_style=s) for s in ("plain", "arched", "french")] if "door" in pc else [{}]
    if pc == "connectors":
        extra = [dict(connector_type=t) for t in ("keys", "pegs", "hinge_pins")]
    if pc == "floor_tile":
        extra = [dict(stairwell=True, rug="oval", floor_texture="herringbone")]
    if pc == "roof_panel":
        extra = [dict(roof_style="tiles")]
    for e in extra:
        tag = "-".join(str(v) for v in e.values())
        CASES.append(("max-%s%s" % (pc, "-" + tag if tag else ""), dict(MAXSET, piece=pc, **e), None))
CASES.append(("room-preview", dict(preview="room"), None))
# the closed-solid overlap check runs on these (plus every maxcol case)
OVERLAP = {"defaults", "wall-brick-lower", "window-arched-siding", "window-round-stone", "door-lower",
           "door-upper-arched", "arch-stars-sliver-415", "round-dots-sliver-415", "leaf-lower", "corner-siding",
           "floor-herringbone-rug", "roof-tiles",
           "stairs-upper", "railing", "thin-walls"}


def scad(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return '"%s"' % v
    return repr(v)


def defines(ov):
    return " ".join("-D %s" % shlex.quote("%s=%s" % (k, scad(v))) for k, v in ov.items())


def docker(script):
    r = subprocess.run(["docker", "run", "--rm", "--label", "scadbuddy-verify=" + os.environ.get("SCADBUDDY_VERIFY_LABEL", "local"), "-v", os.getcwd() + ":/w", "-w", "/w",
                        IMAGE, "bash", "-c", script], capture_output=True, text=True)
    if "WARNING" in r.stderr or "ERROR" in r.stderr:
        print(r.stderr[-3000:])
        sys.exit("FAIL: OpenSCAD reported errors or warnings")
    return r.stdout + r.stderr


def read_3mf(path):
    root = ET.fromstring(zipfile.ZipFile(path).read("3D/3dmodel.model"))
    mats = [(b.get("name"), (b.get("displaycolor") or "")[:7].upper()) for b in root.iter(NS + "base")]
    verts = [(float(v.get("x")), float(v.get("y")), float(v.get("z"))) for v in root.iter(NS + "vertex")]
    tris = [(int(t.get("v1")), int(t.get("v2")), int(t.get("v3")), int(t.get("p1") or 0))
            for t in root.iter(NS + "triangle")]
    return mats, verts, tris


def read_stl(path):
    data = open(path, "rb").read()
    tris = []
    if data[:5] == b"solid" and b"facet" in data[:300]:
        vs = [tuple(map(float, m)) for m in re.findall(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data)]
        tris = [vs[i:i + 3] for i in range(0, len(vs), 3)]
    else:
        n = struct.unpack("<I", data[80:84])[0]
        for i in range(n):
            f = struct.unpack("<12f", data[84 + 50 * i: 84 + 50 * i + 48])
            tris.append([f[3:6], f[6:9], f[9:12]])
    return tris


def volume(tris):
    v = 0.0
    for a, b, c in tris:
        v += (a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0])
              + a[2] * (b[0] * c[1] - b[1] * c[0]))
    return v / 6


failures = []


def check(ok, msg):
    print(("  PASS  " if ok else "  FAIL  ") + msg)
    if not ok:
        failures.append(msg)


def P(ov, k):
    return ov.get(k, D.get(k))


# ---- phase 1: combined renders, each timed
script = []
for name, ov, _ in CASES:
    script.append('s=$(date +%%s%%N); openscad --backend=Manifold %s -o %s/%s.3mf model.scad >%s/%s.log 2>&1 '
                  '|| echo "RENDERFAIL %s"; e=$(date +%%s%%N); echo "TIME %s $(( (e - s) / 1000000 ))"; '
                  'grep -hE "WARNING|ERROR" %s/%s.log >&2 || true'
                  % (defines(ov), OUT, name, OUT, name, name, name, OUT, name))
log = docker("\n".join(script))
if "RENDERFAIL" in log:
    print(log)
    sys.exit("FAIL: a case did not render")
secs = {m.group(1): int(m.group(2)) / 1000 for m in re.finditer(r"TIME (\S+) (\d+)", log)}

# ---- phase 2: closed per-colour solids + the whole piece as one solid
with open("%s/wrap.scad" % OUT, "w") as f:
    f.write('_sb_t = "";\nmodule color(c, alpha = 1) { if (c == _sb_t) children(); }\n'
            "include <../model.scad>\n")
combined = {name: read_3mf("%s/%s.3mf" % (OUT, name)) for name, _, _ in CASES}
jobs = []
for name, ov, _ in CASES:
    if name not in OVERLAP and not name.startswith("maxcol"):
        continue
    mats, _, tris = combined[name]
    used = Counter(t[3] for t in tris)
    jobs.append("openscad --backend=Manifold %s --export-format binstl -o %s/%s_all.stl model.scad"
                % (defines(ov), OUT, name))
    for i, (n, col) in enumerate(mats):
        if n != "Default" and used.get(i):
            jobs.append("openscad --backend=Manifold %s -D '_sb_t=\"%s\"' --export-format binstl -o %s/%s_%s.stl %s/wrap.scad"
                        % (defines(ov), col, OUT, name, col[1:], OUT))
docker("\n".join(j + " >/dev/null 2>&1" for j in jobs))

# What each case must report (NOTE lines, #285); a case not listed must report nothing.
NOTES = {
    "note-window-shrunk": [
        "NOTE: window_width reduced to 45 mm to fit a 0.5-unit wall (it keeps room for the end keys)",
        "NOTE: window_height reduced to 122.4 mm so the window clears the top-edge pegs and keys",
        "NOTE: no room for shutters beside this window; left off"],
    "note-door-shrunk": [
        "NOTE: door walls are at least 1 unit long; width_units 0.5 made as 1",
        "NOTE: door_width reduced to 108 mm to fit a 1-unit wall with its frame and end keys",
        "NOTE: door_height reduced to 330 mm to leave a 25 mm header in two 180 mm courses"],
    "note-window-arch-short": [
        "NOTE: window_width reduced to 70 mm so the arch fits the window's 40 mm height"
        " (raise window_height for a wider one)"],
    "note-window-arch-high-sill": [
        "NOTE: window_width reduced to 134.8 mm so the arch fits the window's 72.4 mm height"
        " (lower window_sill for a wider one)",
        "NOTE: window_height reduced to 72.4 mm so the window clears the top-edge pegs and keys"],
    "note-window-round-high-sill": [
        "NOTE: window_width reduced to 72.4 mm: a round window is as tall as it is wide and only 72.4 mm"
        " fits between the sill and the top-edge pegs (lower window_sill for a bigger one)"],
    "note-stairwell-narrow": [
        "NOTE: the stairwell is only 57.2 mm wide on this tile; the stairs are 100 mm wide (use a wider tile)"],
    "floor-rug-no-room": ["NOTE: this floor tile is too small for a rug; left off"],
}
# cases whose NOTEs are exactly the listed ones: the wall NOTE must not also blame the wall
NOTES_EXACT = {"note-window-arch-short", "note-window-arch-high-sill", "note-window-round-high-sill"}
NOTE_FREE = {"defaults", "door-lower", "door-upper-arched", "leaf-lower", "floor-herringbone-rug", "floor-stairwell",
             "hinge-pins", "room-preview"}

# ---- checks per case
for name, ov, want in CASES:
    mats, verts, tris = combined[name]
    used = Counter(t[3] for t in tris)
    named = {col for i, (n, col) in enumerate(mats) if n != "Default" and used.get(i)}
    xs, ys, zs = zip(*verts)
    ext = (max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))
    print("\n[%s] %.1fs  bbox %.2f x %.2f x %.2f  parts=%d"
          % (name, secs.get(name, -1), ext[0], ext[1], ext[2], len(named)))
    check(used.get(0, 0) == 0, "Default material has no triangles (got %d)" % used.get(0, 0))
    log_notes = [l for l in open("%s/%s.log" % (OUT, name)).read().splitlines() if l.startswith('ECHO: "NOTE:')]
    for want_note in NOTES.get(name, []):
        check(any(want_note in l for l in log_notes), "reports %r" % want_note)
    if name in NOTES_EXACT:
        check(len(log_notes) == len(NOTES[name]), "reports only its %d NOTE(s) (got %s)" % (len(NOTES[name]), log_notes))
    if name in NOTE_FREE:
        check(not log_notes, "reports no NOTE (got %s)" % log_notes)
    check(abs(min(zs)) <= TOL, "sits on z=0 (min z %.3f)" % min(zs))
    if name != "room-preview":
        check(secs.get(name, 99) < 10, "renders in under 10 s (%.1f s)" % secs.get(name, 99))
    if name.startswith("max-"):
        check(all(e <= BED + TOL for e in ext),
              "fits the 300 x 300 x 300 mm envelope (%.1f x %.1f x %.1f)" % ext)
    if want is not None:
        cols = {ov.get(p, DEFAULT_COL[p]).upper() for p in want}
        check(named == cols, "colour parts are exactly %d: %s%s"
              % (len(cols), sorted(cols), "" if named == cols else " (got %s)" % sorted(named)))
        if name.startswith("maxcol"):
            check(len(named) == len(want), "all-distinct colours: %d parts for %d colours used" % (len(named), len(want)))

    # grid dimensions
    pc = ov.get("piece", "wall_window")
    M, u, v, CH, T = P(ov, "module_size"), P(ov, "width_units"), P(ov, "depth_units"), P(ov, "course_height"), P(ov, "wall_thickness")
    if want is not None and pc in WALLS and "preview" not in ov:
        uu = max(u, 1) if "door" in pc else u
        check(abs(ext[0] - (uu * M - G)) <= TOL, "wall length %.2f == %g x %g - 0.4" % (ext[0], uu, M))
        check(abs(ext[1] - CH) <= TOL, "wall course height %.2f == %g" % (ext[1], CH))
    if want is not None and pc in ("floor_tile", "roof_panel"):
        check(abs(ext[0] - (u * M - G)) <= TOL and abs(ext[1] - (v * M - G)) <= TOL,
              "tile %.2f x %.2f == (%g x %g, %g x %g) - 0.4" % (ext[0], ext[1], u, M, v, M))
    if want is not None and pc == "floor_tile":
        check(abs(ext[2] - P(ov, "floor_thickness")) <= TOL, "floor tile %.2f thick" % ext[2])
    if want is not None and pc == "railing":
        check(abs(ext[0] - (u * M - G)) <= TOL and abs(ext[1] - P(ov, "railing_height")) <= TOL
              and abs(ext[2] - T) <= TOL, "railing %.2f x %.2f x %.2f" % ext)
    if want is not None and pc == "corner_post":
        check(abs(max(xs) - (M / 2 - G)) <= TOL and abs(max(ys) - (M / 2 - G)) <= TOL and abs(ext[2] - CH) <= TOL,
              "corner arms reach %.2f / %.2f == %g, one course (%.2f) tall" % (max(xs), max(ys), M / 2 - G, ext[2]))

    # no overlaps between colours
    if name in OVERLAP or name.startswith("maxcol"):
        whole = volume(read_stl("%s/%s_all.stl" % (OUT, name)))
        parts = sum(volume(read_stl("%s/%s_%s.stl" % (OUT, name, c[1:]))) for c in named)
        check(abs(parts - whole) <= 1e-4 * whole + 0.5,
              "colours do not overlap: sum of %d closed parts %.1f == whole %.1f mm3" % (len(named), parts, whole))

# ---- phase 3: fit probes. Each is an intersection that must be empty (or
# must not be). Built by including the model with piece="none".
Mw, CHd, Td, C = D["module_size"], D["course_height"], D["wall_thickness"], D["clip_clearance"]
Lw = Mw - G
SHIFT = C + 0.05
SHIFT_45 = C * math.sqrt(2) + 0.05     # across a 45-degree flare the play is C x sqrt(2)
KEY = "linear_extrude(KT) key2d(0);"
probes = {
    # two 1-unit walls at exactly module pitch
    "inline": "intersection() { wall_piece(\"wall\", 1); translate([%g, 0, 0]) wall_piece(\"wall\", 1); }" % Mw,
    "inline-key": "intersection() { union() { wall_piece(\"wall\", 1); translate([%g, 0, 0]) wall_piece(\"wall\", 1); } "
                  "translate([%g, %g, 0]) %s }" % (Mw, Lw + G / 2, CHd / 4, KEY),
    "inline-key-x": "intersection() { union() { wall_piece(\"wall\", 1); translate([%g, 0, 0]) wall_piece(\"wall\", 1); } "
                    "translate([%g, %g, 0]) %s }" % (Mw, Lw + G / 2 + SHIFT, CHd / 4, KEY),
    "inline-key-y": "intersection() { union() { wall_piece(\"wall\", 1); translate([%g, 0, 0]) wall_piece(\"wall\", 1); } "
                    "translate([%g, %g, 0]) %s }" % (Mw, Lw + G / 2, CHd / 4 + SHIFT_45, KEY),
    "inline-key-z": "intersection() { union() { wall_piece(\"wall\", 1); translate([%g, 0, 0]) wall_piece(\"wall\", 1); } "
                    "translate([%g, %g, %g]) %s }" % (Mw, Lw + G / 2, CHd / 4, SHIFT, KEY),
    # course on course, key across the joint
    "course": "intersection() { wall_piece(\"wall\", 2); translate([0, %g, 0]) wall_piece(\"window\", 2); }" % CHd,
    "course-key": "intersection() { union() { wall_piece(\"wall\", 2); translate([0, %g, 0]) wall_piece(\"window\", 2); } "
                  "translate([%g, %g, 0]) rotate(90) %s }" % (CHd, Mw / 2 - G / 2, CHd, KEY),
    "course-key-x": "intersection() { union() { wall_piece(\"wall\", 2); translate([0, %g, 0]) wall_piece(\"window\", 2); } "
                    "translate([%g, %g, 0]) rotate(90) %s }" % (CHd, Mw / 2 - G / 2, CHd + SHIFT, KEY),
    # corner post and the left wall of the room preview, key in their joint
    "corner": "intersection() { translate([0, %g, 0]) rotate([0, 0, -90]) corner_piece(); "
              "translate([T, %g, 0]) rotate([90, 0, -90]) wall_piece(\"wall\", 1.5); }" % (2 * Mw - G, 1.5 * Mw - G),
    "corner-key": "intersection() { union() { translate([0, %g, 0]) rotate([0, 0, -90]) corner_piece(); "
                  "translate([T, %g, 0]) rotate([90, 0, -90]) wall_piece(\"wall\", 1.5); } "
                  "translate([T, %g, 0]) rotate([90, 0, -90]) translate([%g, %g, 0]) %s }"
                  % (2 * Mw - G, 1.5 * Mw - G, 1.5 * Mw - G, -G / 2, CHd / 4, KEY),
    "corner-key-x": "intersection() { union() { translate([0, %g, 0]) rotate([0, 0, -90]) corner_piece(); "
                    "translate([T, %g, 0]) rotate([90, 0, -90]) wall_piece(\"wall\", 1.5); } "
                    "translate([T, %g, 0]) rotate([90, 0, -90]) translate([%g, %g, 0]) %s }"
                    % (2 * Mw - G, 1.5 * Mw - G, 1.5 * Mw - G, -G / 2 - SHIFT, CHd / 4, KEY),
    # wall standing on the floor tile's edge, peg through both
    "floor-wall": "intersection() { floor_tile(2, 2); translate([%g, %g, FT]) rotate([90, 0, 180]) wall_piece(\"wall\", 1); }"
                  % (Mw / 2 + Lw, 2 * Mw - G - Td),
    "floor-peg": "intersection() { union() { floor_tile(2, 2); translate([%g, %g, FT]) rotate([90, 0, 180]) wall_piece(\"wall\", 1); } "
                 "translate([%g, %g, 0]) cylinder(d = PEG, h = PL, $fn = 8); }"
                 % (Mw / 2 + Lw, 2 * Mw - G - Td, 3 * Mw / 4 - G / 2, 2 * Mw - G - Td / 2),
    "floor-peg-x": "intersection() { union() { floor_tile(2, 2); translate([%g, %g, FT]) rotate([90, 0, 180]) wall_piece(\"wall\", 1); } "
                   "translate([%g, %g, 0]) cylinder(d = PEG, h = PL, $fn = 8); }"
                   % (Mw / 2 + Lw, 2 * Mw - G - Td, 3 * Mw / 4 - G / 2 + SHIFT + 0.2, 2 * Mw - G - Td / 2),
    # two floor tiles side by side, key underneath
    "tiles": "intersection() { floor_tile(1, 2); translate([%g, 0, 0]) floor_tile(1, 2); }" % Mw,
    "tiles-key": "intersection() { union() { floor_tile(1, 2); translate([%g, 0, 0]) floor_tile(1, 2); } "
                 "translate([%g, %g, 0]) %s }" % (Mw, Lw + G / 2, Mw / 2 - G / 2, KEY),
    "tiles-key-x": "intersection() { union() { floor_tile(1, 2); translate([%g, 0, 0]) floor_tile(1, 2); } "
                   "translate([%g, %g, 0]) %s }" % (Mw, Lw + G / 2 + SHIFT, Mw / 2 - G / 2, KEY),
    # the two stair halves joined, keys in both stringers
    "stairs": "intersection() { stair_half(true); translate([SX, SN / 2 * SR - SB, 0]) stair_half(false); }",
    "stairs-key": "intersection() { union() { stair_half(true); translate([SX, SN / 2 * SR - SB, 0]) stair_half(false); } "
                  "union() { translate([SX, SN / 2 * SR - SB / 2, 0]) %s translate([SX, SN / 2 * SR - SB / 2, stair_width - KT]) %s } }" % (KEY, KEY),
    # door walls stacked with both leaves hung and both pins dropped in
    "door-leaves": "let (u = 1) intersection() { union() { wall_piece(\"door_lower\", u); translate([0, CH, 0]) wall_piece(\"door_upper\", u); } "
                   "union() { for (h = [\"lower\", \"upper\"]) translate([0, 0, (T - LT) / 2]) one_leaf(u, h, dr_axes(u)[0]); "
                   "let (a = dr_axes(u)[0], L = hinge_pin_lengths()) { "
                   "translate([a, CH - TAB - 5.5, T / 2]) rotate([-90, 0, 0]) cylinder(d = HP, h = L[0], $fn = 8); "
                   "translate([a, 2 * CH - 0.4 - L[1], T / 2]) rotate([-90, 0, 0]) cylinder(d = HP, h = L[1], $fn = 8); } } }",
    # a 320 mm doll-sized box through the door, from the hinge block to the far jamb
    "door-clear": "let (u = 1, a = dr_axes(u)[0]) intersection() { union() { wall_piece(\"door_lower\", u); "
                  "translate([0, CH, 0]) wall_piece(\"door_upper\", u); } "
                  "translate([a + 3.01, THR + 0.01, -5]) cube([dr_cx(u) + dr_w(u) / 2 - a - 3.02, 320, T + 20]); }",
    "door-clear-french": "let (u = 1.5, a = dr_axes(u)[0], b = dr_axes(u)[1]) intersection() { union() { wall_piece(\"door_lower\", u); "
                         "translate([0, CH, 0]) wall_piece(\"door_upper\", u); } "
                         "translate([a + 3.01, THR + 0.01, -5]) cube([b - a - 6.02, 320, T + 20]); }",
    "door-clear-arched": "let (u = 1, a = dr_axes(u)[0]) intersection() { union() { wall_piece(\"door_lower\", u); "
                         "translate([0, CH, 0]) wall_piece(\"door_upper\", u); } "
                         "translate([dr_cx(u) - 20, THR + 0.01, -5]) cube([40, 320, T + 20]); }",
    # positive controls: 1 mm taller than the clear height hits the header,
    # leaves pushed 1.2 mm towards the hinge jamb hit it
    "door-clear-331": "let (u = 1, a = dr_axes(u)[0]) intersection() { union() { wall_piece(\"door_lower\", u); "
                      "translate([0, CH, 0]) wall_piece(\"door_upper\", u); } "
                      "translate([a + 3.01, THR + 0.01, -5]) cube([dr_cx(u) + dr_w(u) / 2 - a - 3.02, %g, T + 20]); }" % (D["door_height"] + 1),
    "door-leaves-x": "let (u = 1) intersection() { union() { wall_piece(\"door_lower\", u); translate([0, CH, 0]) wall_piece(\"door_upper\", u); } "
                     "translate([-1.2, 0, 0]) for (h = [\"lower\", \"upper\"]) translate([0, 0, (T - LT) / 2]) one_leaf(u, h, dr_axes(u)[0]); }",
    # rail on the floor edge, in line with the next rail
    "railing": "intersection() { railing(1); translate([%g, 0, 0]) railing(1); }" % Mw,
}
PROBE_DEFS = {"door-clear-french": dict(door_style="french", door_width=140),
              "door-clear-arched": dict(door_style="arched")}
EXPECT_SOLID = {"door-clear-331", "door-leaves-x", "inline-key-x", "inline-key-y", "inline-key-z", "course-key-x", "corner-key-x",
                "floor-peg-x", "tiles-key-x"}
jobs = []
for name, body in probes.items():
    with open("%s/probe_%s.scad" % (OUT, name), "w") as f:
        f.write('include <../model.scad>\n%s\n' % body)
    jobs.append('openscad --backend=Manifold -D \'piece="none"\' %s -o %s/probe_%s.stl %s/probe_%s.scad >%s/probe_%s.log 2>&1 || true'
                % (defines(PROBE_DEFS.get(name, {})), OUT, name, OUT, name, OUT, name))
docker("\n".join(jobs))
print("\nfit probes (clearance %.2f, moved keys shifted %.2f mm):" % (C, SHIFT))
for name in probes:
    logt = open("%s/probe_%s.log" % (OUT, name)).read()
    empty = "top level object is empty" in logt.lower() or "empty" in logt.lower() and not os.path.exists("%s/probe_%s.stl" % (OUT, name))
    vol = 0.0
    if not empty and os.path.exists("%s/probe_%s.stl" % (OUT, name)):
        vol = abs(volume(read_stl("%s/probe_%s.stl" % (OUT, name))))
        empty = vol < 1e-6
    if name in EXPECT_SOLID:
        check(not empty and vol > 0, "%-18s moved by clearance+0.05 it binds (%.3f mm3 overlap)" % (name, vol))
    else:
        check(empty, "%-18s mated pieces do not intersect%s" % (name, "" if empty else " (%.3f mm3)" % vol))

# ---- door and window facts from the model's own numbers
H_DOOR = min(D["door_height"], 2 * CHd - 25 - THR)
check(H_DOOR >= 320, "default door is %g mm clear above the threshold across two courses (>= 320)" % H_DOOR)
LIN, PD_ = 1.6, PD
top_max = CHd - PD_ - 4 - LIN
sill = min(D["window_sill"], top_max - 30)
w_sill, w_top = CHd + sill, CHd + sill + min(D["window_height"], top_max - sill)
check(20 <= sill and w_sill <= 0.93 * 300 <= w_top,
      "default window (upper course) spans %g..%g mm above the floor, around a 30 cm doll's eye (279 mm)"
      % (w_sill, w_top))

if failures:
    print("\nFAILED: %d check(s)" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("\nOK")
PY

# The whole-house pipeline (spec 2026-09-27 §5.5), in the image that has the backend:
# the OpenSCAD image has no Python. CI sets SCADBUDDY_PIPELINE_IMAGE to the test image.
PIPELINE_IMAGE="${SCADBUDDY_PIPELINE_IMAGE:-}"
if [ -n "$PIPELINE_IMAGE" ]; then
    echo "==> pipeline: the house cases in pipeline/verify-inputs.json, in $PIPELINE_IMAGE"
    docker run --rm --label "scadbuddy-verify=${SCADBUDDY_VERIFY_LABEL:-local}" -v "$PWD:/template/dollhouse-kit:ro" -w /app/backend "$PIPELINE_IMAGE" uv run --frozen --no-sync python -m scadbuddy.workflows.verify_pipeline /template/dollhouse-kit --inputs /template/dollhouse-kit/pipeline/verify-inputs.json
else
    echo "==> SCADBUDDY_PIPELINE_IMAGE unset: pipeline check skipped (docker build --target test -t scadbuddy:test . and set it to run)"
fi
