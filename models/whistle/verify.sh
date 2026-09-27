#!/usr/bin/env bash
# Render models/whistle with the defaults and each major variation, then check
# concrete facts: the number of colour parts, that no geometry is uncoloured,
# the bounding box the parameters imply, that everything sits on z=0 — and, by
# casting rays through one closed render of the whistle-coloured part (the way
# ScadBuddy builds its parts), that the sound path is really there:
#   - the windway is an open channel from the mouth to the window, at least
#     1 mm high and exactly the height and width the parameters imply;
#   - the window is open to the outside;
#   - the jet meets a labium edge where the parameters put it, and the half of
#     the jet below the edge runs on into the chamber;
#   - the chamber is closed where it should be;
#   - every face is vertical or horizontal, so nothing in the print overhangs
#     at an angle (the only unsupported spans are flat bridges).
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
BBOX_TOL = 0.05          # the 1 mm corner rounding is re-polygonised by offset()

# Defaults, mirrored from model.scad.
D = dict(style="classic_referee", size=1, loop=True, name="")
WHISTLE, TEXT = "#FF7043", "#FFFFFF"
SPEC = {"classic_referee": (10, 9, 27, 1.2, 2.0),
        "round_chamber": (11, 9, 24, 1.2, 2.0),
        "keychain_mini": (6.5, 6, 16, 1.0, 1.6)}
BEVEL, INLAY = 25, 0.6

