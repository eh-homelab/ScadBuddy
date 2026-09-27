#!/usr/bin/env bash
# Render models/train-track with the defaults and each major variation, then
# check concrete facts: the number of colour parts, that no geometry is
# uncoloured, the bounding box each piece's dimensions imply, that everything
# sits on z=0, the groove / peg / socket geometry, that a peg seats in a socket
# with exactly the set clearance, and — from one closed render per colour, the
# way ScadBuddy builds its parts — that every part is a closed mesh and an
# inlaid word exactly fills its pocket.
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
# falls back silently and the text checks would be meaningless.
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
if ! docker run --rm "$IMAGE" fc-list : family | grep -F "$FONT_FAMILY" >/dev/null; then
    echo "FAIL: $IMAGE has no '$FONT_FAMILY'" >&2
    exit 1
fi
echo "==> rendering with $IMAGE"


IMAGE="$IMAGE" OUT="$OUT" python3 - <<'PY'
import math, os, re, shlex, struct, subprocess, sys, time, zipfile
import xml.etree.ElementTree as ET
from collections import Counter

IMAGE, OUT = os.environ["IMAGE"], os.environ["OUT"]
NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
TOL = 0.01
WOOD, BROWN = "#C8A06A", "#5D4037"

# Profile, mirrored from model.scad (README cites the sources).
W, T = 40, 12
GROOVE_W, GROOVE_D, GROOVE_C = 6, 3, 13
PEG_D, HEAD_C = 11.5, 7 + 11.5 / 2
PEG_REACH = HEAD_C + PEG_D / 2            # 18.5 past the end face
BUF_H = 26

D = dict(type="straight", length=144, curve_radius=182, curve_angle=45,
         ramp_rise=64, connectors="male_female", connector_clearance=0.3, text="")

CASES = [
    ("defaults", {}),
    ("straight-text", dict(text="Reagan")),
    ("straight-36-mm", dict(length=36, connectors="male_male")),
    ("name-tile", dict(type="name_tile", text="Reagan", length=216)),
    ("name-tile-short", dict(type="name_tile", text="Too short", length=36)),
    ("curve", dict(type="curve")),
    ("curve-90-small-ff", dict(type="curve", curve_radius=90, curve_angle=90,
                               connectors="female_female")),
    ("curve-22-wide", dict(type="curve", curve_radius=300, curve_angle=22.5)),
    ("ramp", dict(type="ramp", length=216)),
    ("ramp-steep-mm", dict(type="ramp", length=36, ramp_rise=96, connectors="male_male")),
    ("crossing", dict(type="crossing")),
    ("crossing-36-mm", dict(type="crossing", length=36, connectors="male_male")),
    ("end-stop", dict(type="end_stop", length=54, text="ignored")),
    ("end-stop-ff", dict(type="end_stop", length=72, connectors="female_female")),
    ("loose", dict(connector_clearance=0.6)),
]


def scad(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return '"%s"' % v
    return repr(v)


def defines(ov):
    return " ".join("-D %s" % shlex.quote("%s=%s" % (k, scad(v))) for k, v in ov.items())


def docker(script, ok_empty=False):
    r = subprocess.run(["docker", "run", "--rm", "-v", os.getcwd() + ":/w", "-w", "/w",
                        IMAGE, "bash", "-ec", script], capture_output=True, text=True)
    err = r.stderr
    if ok_empty:
        err = err.replace("WARNING: No top level geometry to render", "")
    if r.returncode or "WARNING" in err or "ERROR" in err:
        print(r.stderr)
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
              + a[2] * (b[0] * c[1] - b[1] * c[0]))
    return v / 6


def closed(tris):
    edges = Counter()
    for t in tris:
        k = [tuple(round(c, 4) for c in p) for p in t]
        for a, b in ((0, 1), (1, 2), (2, 0)):
            edges[tuple(sorted((k[a], k[b])))] += 1
    return all(n == 2 for n in edges.values())


