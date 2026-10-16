#!/usr/bin/env bash
# Render models/fidget-slider-board with the defaults and each major variation,
# then check concrete facts about the print-in-place beads and the colours:
#
#   - the plate has exactly the colour parts the parameters imply (the board,
#     the bead colours the pattern uses, the name), nothing on the Default
#     material, sits on z=0, is as tall as a bead's knob, and its longest side
#     is board_size;
#   - the print is exactly board + one separate piece per bead (a bead fused
#     to its track, or to its neighbour, would merge two);
#   - measured on the rendered geometry, every bead is at least `clearance`
#     from the board and from the next bead (grown by clearance - 0.03 it
#     touches nothing), and the probe is real (grown by clearance + 0.03 it
#     touches the track of every bead);
#   - every bead is captured: moved 1.5 mm up, 1.5 mm down or 1.5 mm sideways
#     it hits the board, at every bead;
#   - every row can slide: each bead swept along its track by the free room,
#     grown by clearance - 0.03, touches nothing of the board;
#   - every track, knob and the name stay at least 3 mm inside the outline;
#   - the name is never cut: the inlay is exactly as wide and as tall as the
#     same text rendered on its own at the size the log reports (a box that
#     clipped it, as the name's band once did, makes it smaller);
#   - rendered once per colour the way ScadBuddy builds its closed parts, the
#     colour parts do not overlap (the volume of the union equals the sum);
#   - the render log's NOTE lines say what was dropped or shrunk.
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
# falls back silently and the name would render in the wrong face.
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
WALL = 3

# Defaults, mirrored from model.scad.
D = dict(shape="heart", board_size=180, rows=4, beads_per_row=4, bead_size=15, free_places=1,
         clearance=0.4, color_pattern="diagonal", bead_colors=6, name="MIA", name_size=13)
BOARD = "#F8BBD0"
BEADS = ["#E53935", "#FB8C00", "#FDD835", "#43A047", "#1E88E5", "#8E24AA", "#00ACC1", "#EC407A"]
NAME = "#6A1B9A"
FONT = "DejaVu Sans:style=Bold"

# (case, overrides, NOTE substrings the log must carry, NOTE substrings it must not)
CASES = [
    ("defaults", {}, [], ["NOTE"]),
    ("star-rows", dict(shape="star", rows=6, beads_per_row=8, color_pattern="rows",
                       bead_colors=8, name=""), ["narrower there"], []),
    ("dinosaur-name", dict(shape="dinosaur", board_size=240, rows=3, beads_per_row=6,
                           name="REX"), [], ["name does not fit"]),
    ("rrect-columns", dict(shape="rounded_rectangle", rows=5, beads_per_row=6,
                           color_pattern="columns", bead_colors=4, name="Sam"), [],
     ["does not fit", "holds"]),
    ("circle-big-loose", dict(shape="circle", bead_size=20, clearance=0.6, free_places=3,
                              rows=5, beads_per_row=3, name=""), [], []),
    # One tiny bead at the tightest clearance with the least free room.
    ("rrect-one-tight", dict(shape="rounded_rectangle", board_size=100, rows=1,
                             beads_per_row=1, bead_size=12, clearance=0.25,
                             free_places=0.5, bead_colors=1, name=""), [], ["does not fit"]),
    # Too much asked of the smallest heart: rows are dropped, the free room is
    # cut so one bead fits, and the name is left out.
    ("heart-small-overfull", dict(board_size=100, bead_size=20, free_places=3, rows=10,
                                  beads_per_row=10, name="MAXIMILIAN12", name_size=30),
     ["row 10 of 10 does not fit", "free room cut to 2 bead places", "name does not fit"], []),
    # The smallest dinosaur with the biggest beads: one bead, free room cut.
    ("dinosaur-min", dict(shape="dinosaur", board_size=100, bead_size=20, free_places=3,
                          name=""), ["free room cut to 1 bead places"], []),
    # A long name on a narrow shape shrinks rather than overflowing.
    ("star-long-name", dict(shape="star", board_size=150, rows=2, beads_per_row=3, name="ABCDEFGH"),
     ["name letters are"], ["name does not fit"]),
    # Round, wide capitals on the smallest board: shrunk to fit, never cut (#512).
    ("heart-oscar", dict(board_size=100, rows=1, name="OSCAR"),
     ["name letters are"], ["name does not fit"]),
    # Ascenders and descenders at the full size: the band is as tall as the ink (#512).
    ("heart-peggy", dict(board_size=280, name="Peggy"), [], ["NOTE"]),
    # Wide letters at the most a name holds.
    ("rrect-wide-12", dict(shape="rounded_rectangle", board_size=200, rows=2, name="WMWMWMWMWMWM"),
     ["name letters are"], ["name does not fit"]),
]
# Expected bead counts, read off a render and checked by hand against the
# shapes (row lengths are listed in each case's log line).
EXPECT_BEADS = {"defaults": 16, "dinosaur-min": 1, "rrect-one-tight": 1, "heart-small-overfull": 1}