CASES = [
    ("defaults", {}),
    ("round", dict(style="round_chamber")),
    ("mini", dict(style="keychain_mini")),
    ("mini-0.8-name", dict(style="keychain_mini", size=0.8, name="Ada")),
    ("classic-0.8", dict(size=0.8)),
    ("classic-1.5-noloop", dict(size=1.5, loop=False)),
    ("round-1.3-noloop-name", dict(style="round_chamber", size=1.3, loop=False,
                                   name="Maximilian12")),
    ("name", dict(name="MAYA")),
    ("mini-long-name", dict(style="keychain_mini", name="Maximilian12")),
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
    tris = []
    for i in range(n):
        f = struct.unpack("<12f", data[84 + 50 * i: 84 + 50 * i + 48])
        tris.append([f[3:6], f[6:9], f[9:12]])
    return tris


def bbox(points):
    xs, ys, zs = zip(*points)
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def hits(tris, o, d, length):
    """Distances along the unit ray o + s*d, 0 < s <= length, where it crosses the mesh."""
    out = []
    for a, b, c in tris:
        e1, e2 = sub(b, a), sub(c, a)
        p = cross(d, e2)
        det = dot(e1, p)
        if abs(det) < 1e-12:
            continue
        inv = 1 / det
        tv = sub(o, a)
        u = dot(tv, p) * inv
        if u < 0 or u > 1:
            continue
        q = cross(tv, e1)
        v = dot(d, q) * inv
        if v < 0 or u + v > 1:
            continue
        s = dot(e2, q) * inv
        if 1e-9 < s <= length:
            out.append(s)
    return sorted(out)


def first(tris, o, d, length=200):
    h = hits(tris, o, d, length)
    return h[0] if h else None


# ---- geometry the parameters imply, mirrored from model.scad
def geom(ov):
    p = dict(D, **ov)
    s = p["size"]
    Rc0, wi0, Lm0, hw0, t0 = SPEC[p["style"]]
    g = dict(p=p, Rc=Rc0 * s, wi=wi0 * s, Lm=Lm0 * s, hw=max(1, hw0 * s),
             t=max(1.6, t0 * s), ws=max(1.6, 1.6 * s), fl=max(1.2, 1.2 * s),
             tm=max(2, 2.5 * s))
    g["Wz"] = g["wi"] + 2 * g["ws"]
    g["Lw"] = max(3, 3.3 * g["hw"])
    g["y_f"] = g["Rc"] + g["fl"]
    g["y_e"] = g["y_f"] + g["hw"] / 2
    g["top"] = g["y_f"] + g["hw"] + g["t"]
    g["x_l"] = g["Lw"] * 0.3
    g["x_w"] = g["x_l"] - g["Lw"]
    g["k"] = 1.5 * g["hw"]
    g["x_r"] = g["x_l"] + (g["top"] - g["y_e"]) / math.tan(math.radians(BEVEL)) + 2
    loop_r = max(3, 3.5 * s)
    a = math.radians(-25)
    rr = g["Rc"] + g["t"] + loop_r * 0.55
    g["loop"] = (rr * math.cos(a), rr * math.sin(a), loop_r)
    return g


# ---- renders: the combined 3MF, and one closed STL per colour
with open("%s/wrap.scad" % OUT, "w") as f:
    f.write('_sb_t = "";\nmodule color(c, alpha = 1) { if (c == _sb_t) children(); }\n'
            "include <../model.scad>\n")
jobs = []
for name, ov in CASES:
    d = defines(ov)
    jobs.append("openscad --backend=Manifold %s -o %s/%s.3mf model.scad" % (d, OUT, name))
    for col in [WHISTLE] + ([TEXT] if dict(D, **ov)["name"] else []):
        jobs.append("openscad --backend=Manifold %s -D '_sb_t=\"%s\"' -o %s/%s_%s.stl %s/wrap.scad"
                    % (d, col, OUT, name, col[1:], OUT))
docker("\n".join(jobs))

failures = []


def check(ok, msg):
    print(("  PASS  " if ok else "  FAIL  ") + msg)
    if not ok:
        failures.append(msg)


def near(a, b, tol=TOL):
    return a is not None and abs(a - b) <= tol


for name, ov in CASES:
    print("\n[%s] %s" % (name, " ".join("%s=%s" % kv for kv in ov.items()) or "defaults"))
    g = geom(ov)
    p = g["p"]

    mats, verts, tris3 = read_3mf("%s/%s.3mf" % (OUT, name))
    used = Counter(t[3] for t in tris3)
    named = {col for i, (n, col) in enumerate(mats) if n != "Default" and used.get(i)}
    want = {WHISTLE} | ({TEXT} if p["name"] else set())
    check(named == want, "parts are %s (got %s)" % (sorted(want), sorted(named)))
    check(used.get(0, 0) == 0, "Default material has no triangles (got %d)" % used.get(0, 0))

    lo, hi = bbox(verts)
    Ro = g["Rc"] + g["t"]
    lx, ly, lr = g["loop"]
    elo = (-g["Lm"], min(-Ro, ly - lr) if p["loop"] else -Ro, 0.0)
    ehi = (max(Ro, g["x_r"], lx + lr if p["loop"] else 0), g["top"], g["Wz"])
    check(all(abs(a - b) <= BBOX_TOL for a, b in zip(lo + hi, elo + ehi)),
          "bbox %s .. %s == %s .. %s"
          % (tuple(round(x, 2) for x in lo), tuple(round(x, 2) for x in hi),
             tuple(round(x, 2) for x in elo), tuple(round(x, 2) for x in ehi)))
    check(near(lo[2], 0), "sits on z=0 (min z %.3f)" % lo[2])

    body = read_stl("%s/%s_%s.stl" % (OUT, name, WHISTLE[1:]))
    blo, bhi = bbox([v for t in body for v in t])
    check(near(blo[2], 0) and near(bhi[2], g["Wz"]),
          "whistle part z %.3f .. %.3f (expected 0 .. %.3f)" % (blo[2], bhi[2], g["Wz"]))
    if p["name"]:
        txt = read_stl("%s/%s_%s.stl" % (OUT, name, TEXT[1:]))
        tlo, thi = bbox([v for t in txt for v in t])
        check(near(tlo[2], g["Wz"] - INLAY) and near(thi[2], g["Wz"]),
              "name is inlaid flush, z %.3f .. %.3f (expected %.3f .. %.3f)"
              % (tlo[2], thi[2], g["Wz"] - INLAY, g["Wz"]))
        # Over the chamber, 1.5 mm in from its edge; a long name uses the
        # whole chord (it used to stop at 1.7 x Rc, leaving it tiny).
        rmax = max(math.hypot(v[0], v[1]) for t in txt for v in t)
        room = g["Rc"] + g["t"] - 1.5
        check(rmax <= room + 0.1, "name within %.2f mm of the chamber centre (reaches %.2f)" % (room, rmax))
        if len(p["name"]) >= 10:
            half = (g["Rc"] / 2) / 2
            chord = 2 * math.sqrt(room ** 2 - half ** 2)
            check(thi[0] - tlo[0] >= chord - 0.1,
                  "long name fills the %.2f mm chord (%.2f wide)" % (chord, thi[0] - tlo[0]))

    ws, wi, hw, y_f, y_e = g["ws"], g["wi"], g["hw"], g["y_f"], g["y_e"]
    zm = ws + wi / 2
    X, Y, Z = (1, 0, 0), (0, 1, 0), (0, 0, 1)
    NX, NY, NZ = (-1, 0, 0), (0, -1, 0), (0, 0, -1)

    # Windway: open along its whole length, sampled across its section.
    x0 = -g["Lm"] - 2
    blocked = [(y, z) for y in (y_f + 0.1, y_f + hw / 2, y_f + hw - 0.1)
               for z in (ws + 0.1, zm, ws + wi - 0.1)
               if hits(body, (x0, y, z), X, g["x_w"] - 0.05 - x0)]
    check(not blocked, "windway open from the mouth to the window at 9 points across it"
          + (" (blocked at %s)" % blocked if blocked else ""))
    xm = (g["x_w"] - g["Lm"]) / 2
    up, down = first(body, (xm, y_e, zm), Y), first(body, (xm, y_e, zm), NY)
    h = (up or 0) + (down or 0)
    check(near(h, hw, 0.02) and h >= 1.0 - 0.02,
          "windway is %.3f mm high (expected %.3f, never under 1)" % (h, hw))
    a, b = first(body, (xm, y_e, zm), Z), first(body, (xm, y_e, zm), NZ)
    w = (a or 0) + (b or 0)
    check(near(w, wi, 0.02), "windway is %.3f mm wide (expected %.3f)" % (w, wi))

    # Window: nothing above the jet between the windway exit and the edge.
    xwin = g["x_w"] + g["Lw"] / 2
    check(not hits(body, (xwin, y_e, zm), Y, 100),
          "window is open to the outside above the jet at x=%.2f" % xwin)

    # Labium: just above the jet's centre line the jet hits the bevel near the
    # edge; just below it, the jet passes under the labium into the chamber.
    xs = g["x_w"] + 0.1
    dz = 0.2
    exp = g["x_l"] + dz / math.tan(math.radians(BEVEL)) - xs
    hit_hi = first(body, (xs, y_e + dz, zm), X)
    check(near(hit_hi, exp, 0.1),
          "labium edge: jet %.1f mm above centre meets the bevel after %.2f mm (expected %.2f)"
          % (dz, hit_hi or -1, exp))
    hit_lo = first(body, (xs, y_e - dz, zm), X)
    check(hit_lo is not None and hit_lo >= g["x_l"] + g["k"] - xs - 0.05,
          "labium edge: jet %.1f mm below centre runs under the labium (%.2f mm, at least %.2f)"
          % (dz, hit_lo or -1, g["x_l"] + g["k"] - xs))

    # Chamber: closed round the sides and top and bottom.
    Rc = g["Rc"]
    for d_, lab in ((NX, "-x"), (X, "+x"), (NY, "-y")):
        r = first(body, (0, 0, zm), d_)
        check(near(r, Rc, 0.05), "chamber wall %s at %.3f (expected %.3f)" % (lab, r or -1, Rc))
    r1, r2 = first(body, (0, 0, zm), Z), first(body, (0, 0, zm), NZ)
    check(near(r1, wi / 2) and near(r2, wi / 2),
          "chamber roof and floor at +/-%.3f (expected %.3f); roof bridges a %.1f mm circle"
          % ((r1 or 0), wi / 2, 2 * Rc))

    # Straight-walled: every face is vertical or horizontal.
    sloped = 0
    for a_, b_, c_ in body:
        n = cross(sub(b_, a_), sub(c_, a_))
        l = math.sqrt(dot(n, n))
        if l > 1e-12 and 1e-4 < abs(n[2]) / l < 1 - 1e-4:
            sloped += 1
    check(sloped == 0, "no sloped faces: every face vertical or horizontal (%d sloped of %d)"
          % (sloped, len(body)))

if failures:
    print("\nFAILED: %d check(s)" % len(failures))
    sys.exit(1)
print("\nOK: %d cases" % len(CASES))
PY