def kinds(c):
    return ("female" if c == "female_female" else "male",
            "male" if c == "male_male" else "female")


def ends(p):
    """[(x, y, outward heading deg, kind, deck bottom z)] as in model.scad."""
    L, A, RC = p["length"], p["curve_angle"], p["curve_radius"] + W / 2
    ka, kb = kinds(p["connectors"])
    t = p["type"]
    if t == "curve":
        return [(RC, 0, -90, ka, 0),
                (RC * math.cos(math.radians(A)), RC * math.sin(math.radians(A)), A + 90, kb, 0)]
    if t == "crossing":
        return [(-L / 2, 0, 180, ka, 0), (L / 2, 0, 0, kb, 0),
                (0, -L / 2, -90, ka, 0), (0, L / 2, 90, kb, 0)]
    if t == "end_stop":
        return [(-L / 2, 0, 180, ka, 0)]
    if t == "ramp":
        return [(-L / 2, 0, 180, ka, 0), (L / 2, 0, 0, kb, p["ramp_rise"])]
    return [(-L / 2, 0, 180, ka, 0), (L / 2, 0, 0, kb, 0)]


def expected_bbox(p):
    """Outline points of the deck plus every peg head, as (lo, hi)."""
    L, t = p["length"], p["type"]
    pts = []
    if t == "curve":
        A = p["curve_angle"]
        for r in (p["curve_radius"], p["curve_radius"] + W):
            for i in range(721):
                a = math.radians(A * i / 720)
                pts.append((r * math.cos(a), r * math.sin(a)))
    elif t == "crossing":
        pts += [(-L / 2, -W / 2), (L / 2, W / 2), (-W / 2, -L / 2), (W / 2, L / 2)]
    else:
        pts += [(-L / 2, -W / 2), (L / 2, W / 2)]
    for x, y, h, k, _ in ends(p):
        if k == "male":
            cx = x + HEAD_C * math.cos(math.radians(h))
            cy = y + HEAD_C * math.sin(math.radians(h))
            pts += [(cx - PEG_D / 2, cy - PEG_D / 2), (cx + PEG_D / 2, cy + PEG_D / 2)]
    top = T + (p["ramp_rise"] if t == "ramp" else 0)
    if t == "end_stop":
        top = BUF_H
    xs, ys = zip(*pts)
    return (min(xs), min(ys), 0.0), (max(xs), max(ys), top)


def text_box(p):
    L, sock = p["length"], HEAD_C + (12 + 2 * p["connector_clearance"]) / 2 + 2
    if p["type"] == "name_tile":
        return L - 2 * sock, 2 * (GROOVE_C - GROOVE_W / 2 - 0.5) - 3
    return L - 8, T - 1 - 3


def has_text(p):
    if not p["text"] or p["type"] not in ("straight", "name_tile"):
        return False
    w, h = text_box(p)
    return w >= 6 and h >= 3


# ---- phase 1: combined renders, timed one by one
times = {}
for name, ov in CASES:
    t0 = time.time()
    docker("openscad --backend=Manifold %s -o %s/%s.3mf model.scad" % (defines(ov), OUT, name))
    times[name] = time.time() - t0
combined = {name: read_3mf("%s/%s.3mf" % (OUT, name)) for name, _ in CASES}

# ---- phase 2: closed per-colour renders (ScadBuddy's colour wrapper), the
# text-free twin of each text case, and the peg-in-socket fit test.
with open("%s/wrap.scad" % OUT, "w") as f:
    f.write('_sb_t = "";\nmodule color(c, alpha = 1) { if (c == _sb_t) children(); }\n'
            "include <../model.scad>\n")
