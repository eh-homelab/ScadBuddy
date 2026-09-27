#!/usr/bin/env bash
# Render models/plant-label with the defaults and each major variation, then
# check concrete facts: the number of colour parts, that no geometry is
# uncoloured, the bounding box the parameters imply, that everything sits on
# z=0, and — from one closed render per colour, the way ScadBuddy builds its
# parts — the z range of every part, the width of the pot-rim clip's gap and that
# a long name shrinks to fit the label.
#
# The checking runs on the host with python3 and the standard library only:
# the OpenSCAD image has no Python.
set -euo pipefail

cd "$(dirname "$0")"

BASE_IMAGE="${SCADBUDDY_OPENSCAD_IMAGE:-openscad/openscad:dev}"
FONTS_IMAGE="${SCADBUDDY_FONTS_IMAGE:-scadbuddy-verify:local}"
OUT="${OUT_DIR:-.verify}"
FONT_FAMILY="Lobster Two"

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
if ! docker run --rm "$IMAGE" fc-list : family | grep -F "$FONT_FAMILY" >/dev/null; then
    echo "FAIL: $IMAGE has no '$FONT_FAMILY'" >&2
    exit 1
fi
echo "==> rendering with $IMAGE"

IMAGE="$IMAGE" OUT="$OUT" python3 - <<'PY'
import math, os, re, shlex, struct, subprocess, sys, zipfile
import xml.etree.ElementTree as ET
from collections import Counter

# Defaults, mirrored from model.scad.
D = dict(label_w=70, label_h=22, stake_len=80, thickness=2.5, rim_thickness=3,
         style="stake", text_style="raised")
RELIEF, TEXT_PAD = 1.0, 2.5
CLIP_LEN, CLIP_GRIP = 18, 0.3
HOLE_D, HOLE_WALL = 4, 2.5
GREEN, WHITE = "#6BA368", "#FFFFFF"

# name, overrides
CASES = [(("%s-%s" % (st, ts)), dict(style=st, text_style=ts))
         for st in ("stake", "arrow_stake", "pot_rim_clip", "hanging_tag")
         for ts in ("raised", "inlay")]
CASES[0] = ("defaults", {})
CASES += [
    ("long-name", dict(text="Rosemary & Thyme", label_w=50, style="hanging_tag")),
    ("thin-inlay", dict(thickness=1.6, text_style="inlay")),
    ("wide-rim", dict(style="pot_rim_clip", rim_thickness=8, label_h=12)),
    ("arrow-long", dict(style="arrow_stake", text="Lemon Verbena", font="DejaVu Sans:style=Bold")),
    ("tall-text", dict(text="Mint", label_w=150, label_h=50, text_size=40)),
    ("empty-text", dict(text="")),
    ("long-stake-serif", dict(stake_len=200, font="DejaVu Serif:style=Bold", text_size=25)),
]


IMAGE, OUT = os.environ["IMAGE"], os.environ["OUT"]
NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
TOL = 0.01



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
    return RELIEF if p(ov, "text_style") == "raised" else min(RELIEF, p(ov, "thickness") * 0.4)


def expected_bbox(ov):
    W, H, T, st = p(ov, "label_w"), p(ov, "label_h"), p(ov, "thickness"), p(ov, "style")
    has_text = ov.get("text", "Basil") != ""
    top = T + (rel(ov) if p(ov, "text_style") == "raised" and has_text else 0)
    lo, hi = [-W / 2, -H / 2, 0.0], [W / 2, H / 2, top]
    if st in ("stake", "arrow_stake"):
        lo[1] = -H / 2 - p(ov, "stake_len")
    if st == "arrow_stake":
        hi[0] = W / 2 + H / 2
    if st == "pot_rim_clip":
        lo[1] = -H / 2 - CLIP_LEN
    if st == "hanging_tag":
        lo[0] = -W / 2 - HOLE_D / 2 - 1 - (HOLE_D / 2 + HOLE_WALL)
    return lo, hi