def scad(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return '"%s"' % v
    if isinstance(v, list):
        return "[%s]" % ", ".join(scad(x) for x in v)
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
    probes = [("gap_lo", "-D probe_gap=%s" % lo), ("gap_hi", "-D probe_gap=%s" % hi),
              ("up", "-D 'probe_move=[0,0,1.5]'"), ("down", "-D 'probe_move=[0,0,-1.5]'"),
              ("side", "-D 'probe_move=[0,1.5,0]'"),
              ("slide", "-D probe_slide=true -D probe_gap=%s" % lo),
              ("wall", "-D probe_wall=%s" % (WALL - 0.05))]
    jobs = ["openscad --backend=Manifold %s -o %s/%s.3mf model.scad" % (d, OUT, name),
            "openscad --backend=Manifold %s -o %s/%s_all.stl model.scad" % (d, OUT, name)]
    for tag, extra in probes:
        jobs.append("openscad --backend=Manifold %s %s -o %s/%s_%s.stl model.scad"
                    % (d, extra, OUT, name, tag))
    for col in [BOARD, NAME] + BEADS:
        jobs.append("openscad --backend=Manifold %s -D '_sb_t=\"%s\"' -o %s/%s_%s.stl %s/wrap.scad"
                    " 2>&1 | grep -v 'Current top level object is empty' >&2 || true"
                    % (d, col, OUT, name, col[1:], OUT))
    # Run the 3MF render on its own first, so its log is exactly one model's.
    log = docker(jobs[0])
    docker("\n".join(jobs[1:]))

    print("\n[%s] %s" % (name, " ".join("%s=%s" % kv for kv in ov.items()) or "defaults"))
    m = re.search(r'SB_FIDGET beads=(\d+) rows=\[\[([^\]]*)\]\] row_y=\[\[([^\]]*)\]\] '
                  r'x0=\[\[([^\]]*)\]\] pitch=(\S+) free_places=(\S+) free=(\S+) '
                  r'name_size=(\S+) .*?name_iv=.*? name_y=\S+ size=\[(\S+), (\S+)\] top=(\S+)"', log)
    if not m:
        print(log[-2000:])
        sys.exit("FAIL: no SB_FIDGET echo for %s" % name)
    n_beads = int(m.group(1))
    rows = [int(x) for x in nums(m.group(2))]
    row_y, x0 = nums(m.group(3)), nums(m.group(4))
    pitch, free = float(m.group(5)), float(m.group(7))
    name_size = float(m.group(8))
    size_x, size_y, top = float(m.group(9)), float(m.group(10)), float(m.group(11))
    notes = re.findall(r'ECHO: "(NOTE: [^"]*)"', log)
    print("  beads %d in rows %s, free room %.1f mm, name %.1f mm" % (n_beads, rows, free, name_size))
    for n_ in notes:
        print("        " + n_)

    D_ = p["bead_size"]
    centres = [(x + 1 + (D_ - 2) / 2 + i * pitch, y)
               for x, y, n in zip(x0, row_y, rows) for i in range(n)]
    check(len(centres) == n_beads == sum(rows), "%d beads == sum of rows %s" % (n_beads, rows))
    if name in EXPECT_BEADS:
        check(n_beads == EXPECT_BEADS[name], "%d beads (expected %d)" % (n_beads, EXPECT_BEADS[name]))
    check(all(0 < n <= p["beads_per_row"] for n in rows) and len(rows) <= p["rows"],
          "at most %d rows of at most %d beads" % (p["rows"], p["beads_per_row"]))
    check(abs(free - p["free_places"] * pitch) < 1e-6 or "free room cut" in " ".join(notes),
          "free room is free_places x pitch unless a NOTE says it was cut")
    check(free >= 0.5 * pitch - 1e-6, "every row keeps at least half a bead place free (%.1f mm)" % free)
    for s in want_notes:
        check(any(s in n_ for n_ in notes), "log has NOTE '%s'" % s)
    for s in bad_notes:
        check(not any(s in n_ for n_ in notes), "log has no NOTE '%s'" % s)

    # -- plate
    mats, verts, tris = read_3mf("%s/%s.3mf" % (OUT, name))
    used = Counter(t[3] for t in tris)
    got = {col for i, (n_, col) in enumerate(mats) if n_ != "Default" and used.get(i)}
    pat = p["color_pattern"]
    want = {BOARD} | {BEADS[((k if pat == "rows" else i if pat == "columns" else k + i)
                             % p["bead_colors"])]
                      for k, n in enumerate(rows) for i in range(n)}
    if name_size > 0:
        want.add(NAME)
    check(got == want, "parts are %s (got %s)" % (sorted(want), sorted(got)))
    check(used.get(0, 0) == 0, "Default material has no triangles (got %d)" % used.get(0, 0))
    check((name_size > 0) == bool(p["name"]) or any("name does not fit" in n_ for n_ in notes),
          "the name is there, or a NOTE says why not")
    xs, ys, zs = zip(*verts)
    check(abs(min(zs)) < 1e-3 and abs(max(zs) - top) < 0.05,
          "sits on z=0 and is %.2f mm tall (z %.3f .. %.3f)" % (top, min(zs), max(zs)))
    sx, sy = max(xs) - min(xs), max(ys) - min(ys)
    check(abs(max(sx, sy) - p["board_size"]) < 0.01 and abs(sx - size_x) < 0.01
          and abs(sy - size_y) < 0.01,
          "longest side is board_size %s (%.2f x %.2f)" % (p["board_size"], sx, sy))
    check(sx <= BED_X and sy <= BED_Y, "fits the %dx%d bed" % (BED_X, BED_Y))
    ncomp = components(tris)
    check(ncomp == 1 + n_beads, "%d separate pieces == board + %d beads" % (ncomp, n_beads))

    def beads_hit(pts):
        return {min(range(len(centres)), key=lambda j: (centres[j][0] - x) ** 2
                    + (centres[j][1] - y) ** 2) for x, y, _ in pts}

    # -- clearances
    lo_pts = real_points(read_stl("%s/%s_gap_lo.stl" % (OUT, name)))
    check(not lo_pts, "every bead >= %.2f mm from the board and the next bead (%d overlap vertices)"
          % (lo, len(lo_pts)))
    hit = beads_hit(real_points(read_stl("%s/%s_gap_hi.stl" % (OUT, name))))
    check(len(hit) == n_beads, "grown by %.2f every bead touches its track (%d of %d), so the probe measures"
          % (hi, len(hit), n_beads))
    for tag, what in (("up", "up 1.5 mm"), ("down", "down 1.5 mm"), ("side", "sideways 1.5 mm")):
        hit = beads_hit(real_points(read_stl("%s/%s_%s.stl" % (OUT, name, tag))))
        check(len(hit) == n_beads, "every bead captured moving %s (%d of %d)" % (what, len(hit), n_beads))
    sl = real_points(read_stl("%s/%s_slide.stl" % (OUT, name)))
    check(not sl, "every bead slides %.1f mm along its track clear of the board (%d overlap vertices)"
          % (free, len(sl)))
    if name_size > 0:
        nx = [verts[i][0] for t in tris if mats[t[3]][1] == NAME for i in t[:3]]
        ny = [verts[i][1] for t in tris if mats[t[3]][1] == NAME for i in t[:3]]
        with open("%s/%s_text.scad" % (OUT, name), "w") as f:
            f.write("linear_extrude(1) text(%s, size = %r, font = %s);\n"
                    % (scad(p["name"]), name_size, scad(FONT)))
        docker("openscad --backend=Manifold -o %s/%s_text.stl %s/%s_text.scad" % (OUT, name, OUT, name))
        tv = [v for t in read_stl("%s/%s_text.stl" % (OUT, name)) for v in t]
        tw = max(v[0] for v in tv) - min(v[0] for v in tv)
        th = max(v[1] for v in tv) - min(v[1] for v in tv)
        iw, ih = max(nx) - min(nx), max(ny) - min(ny)
        check(abs(iw - tw) < 0.05 and abs(ih - th) < 0.05,
              "the name is not cut: inlay %.2f x %.2f mm == the text alone %.2f x %.2f mm"
              % (iw, ih, tw, th))
    wl = real_points(read_stl("%s/%s_wall.stl" % (OUT, name)))
    check(not wl, "tracks, knobs and name are >= %.2f mm inside the outline (%d vertices outside)"
          % (WALL - 0.05, len(wl)))

    # -- closed colour parts do not overlap
    bz = [v[2] for t in read_stl("%s/%s_%s.stl" % (OUT, name, BOARD[1:])) for v in t]
    check(abs(max(bz) - 8) < 1e-3, "board is 8 mm thick, full height to its top edge (%.3f)" % max(bz))
    total = sum(volume(read_stl("%s/%s_%s.stl" % (OUT, name, col[1:]))) for col in want)
    union = volume(read_stl("%s/%s_all.stl" % (OUT, name)))
    check(abs(union - total) <= 0.001 * union,
          "colour parts do not overlap: union %.1f == sum of parts %.1f mm^3" % (union, total))

if failures:
    print("\nFAILED: %d check(s)" % len(failures))
    sys.exit(1)
print("\nOK: %d cases" % len(CASES))
PY