jobs = []
for name, ov in CASES:
    mats, _, tris = combined[name]
    used = Counter(t[3] for t in tris)
    for i, (n, col) in enumerate(mats):
        if n != "Default" and used.get(i):
            jobs.append("openscad --backend=Manifold %s -D '_sb_t=\"%s\"' -o %s/%s_%s.stl %s/wrap.scad"
                        % (defines(ov), col, OUT, name, col[1:], OUT))
    if has_text(dict(D, **ov)):
        jobs.append("openscad --backend=Manifold %s -D '_sb_t=\"%s\"' -o %s/%s_bare.stl %s/wrap.scad"
                    % (defines(dict(ov, text="")), WOOD, OUT, name, OUT))
docker("\n".join(jobs))

# Two default straights joined end to end (the second one's peg in the first
# one's socket, 0.02 mm apart so the end faces do not touch), the second nudged
# sideways by `dy`: they must not touch until the nudge takes up the whole
# side gap -- the clearance plus 0.25, since the socket is sized for a 12 mm
# head and the printed peg is 11.5.
FIT = []
for c in (0.1, 0.3, 0.6):
    for dy, want in ((c + 0.25 - 0.05, "empty"), (c + 0.25 + 0.05, "solid")):
        tag = "fit_%g_%g" % (c, dy)
        with open("%s/%s.scad" % (OUT, tag), "w") as f:
            f.write('intersection() { import("fit_%g.stl"); translate([144.02, %g, 0]) import("fit_%g.stl"); }\n'
                    % (c, dy, c))
        FIT.append((c, dy, want, tag))
docker("\n".join("openscad --backend=Manifold -D connector_clearance=%g -o %s/fit_%g.stl model.scad"
                 % (c, OUT, c) for c in (0.1, 0.3, 0.6)))
fit_result = {}
for c, dy, want, tag in FIT:
    log = docker("cd %s && openscad --backend=Manifold -o %s.stl %s.scad || true" % (OUT, tag, tag), ok_empty=True)
    fit_result[tag] = "empty" if "top level object is empty" in log or "No top level geometry" in log else "solid"

# ---- checks
failures = []


def check(ok, msg):
    print(("  PASS  " if ok else "  FAIL  ") + msg)
    if not ok:
        failures.append(msg)


def near(a, b, tol=TOL):
    return abs(a - b) <= tol


