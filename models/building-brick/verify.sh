#!/usr/bin/env bash
# Render models/building-brick with the defaults and each major variation, then
# check concrete facts: the number of colour parts, that no geometry is
# uncoloured, the bounding box the stud counts imply, that everything sits on
# z=0, the stud / tube / rib grip dimensions, the 45-degree slope, and — from
# one closed render per colour, the way ScadBuddy builds its parts — that every
# part is a closed mesh and an inlaid word exactly fills its pocket.
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
if ! docker run --rm --label "scadbuddy-verify=${SCADBUDDY_VERIFY_LABEL:-local}" "$IMAGE" fc-list : family | grep -F "$FONT_FAMILY" >/dev/null; then
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
RED, WHITE = "#E53935", "#FFFFFF"

# Dimensions, mirrored from model.scad (README cites the sources).
SYS = {
    "standard": dict(P=8, stud_d=4.8, stud_h=1.7, brick=9.6, plate=3.2, wall=1.2, roof=1.0),
    "big":      dict(P=16, stud_d=9.4, stud_h=4.5, brick=19.2, plate=9.6, wall=1.6, roof=2.0),
}
PLAY = 0.1

D = dict(system="standard", type="brick", studs_x=4, studs_y=2,
         stud_fit=0, wall_fit=0, top_text="")