for name, ov in CASES:
    print("\n[%s] %s" % (name, " ".join("%s=%s" % kv for kv in ov.items()) or "defaults"))
    mats, verts, tris = combined[name]
    used = Counter(t[3] for t in tris)
    named = {col for i, (n, col) in enumerate(mats) if n != "Default" and used.get(i)}
    has_text = ov.get("text", "Basil") != ""
    want = {GREEN} | ({WHITE} if has_text else set())
    check(named == want, "parts are %s (got %s)" % (sorted(want), sorted(named)))
    check(used.get(0, 0) == 0, "Default material has no triangles (got %d)" % used.get(0, 0))

    lo, hi = bbox(verts)
    elo, ehi = expected_bbox(ov)
    # 0.02: the fillet pass re-traces the stake tip with a polygonal circle.
    check(all(near(a, b, 0.02) for a, b in zip(lo + hi, tuple(elo) + tuple(ehi))),
          "bbox %s .. %s == %s .. %s"
          % (tuple(round(x, 2) for x in lo), tuple(round(x, 2) for x in hi),
             tuple(round(x, 2) for x in elo), tuple(round(x, 2) for x in ehi)))
    check(near(lo[2], 0), "sits on z=0 (min z %.3f)" % lo[2])

    T, r = p(ov, "thickness"), rel(ov)
    raised = p(ov, "text_style") == "raised"
    zs = {GREEN: (0, T), WHITE: (T, T + r) if raised else (T - r, T)}
    parts = {}
    for col in sorted(want):
        stl = read_stl("%s/%s_%s.stl" % (OUT, name, col[1:]))
        parts[col] = [v for t in stl for v in t]
        blo, bhi = bbox(parts[col])
        check(near(blo[2], zs[col][0]) and near(bhi[2], zs[col][1]),
              "%s closed part z %.3f .. %.3f (expected %.3f .. %.3f)"
              % (col, blo[2], bhi[2], zs[col][0], zs[col][1]))

    W, H = p(ov, "label_w"), p(ov, "label_h")
    if has_text:
        blo, bhi = bbox(parts[WHITE])
        w, h = bhi[0] - blo[0], bhi[1] - blo[1]
        # The arrow's text box starts past its fletching notch (H / 4 deep).
        notch = H / 4 if p(ov, "style") == "arrow_stake" else 0
        box_l, box_r = -W / 2 + TEXT_PAD + notch, W / 2 - TEXT_PAD
        check(blo[0] >= box_l - 0.05 and bhi[0] <= box_r + 0.05 and h <= H - 2 * TEXT_PAD + 0.05,
              "text x %.2f .. %.2f, height %.2f fits the text box x %.2f .. %.2f, height %.1f"
              % (blo[0], bhi[0], h, box_l, box_r, H - 2 * TEXT_PAD))
        if name in ("long-name", "arrow-long"):
            check(w >= box_r - box_l - 0.5,
                  "long name shrinks to the text box width (%.2f ~ %.1f)" % (w, box_r - box_l))
        if name == "tall-text":
            check(h > 26, "text_size above 25 is honoured on a tall label (letters %.1f mm tall)" % h)

    if p(ov, "style") == "pot_rim_clip":
        rim = p(ov, "rim_thickness")
        legs = [v for v in parts[GREEN] if v[1] < -H / 2 - 0.5]
        gap = min(abs(v[0]) for v in legs) * 2
        check(near(gap, rim - 2 * CLIP_GRIP),
              "clip gap at the grip bumps is %.3f (rim %.1f - 2 x %.1f)" % (gap, rim, CLIP_GRIP))
        root = [v for v in parts[GREEN] if near(v[1], -H / 2) and abs(v[0]) < W / 4]
        check(bool(root) and near(min(abs(v[0]) for v in root) * 2, rim),
              "clip gap at the label edge is the rim thickness %.1f" % rim)

if failures:
    print("\nFAILED: %d check(s)" % len(failures))
    sys.exit(1)
print("\nOK: %d cases" % len(CASES))
PY
