#!/usr/bin/env bash
# Render models/dice with the defaults and each major variation, then check
# concrete facts:
#   - the colour parts, that no geometry is uncoloured, the bounding box the
#     size and count imply, and that everything sits on z=0;
#   - from one closed render per colour (the way ScadBuddy builds its parts):
#     the inlays are flush (die + faces == the plain body, nothing proud and no
#     gap), the parts do not overlap, every inlay vertex lies within
#     inlay_depth of the surface, each face that should carry artwork does and
#     a blank face does not, one die body per die;
#   - pips: 21 per die, 1 on top, 6 underneath, 2 front, 5 back, 3 right,
#     4 left (opposite faces add up to 7), and their volume is exactly 21
#     pip-sized discs inlay_depth deep.
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
# falls back silently.
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

IMAGE, OUT = os.environ["IMAGE"], os.environ["OUT"]
NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
TOL = 0.01

D = dict(size=20, rounding=2, faces="pips", inlay_depth=0.8, count=1)
PART_GAP = 6
DIE, FACE = "#FFF3E0", "#D81B60"
BLANK = {"face_%d" % k: "" for k in range(1, 7)}

CASES = [
    ("defaults", {}),
    ("numbers", dict(faces="numbers")),
    ("words", dict(faces="custom_text")),
    ("shapes", dict(faces="emoji_shapes")),
    ("pips-6-small-round", dict(count=6, size=12, rounding=5)),
    ("shapes-big-sharp", dict(faces="emoji_shapes", size=40, rounding=0, count=2)),
    ("words-blank-3-long", dict(faces="custom_text", face_3="", face_6="WIGGLES!",
                                inlay_depth=1.4, count=4)),
    ("words-all-blank", dict(faces="custom_text", **BLANK)),
    ("numbers-deep-5", dict(faces="numbers", inlay_depth=2, count=5, size=16)),
    # Full rounding on the smallest die: the artwork must shrink onto the flat
    # rather than wrap over the rounded edges.
    ("words-small-round", dict(faces="custom_text", size=12, rounding=5)),
    ("shapes-small-round", dict(faces="emoji_shapes", size=12, rounding=5)),
    ("numbers-small-round", dict(faces="numbers", size=12, rounding=5)),
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


def components(tris):
    """Connected pieces of a triangle soup, joined on shared vertex positions."""
    parent = {}

    def key(v):
        return tuple(round(c, 4) for c in v)

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for t in tris:
        ks = [key(v) for v in t]
        for k in ks:
            parent.setdefault(k, k)
        for k in ks[1:]:
            ra, rb = find(ks[0]), find(k)
            if ra != rb:
                parent[ra] = rb
    groups = {}
    for t in tris:
        groups.setdefault(find(key(t[0])), []).append(t)
    return list(groups.values())


def p(ov, k):
    return ov.get(k, D[k])


def centres(ov):
    s, n = p(ov, "size"), p(ov, "count")
    cols = min(n, 3)
    return [((i % cols) * (s + PART_GAP) + s / 2, (i // cols) * (s + PART_GAP) + s / 2, s / 2)
            for i in range(n)]


def nearest_centre(ov, v):
    return min(centres(ov), key=lambda c: (c[0] - v[0]) ** 2 + (c[1] - v[1]) ** 2)


# face k -> (axis, sign): 1 top, 6 bottom, 2 front(-y), 5 back, 3 right, 4 left
FACES = {1: (2, 1), 6: (2, -1), 2: (1, -1), 5: (1, 1), 3: (0, 1), 4: (0, -1)}


def face_of(ov, v):
    c = nearest_centre(ov, v)
    d = [v[i] - c[i] for i in range(3)]
    ax = max(range(3), key=lambda i: abs(d[i]))
    sg = 1 if d[ax] > 0 else -1
    return next(k for k, f in FACES.items() if f == (ax, sg))


# ---- renders
docker("\n".join("openscad --backend=Manifold %s -o %s/%s.3mf model.scad"
                 % (defines(ov), OUT, name) for name, ov in CASES))
combined = {name: read_3mf("%s/%s.3mf" % (OUT, name)) for name, _ in CASES}

with open("%s/wrap.scad" % OUT, "w") as f:
    f.write('_sb_t = "";\nmodule color(c, alpha = 1) { if (_sb_t == "*" || c == _sb_t) children(); }\n'
            "include <../model.scad>\n")
jobs = []
for name, ov in CASES:
    mats, _, tris = combined[name]
    used = Counter(t[3] for t in tris)
    present = {col for i, (nm, col) in enumerate(mats) if nm != "Default" and used.get(i)}
    for col, tag in [(DIE, "die"), (FACE, "face"), ("*", "all")]:
        if col != "*" and col not in present:
            continue
        jobs.append("openscad --backend=Manifold %s -D '_sb_t=\"%s\"' -o %s/%s_%s.stl %s/wrap.scad"
                    % (defines(ov), col, OUT, name, tag, OUT))
    # The plain body the inlays must fill exactly: every face blank.
    blank = dict(ov, faces="custom_text", **BLANK)
    jobs.append("openscad --backend=Manifold %s -D '_sb_t=\"*\"' -o %s/%s_body.stl %s/wrap.scad"
                % (defines(blank), OUT, name, OUT))
docker("\n".join(jobs))

failures = []


def check(ok, msg):
    print(("  PASS  " if ok else "  FAIL  ") + msg)
    if not ok:
        failures.append(msg)


def near(a, b, tol=TOL):
    return abs(a - b) <= tol


def load(name, tag):
    path = "%s/%s_%s.stl" % (OUT, name, tag)
    return read_stl(path) if os.path.exists(path) and os.path.getsize(path) > 84 else []


for name, ov in CASES:
    print("\n[%s] %s" % (name, " ".join("%s=%s" % kv for kv in ov.items()) or "defaults"))
    s, n, depth, style = p(ov, "size"), p(ov, "count"), p(ov, "inlay_depth"), p(ov, "faces")
    art = {k: style != "custom_text" or ov.get("face_%d" % k,
           ["HOP", "JUMP", "SPIN", "CLAP", "SING", "DANCE"][k - 1]) != "" for k in FACES}

    mats, verts, tris = combined[name]
    used = Counter(t[3] for t in tris)
    named = {col for i, (nm, col) in enumerate(mats) if nm != "Default" and used.get(i)}
    want = {DIE} | ({FACE} if any(art.values()) else set())
    check(named == want, "parts are %s (got %s)" % (sorted(want), sorted(named)))
    check(used.get(0, 0) == 0, "Default material has no triangles (got %d)" % used.get(0, 0))

    cols, rows = min(n, 3), (n + 2) // 3
    elo = (0.0, 0.0, 0.0)
    ehi = (cols * s + (cols - 1) * PART_GAP, rows * s + (rows - 1) * PART_GAP, s)
    lo, hi = bbox(verts)
    check(all(near(a, b) for a, b in zip(lo + hi, elo + ehi)),
          "bbox %s .. %s == %s .. %s" % (tuple(round(x, 2) for x in lo),
                                         tuple(round(x, 2) for x in hi), elo, ehi))
    check(near(lo[2], 0), "sits on z=0 (min z %.3f)" % lo[2])

    die, face, whole, body = (load(name, t) for t in ("die", "face", "all", "body"))
    vd, vf, vw, vb = volume(die), volume(face), volume(whole), volume(body)
    check(abs(vd + vf - vw) <= 0.001 * vw,
          "parts do not overlap: die %.1f + faces %.1f == whole %.1f mm^3" % (vd, vf, vw))
    check(abs(vw - vb) <= 0.001 * vb,
          "inlays are flush: whole %.1f == plain body %.1f mm^3" % (vw, vb))
    check(len(components(die)) == n, "%d die bod%s (got %d)"
          % (n, "y" if n == 1 else "ies", len(components(die))))

    if not face:
        check(not any(art.values()), "no artwork, no face part")
        continue

    # Every inlay vertex is within inlay_depth of its die's surface.
    worst = min(max(abs(v[i] - nearest_centre(ov, v)[i]) for i in range(3))
                for t in face for v in t)
    check(worst >= s / 2 - depth - TOL,
          "inlays are %.1f mm deep (deepest vertex %.3f in from the surface)"
          % (depth, s / 2 - worst))

    # The artwork stays on the flat of each face, clear of the rounded edges.
    r = min(p(ov, "rounding"), s / 5)
    flat = s / 2 - r
    spill = max(sorted((abs(v[i] - nearest_centre(ov, v)[i]) for i in range(3)))[1]
                for t in face for v in t)
    check(spill <= flat + TOL,
          "artwork stays on the flat: %.3f from the centre line <= %.3f" % (spill, flat))

    # Faces with artwork carry an inlay on every die; blank faces carry none.
    per_face = Counter()
    for comp in components(face):
        c = [sum(v[i] for t in comp for v in t) / (3 * len(comp)) for i in range(3)]
        per_face[face_of(ov, c)] += 1
    for k in sorted(FACES):
        if art[k]:
            check(per_face[k] >= n, "face %d has artwork on all %d dice (%d pieces)"
                  % (k, n, per_face[k]))
        else:
            check(per_face[k] == 0, "face %d is blank (%d pieces)" % (k, per_face[k]))

    if style == "pips":
        check(all(per_face[k] == k * n for k in FACES),
              "pips: %s per face over %d dice == face number x dice"
              % (dict(sorted(per_face.items())), n))
        check(all(per_face[a] + per_face[b] == 7 * n for a, b in [(1, 6), (2, 5), (3, 4)]),
              "opposite faces add up to 7 (1/6 top/bottom, 2/5 front/back, 3/4 right/left)")
        fit = min(1, 0.92 * (s - 2 * r) / (0.72 * s))
        pd = s * 0.17 * fit
        disc = 0.5 * 48 * (pd / 2) ** 2 * math.sin(2 * math.pi / 48)
        exp = 21 * n * disc * depth
        check(abs(vf - exp) <= 0.001 * exp,
              "pip volume %.2f == 21 x %d discs of %.2f mm x %.1f mm deep (%.2f)"
              % (vf, n, pd, depth, exp))

if failures:
    print("\nFAILED: %d check(s)" % len(failures))
    sys.exit(1)
print("\nOK: %d cases" % len(CASES))
PY
