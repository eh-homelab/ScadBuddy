#!/usr/bin/env bash
# Render models/tic-tac-toe with the defaults and each major variation, then
# check concrete facts: the number of colour parts, that no geometry is
# uncoloured, the bounding box the parameters imply, that everything sits on
# z=0, and — from one closed render per colour, the way ScadBuddy builds its
# parts — the z range of every part, the grid inlay's area, that every piece
# fits a well with `clearance` to spare, and, for the box, that the lid sits in
# its grooves without touching the box yet cannot lift out.
#
# The checking runs on the host with python3 and the standard library only:
# the OpenSCAD image has no Python.
set -euo pipefail

cd "$(dirname "$0")"

BASE_IMAGE="${SCADBUDDY_OPENSCAD_IMAGE:-openscad/openscad:dev}"
IMAGE="${SCADBUDDY_FONTS_IMAGE:-scadbuddy-verify:local}"
OUT="${OUT_DIR:-.verify}"

mkdir -p "$OUT"

# Same image as the other templates (no text here, so any OpenSCAD with
# Manifold would do); build it from the base image when it is missing.
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
echo "==> rendering with $IMAGE"

IMAGE="$IMAGE" OUT="$OUT" python3 - <<'PY'
import math, os, re, shlex, struct, subprocess, sys, zipfile
import xml.etree.ElementTree as ET
from collections import Counter

IMAGE, OUT = os.environ["IMAGE"], os.environ["OUT"]
NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
TOL = 0.01

D = dict(size=100, thickness=5, style="flat_board", piece_style="classic_xo",
         piece_thickness=5, clearance=0.4)
BOARD, GRID, XC, OC = "#4FC3F7", "#FFFFFF", "#E53935", "#FDD835"
INLAY, WELL_GAP, WALL, FLOOR, FLANGE, LAND = 1, 2, 4, 2, 1.5, 0.6
PIECE_GAP, PART_GAP, FN = 4, 8, 64

CASES = [
    ("defaults", {}),
    ("box", dict(style="box_with_storage")),
    ("animals", dict(piece_style="animals")),
    ("box-animals", dict(style="box_with_storage", piece_style="animals")),
    ("small-thin", dict(size=70, thickness=3, piece_thickness=3, clearance=0.2,
                        style="box_with_storage")),
    ("big-loose", dict(size=150, thickness=8, piece_thickness=8, clearance=0.8,
                       style="box_with_storage")),
    ("big-flat", dict(size=150, thickness=8, piece_thickness=8, clearance=0.8)),
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
    r = subprocess.run(["docker", "run", "--rm", "-v", os.getcwd() + ":/w", "-w", "/w",
                        IMAGE, "bash", "-ec", script], capture_output=True, text=True)
    if r.returncode or "WARNING" in r.stderr or "ERROR" in r.stderr:
        print(r.stderr)
        sys.exit("FAIL: OpenSCAD reported errors or warnings")


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
    out = []
    for i in range(n):
        f = struct.unpack("<12f", data[84 + 50 * i: 84 + 50 * i + 48])
        out.append([f[3:6], f[6:9], f[9:12]])
    return out


def bbox(points):
    xs, ys, zs = zip(*points)
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def volume(tris):
    v = 0.0
    for a, b, c in tris:
        v += (a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0])
              + a[2] * (b[0] * c[1] - b[1] * c[0]))
    return v / 6


def poly_circle_area(d):
    r = d / 2
    return FN / 2 * r * r * math.sin(2 * math.pi / FN)


# ---- geometry the parameters imply (mirrors model.scad)
def geo(ov):
    p = dict(D, **ov)
    S, T, cl = p["size"], p["thickness"], p["clearance"]
    box = p["style"] == "box_with_storage"
    lid_w = S - 2 * WALL - 2 * cl
    lid_l = S - WALL - cl
    play = lid_w if box else S
    bm = max(4, 0.06 * play)
    cell = (play - 2 * bm) / 3
    line_w = max(2, 0.03 * play)
    well_d = cell - line_w - 2 * WELL_GAP
    piece_d = well_d - 2 * cl
    inner_h = 2 * p["piece_thickness"] + 1.5
    lid_z = FLOOR + inner_h
    return dict(p=p, S=S, T=T, cl=cl, box=box, lid_w=lid_w, lid_l=lid_l, cell=cell,
                line_w=line_w, well_d=well_d, piece_d=piece_d, inner_h=inner_h,
                lid_z=lid_z, box_h=lid_z + T, well_depth=min(2, T - 1.2),
                lid_x=S / 2 + PART_GAP + lid_w / 2 + FLANGE)