for name, ov in CASES:
    p = dict(D, **ov)
    t, L = p["type"], p["length"]
    text = has_text(p)
    print("\n[%s] %s  (%.1fs)" % (name, " ".join("%s=%s" % kv for kv in ov.items()) or "defaults",
                                  times[name]))
    mats, verts, tris = combined[name]
    used = Counter(q[3] for q in tris)
    named = {col for i, (n, col) in enumerate(mats) if n != "Default" and used.get(i)}
    want = {WOOD, BROWN} if text else {WOOD}
    check(named == want, "parts are %s (got %s)" % (sorted(want), sorted(named)))
    check(used.get(0, 0) == 0, "Default material has no triangles (got %d)" % used.get(0, 0))

    xs_, ys_, zs_ = zip(*verts)
    got = (min(xs_), min(ys_), min(zs_), max(xs_), max(ys_), max(zs_))
    lo, hi = expected_bbox(p)
    tol = 0.4 if t == "curve" else TOL      # polygonised arcs sit inside the true arc
    check(all(near(a, b, tol) for a, b in zip(got, lo + hi)),
          "bbox %s .. %s == %s .. %s" % (tuple(round(v, 2) for v in got[:3]),
                                         tuple(round(v, 2) for v in got[3:]),
                                         tuple(round(v, 2) for v in lo), tuple(round(v, 2) for v in hi)))
    check(near(got[2], 0), "sits on z=0 (min z %.3f)" % got[2])

    # Grooves: 6 wide, 3 deep, 26 apart, measured on the first end's face.
    x0, y0, h0, _, z0 = ends(p)[0]
    ux, uy = -math.sin(math.radians(h0)), math.cos(math.radians(h0))   # across the track
    across = sorted({round((v[0] - x0) * ux + (v[1] - y0) * uy, 2) for v in verts
                     if near(v[2], z0 + T - GROOVE_D)
                     and abs((v[0] - x0) * math.cos(math.radians(h0))
                             + (v[1] - y0) * math.sin(math.radians(h0))) <= TOL})
    want_g = [-16.0, -10.0, 10.0, 16.0]
    # A crossing arm shorter than 80 mm ends inside the other arm: no groove
    # edges on its end face.
    if not (t == "crossing" and L < 2 * W):
        check(across == want_g, "grooves %g wide, %g deep, %g apart at the end face (edges at %s)"
              % (GROOVE_W, GROOVE_D, 2 * GROOVE_C, across))

    # Pegs and sockets at every end.
    for x, y, h, k, zb in ends(p):
        c, s = math.cos(math.radians(h)), math.sin(math.radians(h))
        if k == "male":
            cx, cy, r, z = x + HEAD_C * c, y + HEAD_C * s, PEG_D / 2, 0
        else:
            cx, cy, r, z = x - HEAD_C * c, y - HEAD_C * s, 6 + p["connector_clearance"], 0
        ring = [v for v in verts if near(v[2], z) and abs(math.hypot(v[0] - cx, v[1] - cy) - r) <= 0.005]
        top = [v for v in verts if near(v[2], zb + T) and abs(math.hypot(v[0] - cx, v[1] - cy) - r) <= 0.005]
        check(len(ring) >= 20 and len(top) >= 20,
              "%s at (%.1f, %.1f): Ø%.2f from the bed to z=%g" % (k, cx, cy, 2 * r, zb + T))

    if t == "ramp":
        rise = p["ramp_rise"]
        lo_top = [v for v in verts if near(v[0], -L / 2) and near(v[2], T)]
        hi_top = [v for v in verts if near(v[0], L / 2) and near(v[2], rise + T)]
        check(lo_top and hi_top, "ramp deck runs from z=%g to z=%g (rise %g)" % (T, rise + T, rise))

    parts = {}
    for col in sorted(named):
        stl = read_stl("%s/%s_%s.stl" % (OUT, name, col[1:]))
        parts[col] = stl
        check(closed(stl), "%s closed part is a closed mesh (%d triangles)" % (col, len(stl)))
    if text:
        tp = [v for q in parts[BROWN] for v in q]
        if t == "name_tile":
            zr = (min(v[2] for v in tp), max(v[2] for v in tp))
            yr = max(abs(v[1]) for v in tp)
            check(near(zr[0], T - 1) and near(zr[1], T) and yr <= GROOVE_C - GROOVE_W / 2 - 0.5,
                  "word inlaid 1 deep between the grooves, z %.2f .. %.2f, |y| <= %.2f" % (*zr, yr))
        else:
            yr = (min(v[1] for v in tp), max(v[1] for v in tp))
            zr = (min(v[2] for v in tp), max(v[2] for v in tp))
            check(near(yr[0], -W / 2) and near(yr[1], -W / 2 + 1) and zr[0] >= 1.5 - TOL
                  and zr[1] <= T - 1 - 1.5 + TOL,
                  "word inlaid 1 deep in the side, below the edge chamfer, z %.2f .. %.2f" % zr)
        bare = volume(read_stl("%s/%s_bare.stl" % (OUT, name)))
        vb, vt = volume(parts[WOOD]), volume(parts[BROWN])
        check(vt > 1 and abs(vb + vt - bare) <= 1e-4 * bare + 0.01,
              "track %.1f + word %.1f == plain track %.1f mm^3 (no overlap, no gap)" % (vb, vt, bare))
    check(times[name] < 30, "renders in under 30 s (%.1fs)" % times[name])

print("\n[peg in socket, two default straights joined]")
for c, dy, want, tag in FIT:
    check(fit_result[tag] == want,
          "clearance %.2f, peg nudged %.2f sideways: %s (want %s)" % (c, dy, fit_result[tag], want))

if failures:
    print("\nFAILED: %d check(s)" % len(failures))
    sys.exit(1)
print("\nOK: %d cases + fit" % len(CASES))
PY
