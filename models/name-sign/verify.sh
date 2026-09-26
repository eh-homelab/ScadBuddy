#!/usr/bin/env bash
# Render models/name-sign with the defaults and each major variation, then
# check concrete facts: the number of colour parts, that no geometry is
# uncoloured, the bounding box the parameters imply, that everything sits on
# z=0, and — from one closed render per colour, the way ScadBuddy builds its
# parts — the z range of every part and the volume the magnet pockets remove.
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
# falls back silently and every measurement below would be meaningless.
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
if ! docker run --rm "$IMAGE" fc-list : family | grep -qF "$FONT_FAMILY"; then
    echo "FAIL: $IMAGE has no '$FONT_FAMILY'" >&2
    exit 1
fi
echo "==> rendering with $IMAGE"

IMAGE="$IMAGE" OUT="$OUT" python3 - <<'PY'
import math, os, re, shlex, struct, subprocess, sys, zipfile
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict

IMAGE, OUT = os.environ["IMAGE"], os.environ["OUT"]
NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
TOL = 0.01

# Defaults, mirrored from model.scad.
D = dict(width=140, height=45, thickness=4, border=True, border_w=2,
         text_style="raised", mount="desk_stand", stand_angle=70, magnet_d=8)
RELIEF, INSET, TEXT_PAD, PART_GAP = 1.2, 2, 2, 6
STAND_CLEAR, STAND_FRONT, STAND_FLOOR = 0.4, 4, 2
MAGNET_H, MAGNET_CLEAR = 2, 0.2
LOOP_TOP = 2.5 + 1.5 + 5.5          # loop hole r + margin + (hole r + wall)
WHITE, INK, RED = "#FFFFFF", "#1E1E1E", "#C0392B"

LONG = "Dr. Elanor Hasson-Wright"

# name, overrides
CASES = [
    ("defaults", {}),
    ("inlay-screws-2line", dict(text_style="inlay", mount="screw_holes",
                                line2="Platform Engineering")),
    ("cutout-magnets-3col", dict(text_style="cutout", mount="magnet_pockets",
                                 border_color=RED, corner="chamfered")),
    ("cutout-none-3col", dict(text_style="cutout", mount="none",
                              border_color=RED, corner="chamfered")),
    ("raised-magnets", dict(mount="magnet_pockets")),
    ("raised-none", dict(mount="none")),
    ("loop-square", dict(mount="hanging_loop", corner="square")),
    ("fit-long", dict(line1=LONG, border=False, mount="none")),
    ("nofit-long", dict(line1=LONG, border=False, mount="none", auto_fit=False)),
    ("big-45deg", dict(width=300, height=150, stand_angle=45,
                       line2="Second line")),
    ("empty-text", dict(line1="", border=False, mount="none")),
    ("thin-inlay-magnets", dict(thickness=2, text_style="inlay",
                                mount="magnet_pockets")),
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
    tris = []
    if data[:5] == b"solid" and b"facet" in data[:300]:
        vs = [tuple(map(float, m.groups())) for m in re.finditer(
            rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data)]
        tris = [vs[i:i + 3] for i in range(0, len(vs), 3)]
    else:
        n = struct.unpack("<I", data[80:84])[0]
        for i in range(n):
            f = struct.unpack("<12f", data[84 + 50 * i: 84 + 50 * i + 48])
            tris.append([f[3:6], f[6:9], f[9:12]])
    return tris


def bbox(points):
    xs, ys, zs = zip(*points)
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def volume(tris):
    v = 0.0
    for a, b, c in tris:
        v += (a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0])
              + a[2] * (b[0] * c[1] - b[1] * c[0]))
    return v / 6


# ---- phase 1: combined renders
docker("\n".join("openscad --backend=Manifold %s -o %s/%s.3mf model.scad"
                 % (defines(ov), OUT, name) for name, ov in CASES))

combined = {name: read_3mf("%s/%s.3mf" % (OUT, name)) for name, _ in CASES}

# ---- phase 2: one closed render per colour (ScadBuddy's colour wrapper)
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
docker("\n".join(jobs))

# ---- checks
failures = []


def check(ok, msg):
    print(("  PASS  " if ok else "  FAIL  ") + msg)
    if not ok:
        failures.append(msg)


def near(a, b, tol=TOL):
    return abs(a - b) <= tol


def p(ov, k):
    return ov.get(k, D[k])


def rel(ov):
    t = p(ov, "text_style")
    return RELIEF if t == "raised" else min(RELIEF, p(ov, "thickness") * 0.4)


def stand(ov):
    W, H, T, a = p(ov, "width"), p(ov, "height"), p(ov, "thickness"), p(ov, "stand_angle")
    slot = T + (rel(ov) if p(ov, "text_style") == "raised" else 0) + STAND_CLEAR
    depth = max(25, STAND_FRONT + slot + H / 2 * math.cos(math.radians(a)) + 8)
    h = STAND_FLOOR + max(8, min(16, H * 0.12))
    return depth, h