CASES = [
    ("defaults", {}),
    ("plate-2x2", dict(type="plate", studs_x=2, studs_y=2)),
    ("plate-text-too-thin", dict(type="plate", top_text="NOPE")),
    ("tile-text", dict(type="tile", top_text="Reagan")),
    ("tile-1x1-text", dict(type="tile", studs_x=1, studs_y=1, top_text="A")),
    ("brick-side-text", dict(studs_x=6, top_text="Hello")),
    ("slope-3x2-text", dict(type="slope", studs_x=3, top_text="GO")),
    ("slope-1x1", dict(type="slope", studs_x=1, studs_y=1, top_text="X")),
    ("brick-1x1", dict(studs_x=1, studs_y=1)),
    ("plate-1x4", dict(type="plate", studs_x=1, studs_y=4)),
    ("fit-tight", dict(stud_fit=0.2, wall_fit=-0.2)),
    ("big-2x4", dict(system="big")),
    ("big-1x3", dict(system="big", studs_x=3, studs_y=1)),
    ("big-slope-text", dict(system="big", type="slope", studs_x=2, studs_y=2, top_text="Hi")),
    ("big-tile-text", dict(system="big", type="tile", top_text="Mia")),
    ("big-plate", dict(system="big", type="plate", studs_x=2, studs_y=2)),
    ("max-16x16", dict(studs_x=16, studs_y=16)),
    ("max-big-slope", dict(system="big", type="slope", studs_x=16, studs_y=16)),
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
    """Every edge is shared by exactly two triangles."""
    edges = Counter()
    for t in tris:
        k = [tuple(round(c, 4) for c in p) for p in t]
        for a, b in ((0, 1), (1, 2), (2, 0)):
            edges[tuple(sorted((k[a], k[b])))] += 1
    return all(n == 2 for n in edges.values())


# ---- phase 1: combined renders, timed one by one
times = {}
for name, ov in CASES:
    t0 = time.time()
    docker("openscad --backend=Manifold %s -o %s/%s.3mf model.scad" % (defines(ov), OUT, name))
    times[name] = time.time() - t0
combined = {name: read_3mf("%s/%s.3mf" % (OUT, name)) for name, _ in CASES}

# ---- phase 2: one closed render per colour (ScadBuddy's colour wrapper), plus
# the same case without its text so the inlay can be checked by volume.
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
    if ov.get("top_text"):
        bare = dict(ov, top_text="")
        jobs.append("openscad --backend=Manifold %s -D '_sb_t=\"%s\"' -o %s/%s_bare.stl %s/wrap.scad"
                    % (defines(bare), RED, OUT, name, OUT))
docker("\n".join(jobs))

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
    s = SYS[p["system"]]
    P, nx, ny, t = s["P"], p["studs_x"], p["studs_y"], p["type"]
    big = p["system"] == "big"
    LX, LY = nx * P - 2 * PLAY, ny * P - 2 * PLAY
    H = s["brick"] if t in ("brick", "slope") else s["plate"]
    has_studs = t != "tile" and not (t == "slope" and nx == 1)
    top = H + (s["stud_h"] if has_studs else 0)
    margin = 1.0 * (2 if big else 1)
    run = min(P - PLAY, LX)
    xs = LX / 2 - run
    box_w = ((xs if t == "slope" else LX / 2) - margin) - (-LX / 2 + margin)
    box_h = LY - 2 * margin if t == "tile" else H - 2 * margin
    text = bool(p["top_text"]) and box_w >= 4 and box_h >= 2.5

    print("\n[%s] %s  (%.1fs)" % (name, " ".join("%s=%s" % kv for kv in ov.items()) or "defaults",
                                  times[name]))
    mats, verts, tris = combined[name]
    used = Counter(q[3] for q in tris)
    named = {col for i, (n, col) in enumerate(mats) if n != "Default" and used.get(i)}
    want = {RED, WHITE} if text else {RED}
    check(named == want, "parts are %s (got %s)" % (sorted(want), sorted(named)))
    check(used.get(0, 0) == 0, "Default material has no triangles (got %d)" % used.get(0, 0))

    xs_, ys_, zs_ = zip(*verts)
    got = (min(xs_), min(ys_), min(zs_), max(xs_), max(ys_), max(zs_))
    exp = (-LX / 2, -LY / 2, 0, LX / 2, LY / 2, top)
    check(all(near(a, b) for a, b in zip(got, exp)),
          "bbox %.2f x %.2f x %.2f == %d x %g - 0.2, %d x %g - 0.2, %g%s"
          % (got[3] - got[0], got[4] - got[1], got[5] - got[2], nx, P, ny, P, H,
             " + %g studs" % s["stud_h"] if has_studs else ""))
    check(near(got[2], 0), "sits on z=0 (min z %.3f)" % got[2])

    def ring(cx, cy, z, r, tol=0.005):
        return sum(1 for v in verts if abs(v[2] - z) <= tol
                   and abs(math.hypot(v[0] - cx, v[1] - cy) - r) <= tol)

    x0, y0 = -nx * P / 2 + P / 2, -ny * P / 2 + P / 2   # first stud centre
    if has_studs:
        r = (s["stud_d"] + p["stud_fit"]) / 2
        check(ring(x0, y0, H + s["stud_h"] - 0.2 * (2 if big else 1), r) >= 24,
              "stud diameter %.2f (%.1f + stud_fit %+.2f)" % (2 * r, s["stud_d"], p["stud_fit"]))
    wf = p["wall_fit"]
    if nx > 1 and ny > 1:
        od = P * math.sqrt(2) - s["stud_d"] + 2 * wf
        check(ring(x0 + P / 2, y0 + P / 2, 0, od / 2) >= 24 and ring(x0 + P / 2, y0 + P / 2, 0, 2.4 * P / 8) >= 24,
              "tube OD %.3f / ID %.1f touches the studs below" % (od, 4.8 * P / 8))
    elif nx > 1 or ny > 1:
        pd = P - s["stud_d"] + 2 * wf
        cx, cy = (x0 + P / 2, 0) if nx > 1 else (0, y0 + P / 2)
        check(ring(cx, cy, 0, pd / 2) >= 24, "1-wide pin diameter %.2f" % pd)
    grip = P / 2 - PLAY - s["stud_d"] / 2 + wf
    rib = [v for v in verts if near(v[2], 0) and near(v[1], -LY / 2 + grip) and abs(v[0] - x0) < 1]
    check(len(rib) >= 2, "wall ribs reach %.2f in from the face, touching a %.1f stud" % (grip, s["stud_d"]))

    if t == "slope":
        above = [v for v in verts if v[0] > xs + TOL and v[2] > H - (v[0] - xs) + TOL]
        lip = [v for v in verts if near(v[0], LX / 2) and near(v[2], H - run)]
        check(not above and lip,
              "45-degree slope from z=%.2f at x=%.2f down to a %.2f lip at the +X face" % (H, xs, H - run))

    parts = {}
    for col in sorted(named):
        stl = read_stl("%s/%s_%s.stl" % (OUT, name, col[1:]))
        parts[col] = stl
        check(closed(stl), "%s closed part is a closed mesh (%d triangles)" % (col, len(stl)))
    if text:
        tp = [v for q in parts[WHITE] for v in q]
        depth = 0.6 * (2 if big else 1) if t == "tile" else s["wall"] / 2
        if t == "tile":
            zr = (min(v[2] for v in tp), max(v[2] for v in tp))
            check(near(zr[0], H - depth) and near(zr[1], H),
                  "word inlaid %.1f deep in the top, z %.3f .. %.3f" % (depth, *zr))
        else:
            yr = (min(v[1] for v in tp), max(v[1] for v in tp))
            zr = (min(v[2] for v in tp), max(v[2] for v in tp))
            check(near(yr[0], -LY / 2) and near(yr[1], -LY / 2 + depth) and zr[0] >= margin - TOL
                  and zr[1] <= H - margin + TOL,
                  "word inlaid %.1f deep in the front face, z %.2f .. %.2f" % (depth, *zr))
        bare = volume(read_stl("%s/%s_bare.stl" % (OUT, name)))
        vb, vt = volume(parts[RED]), volume(parts[WHITE])
        check(vt > 1 and abs(vb + vt - bare) <= 1e-4 * bare + 0.01,
              "brick %.2f + word %.2f == plain brick %.2f mm^3 (no overlap, no gap)" % (vb, vt, bare))
    if name.startswith("max"):
        check(times[name] < 30, "renders in under 30 s (%.1fs)" % times[name])

if failures:
    print("\nFAILED: %d check(s)" % len(failures))
    sys.exit(1)
print("\nOK: %d cases" % len(CASES))
PY