# ---- renders
jobs = []
for name, ov in CASES:
    d = defines(ov)
    jobs.append("openscad --backend=Manifold %s -o %s/%s.3mf model.scad" % (d, OUT, name))
    jobs.append("openscad --backend=Manifold %s -o %s/%s_plate.stl model.scad" % (d, OUT, name))
    if ov.get("style") == "box_with_storage":
        # The lid rests on the groove floors; the dovetail leaves it
        # clearance * (1 + sqrt 2) of vertical play before the 45-degree faces
        # meet. Lift it 0.5 mm past that: it must collide.
        cl = ov.get("clearance", D["clearance"])
        for i, lift in enumerate((0, cl * (1 + math.sqrt(2)) + 0.5)):
            jobs.append("openscad --backend=Manifold %s -D assembled=true -D lid_lift=%.4f "
                        "-o %s/%s_asm%d.stl model.scad" % (d, lift, OUT, name, i))
docker("\n".join(jobs))
combined = {name: read_3mf("%s/%s.3mf" % (OUT, name)) for name, _ in CASES}

with open("%s/wrap.scad" % OUT, "w") as f:
    f.write('_sb_t = "";\nmodule color(c, alpha = 1) { if (c == _sb_t) children(); }\n'
            "include <../model.scad>\n")
jobs = []
for name, ov in CASES:
    for col in (BOARD, GRID, XC, OC):
        jobs.append("openscad --backend=Manifold %s -D '_sb_t=\"%s\"' -o %s/%s_%s.stl %s/wrap.scad"
                    % (defines(ov), col, OUT, name, col[1:], OUT))
docker("\n".join(jobs))

# ---- checks
failures = []


def check(ok, msg):
    print(("  PASS  " if ok else "  FAIL  ") + msg)
    if not ok:
        failures.append(msg)


def near(a, b, tol=TOL):
    return abs(a - b) <= tol


def r2(v):
    return tuple(round(x, 2) for x in v)