def expected_bbox(ov):
    W, H, T = p(ov, "width"), p(ov, "height"), p(ov, "thickness")
    proud = p(ov, "text_style") == "raised" and (
        p(ov, "border") or ov.get("line1", "Elan") != "" or ov.get("line2", "") != "")
    top = T + (rel(ov) if proud else 0)
    lo, hi = [-W / 2, -H / 2, 0.0], [W / 2, H / 2, top]
    m = p(ov, "mount")
    if m == "desk_stand":
        depth, h = stand(ov)
        hi[1] = H / 2 + PART_GAP + depth
        hi[2] = max(top, h)
    elif m == "hanging_loop":
        hi[1] = H / 2 + LOOP_TOP
    return lo, hi


def part_z(ov):
    """Expected z range of each colour's closed part, keyed by role."""
    T, r, s = p(ov, "thickness"), rel(ov), p(ov, "text_style")
    if s == "raised":
        return {"plate": (0, T), "text": (T, T + r), "border": (T, T + r)}
    if s == "inlay":
        return {"plate": (0, T), "text": (T - r, T), "border": (T - r, T)}
    return {"plate": (T - r, T), "text": (0, T - r), "border": (T - r, T)}


closed = {}
for name, ov in CASES:
    print("\n[%s] %s" % (name, " ".join("%s=%s" % kv for kv in ov.items()) or "defaults"))
    mats, verts, tris = combined[name]
    used = Counter(t[3] for t in tris)
    named = {col: i for i, (n, col) in enumerate(mats) if n != "Default" and used.get(i)}

    colours = {"plate": ov.get("plate_color", WHITE), "text": ov.get("text_color", INK),
               "border": ov.get("border_color", INK)}
    has_text = ov.get("line1", "Elan") != "" or ov.get("line2", "") != ""
    roles = ["plate"] + (["text"] if has_text or p(ov, "text_style") == "cutout" else []) \
        + (["border"] if p(ov, "border") else [])
    want = {colours[r] for r in roles}
    check(set(named) == want, "parts are %s (got %s)" % (sorted(want), sorted(named)))
    check(used.get(0, 0) == 0, "Default material has no triangles (got %d)" % used.get(0, 0))

    lo, hi = bbox(verts)
    elo, ehi = expected_bbox(ov)
    check(all(near(a, b) for a, b in zip(lo + hi, tuple(elo) + tuple(ehi))),
          "bbox %s .. %s == %s .. %s"
          % (tuple(round(x, 2) for x in lo), tuple(round(x, 2) for x in hi),
             tuple(round(x, 2) for x in elo), tuple(round(x, 2) for x in ehi)))
    check(near(lo[2], 0), "sits on z=0 (min z %.3f)" % lo[2])

    zs = part_z(ov)
    parts = {}
    for role in roles:
        col = colours[role]
        stl = read_stl("%s/%s_%s.stl" % (OUT, name, col[1:]))
        parts[col] = stl
        blo, bhi = bbox([v for t in stl for v in t])
        zlo, zhi = zs[role]
        if role == "plate" and p(ov, "mount") == "desk_stand":
            zlo, zhi = 0, ehi[2]           # the foot is plate-coloured
        if role == "text" and p(ov, "text_style") == "cutout" and colours["text"] == colours["border"]:
            zhi = zs["border"][1]
        if role == "border" and colours["border"] == colours["text"] and p(ov, "text_style") == "cutout":
            zlo = 0
        check(near(blo[2], zlo) and near(bhi[2], zhi),
              "%-6s %s closed part z %.3f .. %.3f (expected %.3f .. %.3f)"
              % (role, col, blo[2], bhi[2], zlo, zhi))
    closed[name] = parts

    W = p(ov, "width")
    if name in ("fit-long", "nofit-long"):
        stl = parts[colours["text"]]
        blo, bhi = bbox([v for t in stl for v in t])
        w = bhi[0] - blo[0]
        if ov.get("auto_fit", True):
            target = W - 2 * (INSET + TEXT_PAD)
            check(abs(w - target) <= 0.5,
                  "auto-fit shrinks the long line to the text box: width %.2f ~ %.2f" % (w, target))
        else:
            target = W - 2 * (INSET + TEXT_PAD / 2)
            check(near(w, target, 0.05),
                  "without auto-fit the long line is clipped to the plate: width %.2f == %.2f" % (w, target))


def pocket_volume(ov, depth):
    r = (p(ov, "magnet_d") + MAGNET_CLEAR) / 2
    return 4 * 32 * r * r * math.sin(2 * math.pi / 64) * depth   # $fn=64 polygon


print("\n[magnet pockets]")
for with_m, without, col, T, r, raised in [
        ("raised-magnets", "raised-none", WHITE, 4, RELIEF, True),
        ("cutout-magnets-3col", "cutout-none-3col", INK, 4, RELIEF, False)]:
    depth = min(MAGNET_H + MAGNET_CLEAR, T - (0.8 if raised else r + 0.6))
    removed = volume(closed[without][col]) - volume(closed[with_m][col])
    exp = pocket_volume({}, depth)
    check(abs(removed - exp) <= 0.01 * exp,
          "%s: four %.1f mm deep pockets remove %.1f mm^3 (expected %.1f)"
          % (with_m, depth, removed, exp))

if failures:
    print("\nFAILED: %d check(s)" % len(failures))
    sys.exit(1)
print("\nOK: %d cases" % len(CASES))
PY
