#!/usr/bin/env bash
# Render models/shape-sorter-puzzle with the defaults and each major
# variation, then check concrete facts:
#
#   - the plate has exactly the colour parts the parameters imply (the tray,
#     one colour per piece, the colour hints in the hole floors), nothing on
#     the Default material, sits on z=0, is as tall as a piece and its knob
#     (or the tray), and fits the 300 x 320 plate;
#   - the tray is exactly the size its grid of holes implies, and the print is
#     exactly the tray plus one separate piece per hole (or either alone);
#   - every piece fits its hole at the set clearance: sitting in its hole and
#     grown by clearance - 0.03 it touches nothing of the tray, and grown by
#     clearance + 0.03 it touches the wall of every hole, so the probe
#     measures the real gap;
#   - every hole, with its lead-in chamfer, stays inside its cell, and
#     neighbouring holes stay tray_wall apart;
#   - every knob sits wholly on its piece, with knob_margin to spare;
#   - rendered once per colour the way ScadBuddy builds its closed parts, the
#     colour parts do not overlap (the volume of the union equals the sum);
#   - the render log's NOTE lines say what was shrunk, skipped or split onto
#     a second plate, and a layout that does not fit falls back to the tray.
#
# The checking runs on the host with python3 and the standard library only:
# the OpenSCAD image has no Python.

set -euo pipefail

cd "$(dirname "$0")"

BASE_IMAGE="${SCADBUDDY_OPENSCAD_IMAGE:-openscad/openscad:dev}"
FONTS_IMAGE="${SCADBUDDY_FONTS_IMAGE:-scadbuddy-verify:local}"
OUT="${OUT_DIR:-.verify}"
FONT_FAMILY="DejaVu Sans"

mkdir -p "$OUT"

# Render in the ScadBuddy fonts image. Build it from the base image with the
# same Debian font packages when it is missing -- a face that is not installed
# falls back silently and the letters would render in the wrong face.
IMAGE="$FONTS_IMAGE"
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "==> building $IMAGE from $BASE_IMAGE with the image's font packages"
    docker build -q -t "$IMAGE" - <<DOCKERFILE
