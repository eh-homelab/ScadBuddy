#!/usr/bin/env bash
# Render models/spinning-top with the defaults and each major variation, then
# check concrete facts: the number of colour parts, that no geometry is
# uncoloured, the plate bounding box the parameters imply, that everything sits
# on z=0 — and, from one closed render per colour of the ASSEMBLED top (the way
# ScadBuddy builds its parts), that the parts do not overlap, sit at the heights
# the parameters imply, and that every part's centre of mass, and the whole
# top's, lies on the spin axis within 0.05 mm.
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
import math, os, re, shlex, struct, subprocess, sys, zipfile
import xml.etree.ElementTree as ET
from collections import Counter

IMAGE, OUT = os.environ["IMAGE"], os.environ["OUT"]
NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
TOL = 0.01
AXIS_TOL = 0.05          # centre of mass distance from the spin axis, mm

# Defaults, mirrored from model.scad.
D = dict(diameter=50, style="classic_cone", stem_length=20, stem_d=6, tip="ball",
         fit_clearance=0.15, pattern="spiral", name="")
BODY, PATTERN, STEM = "#1E88E5", "#FFEB3B", "#E53935"
PART_GAP, COLLAR, INLAY = 6, 1.2, 0.6

CASES = [
    ("defaults", {}),
    ("ufo-dots-point", dict(style="ufo_disc", pattern="dots", tip="point")),
    ("flower-rays-name", dict(style="flower", pattern="rays", name="LEO")),
    ("spiral-name", dict(name="MAYA")),
    ("classic-none", dict(pattern="none")),
    ("name-only", dict(pattern="none", name="Samantha")),
    ("small-fat-longname", dict(diameter=30, stem_d=10, stem_length=10,
                                style="ufo_disc", name="Maximilian12")),
    ("name-narrow-letters", dict(pattern="none", name="Oliver")),
    ("big-thin-tight", dict(diameter=80, stem_d=4, stem_length=35, tip="point",
                            fit_clearance=0, style="flower", pattern="dots")),
    ("ufo-rays-loose", dict(style="ufo_disc", pattern="rays", fit_clearance=0.4)),
]


# DejaVu Sans Bold advance widths at size 10, read from model.scad.
ADV10 = [float(x) for x in re.search(r"ADV10 = \[(.*?)\];", open("model.scad").read(),
                                      re.S).group(1).split(",")]


def letter_gaps(tris, r_mid):
    """Arc-length gaps between neighbouring letters of the copy on the +y side.

    Letters are the angular extents of the pattern's triangles, merged where
    they overlap (the dot of an i joins its stem)."""
    spans = []
    for t in tris:
        if min(v[1] for v in t) <= 0:
            continue
        a = [math.degrees(math.atan2(v[1], v[0])) for v in t]
        spans.append([min(a), max(a)])
    spans.sort()
    merged = []
    for a0, a1 in spans:
        if merged and a0 <= merged[-1][1] + 1e-6:
            merged[-1][1] = max(merged[-1][1], a1)
        else:
            merged.append([a0, a1])
    return [math.radians(b[0] - a[1]) * r_mid for a, b in zip(merged, merged[1:])]


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
    tris = []
    for i in range(n):
        f = struct.unpack("<12f", data[84 + 50 * i: 84 + 50 * i + 48])
        tris.append([f[3:6], f[6:9], f[9:12]])
    return tris


def bbox(points):
    xs, ys, zs = zip(*points)
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def mass_props(tris):
    """Volume and centroid of a closed mesh (signed tetrahedra from the origin)."""
    v = cx = cy = cz = 0.0
    for a, b, c in tris:
        d = (a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0])
             + a[2] * (b[0] * c[1] - b[1] * c[0])) / 6
        v += d
        cx += d * (a[0] + b[0] + c[0]) / 4
        cy += d * (a[1] + b[1] + c[1]) / 4
        cz += d * (a[2] + b[2] + c[2]) / 4
    return v, (cx / v, cy / v, cz / v)


# ---- geometry the parameters imply, mirrored from model.scad
def geom(ov):
    p = dict(D, **ov)
    R = p["diameter"] / 2
    r_s = p["stem_d"] / 2
    r_c = r_s + COLLAR
    tip_r = min(r_c, max(2, p["diameter"] * 0.06)) if p["tip"] == "ball" else 0.6
    z_c = max(r_c * 1.5, 2 * tip_r + 1)
    ufo = p["style"] == "ufo_disc"
    rim_t = max(3.5, p["diameter"] * 0.08) if ufo else max(4, p["diameter"] * 0.1)
    under_h = max(p["diameter"] * (0.16 if ufo else 0.34), z_c + COLLAR + 3)
    H = under_h + rim_t
    b = 0 if ufo else 1
    flower_y = 0.7 * R * math.sin(math.radians(60)) + 0.3 * R
    return dict(p=p, R=R, r_c=r_c, z_c=z_c, rim_t=rim_t, under_h=under_h, H=H, b=b,
                half_y=flower_y if p["style"] == "flower" else R,
                spindle_len=H + p["stem_length"])


def decor_expected(ov):
    """Whether the pattern colour exists at all (mirrors pattern_ok / name_ok)."""
    g = geom(ov)
    p, R = g["p"], g["R"]
    r_h = p["stem_d"] / 2 + p["fit_clearance"]
    face_r = (0.78 * R if p["style"] == "flower" else R - 0.8) - 1.5
    r0 = r_h + 2
    name_ok = False
    r1 = face_r
    if p["name"]:
        name_r = face_r - 3.5 / 2
        adv = sum(ADV10[ord(c) - 32] if 32 <= ord(c) <= 126 else ADV10[78 - 32]
                  for c in p["name"])
        s = min(3.5, math.pi * name_r * 0.8 * 10 / adv)
        name_ok = s >= 1.5 and name_r - s > r0 + 1
        if name_ok:
            r1 = name_r - s / 2 - 1.5
    return name_ok or (p["pattern"] != "none" and r1 - r0 >= 3)