for name, ov in CASES:
    print("\n[%s] %s" % (name, " ".join("%s=%s" % kv for kv in ov.items()) or "defaults"))
    g = geo(ov)
    p, S, T = g["p"], g["S"], g["T"]
    mats, verts, tris = combined[name]
    used = Counter(t[3] for t in tris)
    named = {col for i, (n, col) in enumerate(mats) if n != "Default" and used.get(i)}
    check(named == {BOARD, GRID, XC, OC}, "four colour parts (got %s)" % sorted(named))
    check(used.get(0, 0) == 0, "Default material has no triangles (got %d)" % used.get(0, 0))

    lo, hi = bbox(verts)
    check(near(lo[2], 0), "sits on z=0 (min z %.3f)" % lo[2])
    pd, pitch = g["piece_d"], g["piece_d"] + PIECE_GAP
    right = g["lid_x"] + g["lid_w"] / 2 + FLANGE if g["box"] else S / 2
    right = max(right, -S / 2 + 5 * pitch - PIECE_GAP)
    top = max(g["box_h"] if g["box"] else T, p["piece_thickness"])
    ex_lo = (-S / 2, -S / 2 - PART_GAP - pd - pitch, 0)
    ex_hi = (right, S / 2, top)
    if p["piece_style"] == "classic_xo":
        check(all(near(a, b, 0.02) for a, b in zip(lo + hi, ex_lo + ex_hi)),
              "plate bbox %s .. %s == %s .. %s" % (r2(lo), r2(hi), r2(ex_lo), r2(ex_hi)))
    else:
        # star points and the heart's tip fall inside the piece circle
        check(near(lo[0], ex_lo[0], 0.5) and lo[1] >= ex_lo[1] - 0.01 and near(lo[2], 0)
              and hi[0] <= ex_hi[0] + 0.01 and near(hi[1], ex_hi[1]) and near(hi[2], ex_hi[2]),
              "plate bbox %s .. %s within %s .. %s" % (r2(lo), r2(hi), r2(ex_lo), r2(ex_hi)))

    parts = {c: read_stl("%s/%s_%s.stl" % (OUT, name, c[1:])) for c in (BOARD, GRID, XC, OC)}
    pb = {c: bbox([v for t in s for v in t]) for c, s in parts.items()}

    # Board / box.
    blo, bhi = pb[BOARD]
    zb = g["box_h"] if g["box"] else T
    check(near(blo[2], 0) and near(bhi[2], zb),
          "board part z %.3f .. %.3f (expected 0 .. %.2f)" % (blo[2], bhi[2], zb))
    glo, ghi = pb[GRID]
    check(near(glo[2], T - INLAY) and near(ghi[2], T),
          "grid inlay z %.3f .. %.3f (expected %.2f .. %.2f, flush with the face)"
          % (glo[2], ghi[2], T - INLAY, T))
    span = 3 * g["cell"]
    check(near(ghi[0] - glo[0], span, 0.02) and near(ghi[1] - glo[1], span, 0.02),
          "grid spans %.2f x %.2f (three %.2f mm cells)"
          % (ghi[0] - glo[0], ghi[1] - glo[1], g["cell"]))
    lw = g["line_w"]
    stadium = (span - lw) * lw + poly_circle_area(lw)
    area = volume(parts[GRID]) / INLAY
    exp_area = 4 * stadium - 4 * lw * lw
    check(abs(area - exp_area) <= 0.005 * exp_area,
          "grid inlay area %.1f mm^2 == four %.2f mm lines %.1f" % (area, lw, exp_area))
    if not g["box"]:
        r = min(8, S / 2 - 0.01)
        slab = ((S - 2 * r) ** 2 + 4 * (S - 2 * r) * r + poly_circle_area(2 * r)) * T
        cut = 9 * poly_circle_area(g["well_d"]) * g["well_depth"] + exp_area * INLAY
        vb = volume(parts[BOARD])
        check(abs(vb - (slab - cut)) <= 0.003 * slab,
              "board volume %.0f == slab %.0f - nine %.1f mm wells %.1f deep - grid (%.0f)"
              % (vb, slab, g["well_d"], g["well_depth"], slab - cut))

    # Pieces: two rows of five, each inside a circle of piece_d = well_d - 2*clearance.
    for col, row in ((XC, 0), (OC, 1)):
        plo, phi = pb[col]
        check(near(plo[2], 0) and near(phi[2], p["piece_thickness"]),
              "%s pieces z %.3f .. %.3f (expected 0 .. %.2f)"
              % (col, plo[2], phi[2], p["piece_thickness"]))
        cy = -S / 2 - PART_GAP - pd / 2 - row * pitch
        centres = [(-S / 2 + pd / 2 + i * pitch, cy) for i in range(5)]
        worst, counts = 0, Counter()
        for t in parts[col]:
            for v in t:
                k = min(range(5), key=lambda i: math.hypot(v[0] - centres[i][0], v[1] - centres[i][1]))
                counts[k] += 1
                worst = max(worst, math.hypot(v[0] - centres[k][0], v[1] - centres[k][1]))
        check(len(counts) == 5 and worst <= pd / 2 + 1e-3,
              "%s: 5 pieces, each within %.2f mm of its centre (well %.2f - 2 x %.2f clearance); widest %.3f"
              % (col, pd / 2, g["well_d"], g["cl"], worst))

    parts_v = sum(volume(s) for s in parts.values())
    plate_v = volume(read_stl("%s/%s_plate.stl" % (OUT, name)))
    check(abs(plate_v - parts_v) <= 1e-4 * parts_v,
          "no two pieces overlap on the plate: union %.0f == sum of parts %.0f mm^3" % (plate_v, parts_v))

    if g["box"]:
        inner = S - 2 * WALL
        per_layer = int(inner // pd) ** 2
        check(per_layer >= 5 and g["inner_h"] >= 2 * p["piece_thickness"],
              "storage %.1f x %.1f x %.1f holds %d pieces a layer, two layers of %.1f mm"
              % (inner, inner, g["inner_h"], per_layer, p["piece_thickness"]))
        asm0 = read_stl("%s/%s_asm0.stl" % (OUT, name))
        a0 = volume(asm0)
        a1 = volume(read_stl("%s/%s_asm1.stl" % (OUT, name)))
        check(abs(a0 - parts_v) <= 1e-4 * parts_v,
              "lid seats in its grooves without touching the box: %.0f == %.0f mm^3" % (a0, parts_v))
        check(a1 < parts_v - 10,
              "lid cannot lift out: raised %.2f mm it collides with the grooves (%.0f mm^3 overlap)"
              % (g["cl"] * (1 + math.sqrt(2)) + 0.5, parts_v - a1))
        check(FLANGE - g["cl"] > 0.5,
              "dovetail overlaps the groove by %.2f mm in plan" % (FLANGE - g["cl"]))
        alo, ahi = bbox([v for t in asm0 for v in t if v[1] > -S / 2 - 1])   # not the pieces
        check(near(ahi[2], g["box_h"]) and near(ahi[0] - alo[0], S) and near(ahi[1] - alo[1], S),
              "closed box is %.2f x %.2f x %.2f, lid face flush with the rim (expected %.2f cube x %.2f)"
              % (ahi[0] - alo[0], ahi[1] - alo[1], ahi[2], S, g["box_h"]))

if failures:
    print("\nFAILED: %d check(s)" % len(failures))
    sys.exit(1)
print("\nOK: %d cases" % len(CASES))
PY