FROM $BASE_IMAGE
RUN apt-get update \\
 && apt-get install -y --no-install-recommends \\
      fonts-lobster fonts-lobstertwo fonts-dejavu fonts-noto-core \\
 && fc-cache -f \\
 && rm -rf /var/lib/apt/lists/*
DOCKERFILE
fi
if ! docker run --rm --label "scadbuddy-verify=${SCADBUDDY_VERIFY_LABEL:-local}" "$IMAGE" fc-list : family | grep -F "$FONT_FAMILY" >/dev/null; then
    echo "FAIL: $IMAGE has no '$FONT_FAMILY'" >&2
    exit 1
fi
echo "==> rendering with $IMAGE"

IMAGE="$IMAGE" OUT="$OUT" python3 - <<'PY'
import os, re, shlex, struct, subprocess, sys, zipfile
import xml.etree.ElementTree as ET
from collections import Counter

IMAGE, OUT = os.environ["IMAGE"], os.environ["OUT"]
NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
BED_X, BED_Y = 300, 320

# Defaults, mirrored from model.scad.
D = dict(set="shapes", letters="ANNA", piece_size=55, layout="both", knobs=True,
         knob_diameter=12, knob_height=10, piece_thickness=8, clearance=0.5, hole_depth=5,
         floor_thickness=2.4, tray_wall=7, color_hints=True)
TRAY = "#FFF3E0"
PIECE = ["#E53935", "#1E88E5", "#FDD835", "#43A047", "#FB8C00", "#8E24AA", "#EC407A", "#00ACC1",
         "#7CB342", "#5E35B1", "#6D4C41", "#F4511E"]
KNOB_MIN, KNOB_MARGIN = 6, 1.2

# (case, overrides, NOTE substrings the log must carry, NOTE substrings it must not)
CASES = [
    ("defaults", {}, [], ["NOTE"]),
    ("animals", dict(set="animals"), [], ["NOTE"]),
    ("numbers", dict(set="numbers"), [], ["NOTE"]),
    ("letters-anna", dict(set="letters"), [], ["NOTE"]),
    # Twelve letters with pieces too: they do not fit one plate with the
    # tray, so the tray prints alone and the log says how to get the pieces.
    ("letters-12-both", dict(set="letters", letters="MAXIMILIAN12"),
     ["do not fit one plate together"], []),
    ("letters-12-pieces", dict(set="letters", letters="MAXIMILIAN12", layout="pieces"), [], ["NOTE"]),
    # Lower case is made upper case; spaces and punctuation are skipped.
    ("letters-junk", dict(set="letters", letters="a-b c!"), ["3 other character(s) skipped"], []),
    ("letters-empty", dict(set="letters", letters=""), ["one A piece is made"], []),
    # Every glyph, at the smallest size with the biggest knob, as pieces: the
    # knob table must put a knob on every one of them.
    ("glyphs-1-small", dict(set="letters", letters="ABCDEFGHIJKL", layout="pieces",
                            piece_size=35, knob_diameter=16),
     ["narrower than 16", "1 piece(s) have no room for a knob"], []),
    ("glyphs-2-small", dict(set="letters", letters="MNOPQRSTUVWX", layout="pieces",
                            piece_size=35, knob_diameter=16), ["narrower than 16"], []),
    ("glyphs-3-small", dict(set="letters", letters="YZ0123456789", layout="pieces",
                            piece_size=35, knob_diameter=16), ["narrower than 16"], []),
    ("glyphs-1-tray", dict(set="letters", letters="ABCDEFGHIJKL", layout="tray"), [], []),
    ("glyphs-2-tray", dict(set="letters", letters="MNOPQRSTUVWX", layout="tray",
                           clearance=1.0, tray_wall=5), [], []),
    ("glyphs-3-tray", dict(set="letters", letters="YZ0123456789", layout="tray",
                           clearance=0.3), [], []),
    # Every glyph's colour hint at the smallest size: hint_inset is a fixed
    # 1.5 mm, so a thin stroke at piece_size 35 is where a hint would vanish.
    ("glyphs-1-tray-small", dict(set="letters", letters="ABCDEFGHIJKL", layout="tray",
                                 piece_size=35), [], []),
    ("glyphs-2-tray-small", dict(set="letters", letters="MNOPQRSTUVWX", layout="tray",
                                 piece_size=35), [], []),
    ("glyphs-3-tray-small", dict(set="letters", letters="YZ0123456789", layout="tray",
                                 piece_size=35), [], []),
    # Big numbers: the pieces shrink so the tray fits, then the tray prints alone.
    ("numbers-big", dict(set="numbers", piece_size=80),
     ["pieces are 71 mm (80 asked)", "do not fit one plate together"], []),
    # Loosest, deepest, no knobs, no hints; the thin pieces rise to the hole depth.
    ("shapes-plain-deep", dict(knobs=False, color_hints=False, hole_depth=10, piece_thickness=4,
                               clearance=1.0, floor_thickness=4, tray_wall=14), [], ["NOTE"]),
    # Pieces alone with no knobs: the plate is exactly piece_thickness tall, so
    # a piece's rounded top edge must reach it (the last rounding step once
    # came out empty and left every piece 0.27 mm short).
    ("shapes-pieces-plain", dict(layout="pieces", knobs=False), [], ["NOTE"]),
    ("animals-small-tight", dict(set="animals", piece_size=35, clearance=0.3, knob_diameter=16,
                                 knob_height=16, tray_wall=5), [], []),
    ("shapes-big", dict(piece_size=80, layout="both"), [], []),
]


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
                        IMAGE, "bash", "-ec", script], capture_output=True, text=True)
    if r.returncode or "WARNING" in r.stderr or "ERROR" in r.stderr:
        print(r.stderr[-4000:])
        sys.exit("FAIL: OpenSCAD reported errors or warnings")
    return r.stderr


def read_3mf(path):
    root = ET.fromstring(zipfile.ZipFile(path).read("3D/3dmodel.model"))
    mats = [(b.get("name"), (b.get("displaycolor") or "")[:7].upper())
            for b in root.iter(NS + "base")]
    verts = [(float(v.get("x")), float(v.get("y")), float(v.get("z")))
             for v in root.iter(NS + "vertex")]
    tris = [(int(t.get("v1")), int(t.get("v2")), int(t.get("v3")), int(t.get("p1") or 0))
            for t in root.iter(NS + "triangle")]
    return mats, verts, tris


def read_stl(path):
    data = open(path, "rb").read()
    if data[:5] == b"solid" and b"facet" in data[:300]:
        vs = [tuple(map(float, m.groups())) for m in re.finditer(
            rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data)]
        return [vs[i:i + 3] for i in range(0, len(vs), 3)]
    n = struct.unpack("<I", data[80:84])[0]
    tris = []
    for i in range(n):
        f = struct.unpack("<12f", data[84 + 50 * i: 84 + 50 * i + 48])
        tris.append([f[3:6], f[6:9], f[9:12]])
    return tris


def volume(tris):
    v = 0.0
    for a, b, c in tris:
        v += (a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0])
              + a[2] * (b[0] * c[1] - b[1] * c[0])) / 6
    return v


def real_points(stl):
    """Vertices of a probe render, minus the sentinel cube at x = -1000."""
    return [v for t in stl for v in t if v[0] > -900]


def components(tris):
    parent = {}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for t in tris:
        for v in t[:3]:
            parent.setdefault(v, v)
        for a, b in ((t[0], t[1]), (t[1], t[2])):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb
    return len({find(v) for v in parent})


def nums(s):
    return [float(x) for x in re.findall(r"-?[\d.]+(?:e-?\d+)?", s)]


with open("%s/wrap.scad" % OUT, "w") as f:
    f.write('_sb_t = "";\nmodule color(c, alpha = 1) { if (c == _sb_t) children(); }\n'
            "include <../model.scad>\n")

failures = []


def check(ok, msg):
    print(("  PASS  " if ok else "  FAIL  ") + msg)
    if not ok:
        failures.append(msg)


for name, ov, want_notes, bad_notes in CASES:
    p = dict(D, **ov)
    d = defines(ov)
    c = p["clearance"]
    lo, hi = round(c - 0.03, 3), round(c + 0.03, 3)
    log = docker("openscad --backend=Manifold %s -o %s/%s.3mf model.scad" % (d, OUT, name))
    m = re.search(r'SB_SORTER n=(\d+) size=(\S+) tray=\[(\S+), (\S+)\] grid=\[(\d+), (\d+)\] '
                  r'pieces=\[(\S+), (\S+)\] pgrid=\[(\d+), (\d+)\] both_fit=(\w+) '
                  r'tray_shown=(\w+) pieces_shown=(\w+) piece_h=(\S+) knobs=\[\[([^\]]*)\]\] '
                  r'holes=\[\[(.*)\]\]"', log)
    if not m:
        print(log[-2000:])
        sys.exit("FAIL: no SB_SORTER echo for %s" % name)
    n, S = int(m.group(1)), float(m.group(2))
    TW, TD = float(m.group(3)), float(m.group(4))
    TC, TR = int(m.group(5)), int(m.group(6))
    PW, PD = float(m.group(7)), float(m.group(8))
    both_fit = m.group(11) == "true"
    tray_shown, pieces_shown = m.group(12) == "true", m.group(13) == "true"
    PT = float(m.group(14))
    knobs = nums(m.group(15))
    hv = nums(m.group(16))
    holes = list(zip(hv[0::2], hv[1::2]))
    notes = re.findall(r'ECHO: "(NOTE: [^"]*)"', log)

    print("\n[%s] %s" % (name, " ".join("%s=%s" % kv for kv in ov.items()) or "defaults"))
    print("  %d pieces of %.0f mm, tray %.0f x %.0f (%d x %d), pieces %.0f x %.0f, knobs %s"
          % (n, S, TW, TD, TC, TR, PW, PD, knobs))
    for n_ in notes:
        print("        " + n_)
    for s in want_notes:
        check(any(s in n_ for n_ in notes), "log has NOTE '%s'" % s)
    for s in bad_notes:
        check(not any(s in n_ for n_ in notes), "log has no NOTE '%s'" % s)

    # -- what the set implies
    if p["set"] == "letters":
        word = [ch.upper() for ch in p["letters"] if ch.upper().isalnum() and ch.isascii()]
        want_n = len(word) or 1
    else:
        want_n = {"shapes": 6, "animals": 6, "numbers": 10}[p["set"]]
    check(n == want_n and len(holes) == n and len(knobs) == n, "%d pieces (want %d)" % (n, want_n))
    check(S <= p["piece_size"] and (S == p["piece_size"] or any("pieces are" in n_ for n_ in notes)),
          "pieces are piece_size, or smaller with a NOTE (%.0f)" % S)
    check(TW == TC * (S + p["tray_wall"]) + p["tray_wall"] and TD == TR * (S + p["tray_wall"]) + p["tray_wall"]
          and TC * TR >= n, "tray %.0f x %.0f is its %d x %d grid of holes" % (TW, TD, TC, TR))
    want_tray = p["layout"] != "pieces"
    want_pieces = p["layout"] == "pieces" or (p["layout"] == "both" and both_fit)
    check(tray_shown == want_tray and pieces_shown == want_pieces,
          "tray %s, pieces %s for layout %s" % (tray_shown, pieces_shown, p["layout"]))
    check(both_fit == (TD + 5 + PD <= BED_Y and max(TW, PW) <= BED_X),
          "tray and pieces fit together: %s (%.0f + 5 + %.0f deep)" % (both_fit, TD, PD))
    check(both_fit or p["layout"] != "both" or any("do not fit one plate" in n_ for n_ in notes),
          "a layout that does not fit says so")
    check(PT == max(p["piece_thickness"], p["hole_depth"]), "pieces are %.1f mm thick" % PT)
    if p["knobs"]:
        check(all(k == 0 or KNOB_MIN <= k <= p["knob_diameter"] for k in knobs),
              "every knob is %d..%s mm or absent" % (KNOB_MIN, p["knob_diameter"]))
        check(all(k > 0 for k in knobs) or any("no room for a knob" in n_ for n_ in notes),
              "every piece has a knob, or a NOTE says why not")
    else:
        check(not any(knobs), "no knobs")

    # -- plate
    mats, verts, tris = read_3mf("%s/%s.3mf" % (OUT, name))
    used = Counter(t[3] for t in tris)
    got = {col for i, (n_, col) in enumerate(mats) if n_ != "Default" and used.get(i)}
    want = set()
    if tray_shown:
        want.add(TRAY)
        if p["color_hints"]:
            want |= {PIECE[k % 12] for k in range(n)}
    if pieces_shown:
        want |= {PIECE[k % 12] for k in range(n)}
    check(got == want, "parts are %s (got %s)" % (sorted(want), sorted(got)))
    check(used.get(0, 0) == 0, "Default material has no triangles (got %d)" % used.get(0, 0))
    xs, ys, zs = zip(*verts)
    tray_h = p["floor_thickness"] + p["hole_depth"]
    top = max(tray_h if tray_shown else 0,
              (PT + (p["knob_height"] if any(knobs) else 0)) if pieces_shown else 0)
    check(abs(min(zs)) < 1e-3 and abs(max(zs) - top) < 0.02,
          "sits on z=0 and is %.2f mm tall (z %.3f .. %.3f)" % (top, min(zs), max(zs)))
    sx, sy = max(xs) - min(xs), max(ys) - min(ys)
    check(sx <= BED_X and sy <= BED_Y, "fits the %dx%d bed (%.1f x %.1f)" % (BED_X, BED_Y, sx, sy))
    if tray_shown and not pieces_shown:
        check(abs(sx - TW) < 0.01 and abs(sy - TD) < 0.01, "tray alone is %.0f x %.0f" % (TW, TD))
    ncomp = components(tris)
    want_comp = (1 if tray_shown else 0) + (n if pieces_shown else 0)
    check(ncomp == want_comp, "%d separate pieces == %s" % (
        ncomp, " + ".join((["tray"] if tray_shown else []) + (["%d pieces" % n] if pieces_shown else []))))

    # -- probes
    jobs = []
    for tag, extra in (("fit_lo", "-D probe_fit=%s" % lo), ("fit_hi", "-D probe_fit=%s" % hi),
                       ("cell", "-D probe_cell=true"), ("knob", "-D probe_knob=true"),
                       ("all", "")):
        jobs.append("openscad --backend=Manifold %s %s -o %s/%s_%s.stl model.scad"
                    % (d, extra, OUT, name, tag))
    for col in [TRAY] + PIECE:
        # An empty colour writes no file, so drop a previous run's.
        if os.path.exists("%s/%s_%s.stl" % (OUT, name, col[1:])):
            os.remove("%s/%s_%s.stl" % (OUT, name, col[1:]))
        jobs.append("openscad --backend=Manifold %s -D '_sb_t=\"%s\"' -o %s/%s_%s.stl %s/wrap.scad"
                    " 2>&1 | grep -v 'Current top level object is empty' >&2 || true"
                    % (d, col, OUT, name, col[1:], OUT))
    docker("\n".join(jobs))

    def holes_hit(pts):
        return {min(range(n), key=lambda j: (holes[j][0] - x) ** 2 + (holes[j][1] - y) ** 2)
                for x, y, _ in pts}

    pts = real_points(read_stl("%s/%s_fit_lo.stl" % (OUT, name)))
    check(not pts, "every piece sits in its hole >= %.2f mm from the tray (%d overlap vertices)"
          % (lo, len(pts)))
    hit = holes_hit(real_points(read_stl("%s/%s_fit_hi.stl" % (OUT, name))))
    check(len(hit) == n, "grown by %.2f every piece touches its hole's wall (%d of %d), so the probe measures"
          % (hi, len(hit), n))
    pts = real_points(read_stl("%s/%s_cell.stl" % (OUT, name)))
    check(not pts, "every hole and lead-in stays in its cell and tray_wall from the next (%d vertices)"
          % len(pts))
    pts = real_points(read_stl("%s/%s_knob.stl" % (OUT, name)))
    check(not pts, "every knob sits on its piece with %.1f mm to spare (%d vertices off)"
          % (KNOB_MARGIN - 0.1, len(pts)))

    if tray_shown:
        tz = [v[2] for t in read_stl("%s/%s_%s.stl" % (OUT, name, TRAY[1:])) for v in t]
        check(abs(max(tz) - tray_h) < 1e-3, "tray is %.1f mm tall, full height to its top edge (%.3f)"
              % (tray_h, max(tz)))
    if tray_shown and not pieces_shown and p["color_hints"] and n <= len(PIECE):
        # One colour per hole, so each colour's part is exactly that hole's hint.
        def hint_holes(k):
            f = "%s/%s_%s.stl" % (OUT, name, PIECE[k][1:])
            return holes_hit(real_points(read_stl(f))) if os.path.exists(f) else set()
        bad = [k for k in range(n) if hint_holes(k) != {k}]
        check(not bad, "every hole has its own colour hint (missing or misplaced: %s)" % bad)
    total = sum(volume(read_stl("%s/%s_%s.stl" % (OUT, name, col[1:]))) for col in want)
    union = volume(read_stl("%s/%s_all.stl" % (OUT, name)))
    check(abs(union - total) <= 0.001 * union,
          "colour parts do not overlap: union %.1f == sum of parts %.1f mm^3" % (union, total))

if failures:
    print("\nFAILED: %d check(s)" % len(failures))
    sys.exit(1)
print("\nOK: %d cases" % len(CASES))
PY