# ---- renders: the plate layout, and one closed STL per colour assembled
with open("%s/wrap.scad" % OUT, "w") as f:
    f.write('_sb_t = "";\nmodule color(c, alpha = 1) { if (c == _sb_t) children(); }\n'
            "include <../model.scad>\n")
jobs = []
for name, ov in CASES:
    d = defines(ov)
    jobs.append("openscad --backend=Manifold %s -o %s/%s.3mf model.scad" % (d, OUT, name))
    jobs.append("openscad --backend=Manifold %s -D assembled=true -o %s/%s_all.stl model.scad"
                % (d, OUT, name))
    cols = [BODY, STEM] + ([PATTERN] if decor_expected(ov) else [])
    for col in cols:
        jobs.append("openscad --backend=Manifold %s -D assembled=true -D '_sb_t=\"%s\"' "
                    "-o %s/%s_%s.stl %s/wrap.scad" % (d, col, OUT, name, col[1:], OUT))
docker("\n".join(jobs))

failures = []


def check(ok, msg):
    print(("  PASS  " if ok else "  FAIL  ") + msg)
    if not ok:
        failures.append(msg)


def near(a, b, tol=TOL):
    return abs(a - b) <= tol


for name, ov in CASES:
    print("\n[%s] %s" % (name, " ".join("%s=%s" % kv for kv in ov.items()) or "defaults"))
    g = geom(ov)
    R, H = g["R"], g["H"]
    has_decor = decor_expected(ov)

    # -- plate layout
    mats, verts, tris = read_3mf("%s/%s.3mf" % (OUT, name))
    used = Counter(t[3] for t in tris)
    named = {col for i, (n, col) in enumerate(mats) if n != "Default" and used.get(i)}
    want = {BODY, STEM} | ({PATTERN} if has_decor else set())
    check(named == want, "parts are %s (got %s)" % (sorted(want), sorted(named)))
    check(used.get(0, 0) == 0, "Default material has no triangles (got %d)" % used.get(0, 0))
    lo, hi = bbox(verts)
    elo = (-R, -max(g["half_y"], g["r_c"]), 0.0)
    ehi = (3 * R + 2 * PART_GAP + 2 * g["r_c"], max(g["half_y"], g["r_c"]),
           max(g["rim_t"], g["under_h"] - g["z_c"], g["spindle_len"]))
    check(all(near(a, b) for a, b in zip(lo + hi, elo + ehi)),
          "plate bbox %s .. %s == %s .. %s"
          % (tuple(round(x, 2) for x in lo), tuple(round(x, 2) for x in hi),
             tuple(round(x, 2) for x in elo), tuple(round(x, 2) for x in ehi)))
    check(near(lo[2], 0), "sits on z=0 (min z %.3f)" % lo[2])

    # -- assembled, one closed part per colour
    parts = {}
    for col in sorted(want):
        stl = read_stl("%s/%s_%s.stl" % (OUT, name, col[1:]))
        parts[col] = stl
    zr = {BODY: (g["z_c"], H), PATTERN: (H - INLAY, H), STEM: (0, g["spindle_len"])}
    total_v, mx, my, mz = 0.0, 0.0, 0.0, 0.0
    for col, stl in sorted(parts.items()):
        blo, bhi = bbox([v for t in stl for v in t])
        check(near(blo[2], zr[col][0]) and near(bhi[2], zr[col][1]),
              "%s closed part z %.3f .. %.3f (expected %.3f .. %.3f)"
              % (col, blo[2], bhi[2], zr[col][0], zr[col][1]))
        v, (cx, cy, cz) = mass_props(stl)
        off = math.hypot(cx, cy)
        check(off <= AXIS_TOL, "%s centre of mass %.4f mm off the axis (volume %.0f mm^3)"
              % (col, off, v))
        total_v += v
        mx, my, mz = mx + v * cx, my + v * cy, mz + v * cz
    cx, cy, cz = mx / total_v, my / total_v, mz / total_v
    check(math.hypot(cx, cy) <= AXIS_TOL,
          "whole top: centre of mass %.4f mm off the axis, %.1f mm above the tip "
          "(disc face at %.1f)" % (math.hypot(cx, cy), cz, H))
    union_v, _ = mass_props(read_stl("%s/%s_all.stl" % (OUT, name)))
    check(abs(union_v - total_v) <= 0.001 * total_v,
          "parts do not overlap: union %.1f == sum of parts %.1f mm^3" % (union_v, total_v))

    if name == "name-narrow-letters":
        # Letters sit at their own advance widths, not a fixed pitch: the gap
        # after the narrow l and i must match the others.
        gaps = letter_gaps(parts[PATTERN], (0.8 * R + 0.5))
        check(len(gaps) == len(p_name := ov["name"]) - 1 and max(gaps) - min(gaps) <= 0.6,
              "%d letters, evenly spaced: gaps %s mm" % (len(p_name), [round(x, 2) for x in gaps]))

if failures:
    print("\nFAILED: %d check(s)" % len(failures))
    sys.exit(1)
print("\nOK: %d cases" % len(CASES))
PY
