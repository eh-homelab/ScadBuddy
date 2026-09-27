#!/usr/bin/env bash
# Render models/articulated-critter with the defaults and each major variation,
# then check concrete facts about the print-in-place hinges and the colours:
#
#   - the plate has exactly the colour parts the parameters imply, nothing on
#     the Default material, sits on z=0, is `thickness` tall and fits the bed;
#   - laid out straight, it is `length` long and at least `width` wide;
#   - the print is exactly head + body segments + tail separate pieces (a fused
#     joint would merge two);
#   - measured on the rendered geometry, every gap between neighbouring
#     segments is at least `clearance` on every probed layer, and not wider
#     than it needs to be (the probe at clearance + 0.03 finds the hinges);
#   - every pin is captured: moving a segment 2 mm forwards out of its
#     neighbour's C ring, or 1.5 mm up out of it, collides at every joint;
#   - rendered once per colour the way ScadBuddy builds its closed parts, the
#     colour parts do not overlap (the volume of the union equals the sum).
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
import os, re, shlex, struct, subprocess, sys, zipfile
import xml.etree.ElementTree as ET
from collections import Counter

IMAGE, OUT = os.environ["IMAGE"], os.environ["OUT"]
NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
BED_X, BED_Y = 300, 320

# Defaults, mirrored from model.scad.
D = dict(animal="dragon", segments=7, length=220, width=26, thickness=8, pose="wave",
         name="", clearance=0.4)
BODY, STRIPE, HEAD, EYE, DETAIL, NAME = ("#43A047", "#FDD835", "#FB8C00", "#FFFFFF",
                                         "#212121", "#1E88E5")

CASES = [
    ("defaults", {}),
    ("snake-straight", dict(animal="snake", pose="straight")),
    ("lizard-name", dict(animal="lizard", name="LEO", pose="straight")),
    ("fish-curl", dict(animal="fish", pose="curl", name="Nemo")),
    ("caterpillar", dict(animal="caterpillar", pose="straight", segments=9)),
    ("dragon-straight-name", dict(pose="straight", name="MAYA")),
    # Many thin segments at the tightest clearance, curled.
    ("snake-max-tight", dict(animal="snake", segments=20, length=300, width=22, thickness=6,
                             clearance=0.25, pose="curl")),
    # Widest, thickest, loosest, shortest: segments must be dropped to fit.
    ("caterpillar-fat-short", dict(animal="caterpillar", segments=20, length=120, width=40,
                                   thickness=12, clearance=0.6, pose="wave")),
    # A 12-letter name with 3 segments asked for: segments are added for it.
    ("lizard-long-name", dict(animal="lizard", segments=3, length=300, name="Maximilian12",
                              pose="straight")),
    ("dragon-min", dict(segments=3, length=120, width=22, thickness=6, pose="straight")),
    ("fish-wave-wide", dict(animal="fish", segments=5, width=40, clearance=0.6)),
    # A name with wide letters on a critter too short for the segments asked:
    # segments are dropped, but never below the pitch a 5 mm letter needs.
    ("name-pitch-limited", dict(animal="snake", segments=20, length=200, width=22,
                                name="WQW", pose="curl")),
    # Too wide for its length even with one segment: the length grows (and
    # the render log says so) rather than the head and tail overlapping.
    ("dragon-grows", dict(width=40, length=120, pose="straight")),
]

# Requests that cannot be met must fail the render with a message that says
# what to change, never render a critter with letters missing.
ERROR_CASES = [
    ("name-too-long", dict(animal="lizard", length=120, name="Maximilian12"),
     "raise length or shorten the name"),
    ("name-wide-short", dict(animal="caterpillar", length=120, width=40, name="WQW"),
     "raise length or shorten the name"),
    # The plate-fit assert fires. No customizer combination reaches it (the
    # widest straight critter is about 100 mm across), so the plate is
    # narrowed through the hidden bed size to prove the guard works.
    ("bed-too-small", dict(bed_w=150), "more than the 150 x 320 mm plate"),
]

# Cases asking for more segments than the length has room for at the minimum
# pitch, and how many fit (computed by hand from the head, tail and pitch
# factors in model.scad).
DROPPED = {"snake-max-tight": 15, "caterpillar-fat-short": 3, "dragon-min": 2,
           "name-pitch-limited": 7, "dragon-grows": 1,
           "dragon-straight-name": 6}
# Cases where even one segment does not fit, so the critter is longer than asked.
GROWS = {"dragon-grows"}


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


# ---- requests that must fail loudly
for name, ov, want in ERROR_CASES:
    r = subprocess.run(["docker", "run", "--rm", "-v", os.getcwd() + ":/w", "-w", "/w", IMAGE,
                        "bash", "-ec", "openscad --backend=Manifold %s -o %s/%s.3mf model.scad"
                        % (defines(ov), OUT, name)], capture_output=True, text=True)
    ok = r.returncode != 0 and "Assertion" in r.stderr and want in r.stderr
    print(("  PASS  " if ok else "  FAIL  ") + "[%s] render fails with '%s'" % (name, want))
    if not ok:
        print(r.stderr[-1500:])
        sys.exit("FAIL: %s did not fail as expected" % name)

# ---- renders
with open("%s/wrap.scad" % OUT, "w") as f:
    f.write('_sb_t = "";\nmodule color(c, alpha = 1) { if (c == _sb_t) children(); }\n'
            "include <../model.scad>\n")

info = {}
for name, ov in CASES:
    p = dict(D, **ov)
    d = defines(ov)
    c = p["clearance"]
    jobs = [
        "openscad --backend=Manifold %s -o %s/%s.3mf model.scad" % (d, OUT, name),
        "openscad --backend=Manifold %s -o %s/%s_all.stl model.scad" % (d, OUT, name),
        "openscad --backend=Manifold %s -D probe_gap=%s -o %s/%s_gap_lo.stl model.scad"
        % (d, round(c - 0.03, 3), OUT, name),
        "openscad --backend=Manifold %s -D probe_gap=%s -o %s/%s_gap_hi.stl model.scad"
        % (d, round(c + 0.03, 3), OUT, name),
        "openscad --backend=Manifold %s -D 'probe_capture=[2,0]' -o %s/%s_cap_x.stl model.scad"
        % (d, OUT, name),
        "openscad --backend=Manifold %s -D 'probe_capture=[0,1.5]' -o %s/%s_cap_z.stl model.scad"
        % (d, OUT, name),
    ]
    for col in (BODY, STRIPE, HEAD, EYE, DETAIL, NAME):
        jobs.append("openscad --backend=Manifold %s -D '_sb_t=\"%s\"' -o %s/%s_%s.stl %s/wrap.scad"
                    " 2>&1 | grep -v 'Current top level object is empty' >&2 || true"
                    % (d, col, OUT, name, col[1:], OUT))
    log = docker("\n".join(jobs))
    m = re.search(r'SB_CRITTER N=(\d+) pitch=(\S+) length=(\S+) R=(\S+) neck=(\S+) '
                  r'cap_mid=(\S+) cap_top=(\S+) cap_lip=(\S+) low_h=(\S+) name_size=(\S+) '
                  r'bound=\[(\S+), (\S+)\] '
                  r'joints=\[([^\]]*)\]', log)
    if not m:
        print(log[-2000:])
        sys.exit("FAIL: no SB_CRITTER echo for %s" % name)
    info[name] = dict(N=int(m.group(1)), pitch=float(m.group(2)), L=float(m.group(3)),
                      R=float(m.group(4)), neck=float(m.group(5)), cap_mid=float(m.group(6)),
                      cap_top=float(m.group(7)), cap_lip=float(m.group(8)),
                      name_size=float(m.group(10)),
                      bound=(float(m.group(11)), float(m.group(12))),
                      joints=[float(x) for x in m.group(13).split(",")])

failures = []


def check(ok, msg):
    print(("  PASS  " if ok else "  FAIL  ") + msg)
    if not ok:
        failures.append(msg)


for name, ov in CASES:
    p = dict(D, **ov)
    I = info[name]
    N, T, c = I["N"], p["thickness"], p["clearance"]
    print("\n[%s] %s" % (name, " ".join("%s=%s" % kv for kv in ov.items()) or "defaults"))
    print("  N=%d pitch=%.2f length=%.1f neck=%.2f capture mid %.2f top %.2f lip %.2f"
          " name size %.2f" % (N, I["pitch"], I["L"], I["neck"], I["cap_mid"], I["cap_top"],
                               I["cap_lip"], I["name_size"]))

    # -- segments: as many as asked (or the name needs), unless too short
    want_n = max(p["segments"], len(p["name"]))
    if name in DROPPED:
        # Too many for the length: as many as fit, each at least the hinge minimum.
        check(N == DROPPED[name], "segments dropped to fit the length: %d of %d (expected %d)"
              % (N, want_n, DROPPED[name]))
    else:
        check(N == want_n, "%d body segments (want %d)" % (N, want_n))
    check(I["pitch"] >= 2 * I["R"] + c + 2 - 1e-6,
          "segment pitch %.2f >= hinge minimum %.2f" % (I["pitch"], 2 * I["R"] + c + 2))

    # -- plate
    mats, verts, tris = read_3mf("%s/%s.3mf" % (OUT, name))
    used = Counter(t[3] for t in tris)
    named = {col for i, (n, col) in enumerate(mats) if n != "Default" and used.get(i)}
    want = {BODY, STRIPE, HEAD, EYE, DETAIL} | ({NAME} if p["name"] else set())
    check(named == want, "parts are %s (got %s)" % (sorted(want), sorted(named)))
    check(used.get(0, 0) == 0, "Default material has no triangles (got %d)" % used.get(0, 0))
    xs, ys, zs = zip(*verts)
    check(abs(min(zs)) < 1e-3 and abs(max(zs) - T) < 1e-3,
          "sits on z=0 and is %.1f mm tall (z %.3f .. %.3f)" % (T, min(zs), max(zs)))
    sx, sy = max(xs) - min(xs), max(ys) - min(ys)
    check(sx <= BED_X and sy <= BED_Y, "fits the %dx%d bed (%.1f x %.1f)" % (BED_X, BED_Y, sx, sy))
    # The model asserts its own plate fit from a bound it computes; that bound
    # must really contain the render, or the assert protects nothing.
    bx, by = I["bound"]
    check(sx <= bx + 0.01 and sy <= by + 0.01,
          "render %.1f x %.1f lies inside the model's own bound %.1f x %.1f"
          % (sx, sy, bx, by))
    if name in GROWS:
        check(I["L"] > p["length"], "too short for one segment: grows to %.1f (asked %s)"
              % (I["L"], p["length"]))
        check(N == 1, "with a single body segment (%d)" % N)
    elif p["pose"] == "straight":
        check(abs(I["L"] - p["length"]) < 1e-3, "length %.1f == %s" % (I["L"], p["length"]))
        check(abs(sx - I["L"]) <= 1.5, "straight: %.1f mm long (%.1f)" % (sx, I["L"]))
        check(sy >= p["width"] - 0.5, "straight: %.1f mm wide >= width %s" % (sy, p["width"]))
    check(components(tris) == N + 2,
          "%d separate pieces == head + %d segments + tail" % (components(tris), N))
    if p["name"]:
        # Room for a 4.5 mm letter as wide as the name's widest (W 1.45, M/Q/m/w
        # 1.22, others 1.07 x the size) between the hinge hole and the notch.
        wide = max(1.45 if ch == "W" else 1.22 if ch in "MmwQ@&%" else 1.07
                   for ch in p["name"])
        need = 4.5 * wide + (3.2 + c + 0.6 + 0.6) + (I["R"] + c + 0.6)
        check(I["pitch"] >= need - 1e-6, "pitch %.2f leaves room for a 4.5 mm letter (>= %.2f)"
              % (I["pitch"], need))
        check(I["name_size"] >= 4.5 - 0.01, "name letters are %.1f mm (>= 4.5)" % I["name_size"])

    # -- hinge capture, from the geometry the parameters imply
    check(I["neck"] >= 2.4, "hinge neck %.2f mm wide" % I["neck"])
    check(I["cap_mid"] > 0.3 and I["cap_top"] > 0.15 and I["cap_lip"] > 0.5,
          "pin wider than the gap in the C (%.2f / %.2f mm) and than the lip (%.2f mm)"
          % (I["cap_mid"], I["cap_top"], I["cap_lip"]))

    # -- clearance probes
    lo = real_points(read_stl("%s/%s_gap_lo.stl" % (OUT, name)))
    hi = real_points(read_stl("%s/%s_gap_hi.stl" % (OUT, name)))
    check(not lo, "every gap between neighbours >= %.2f mm on every probed layer "
          "(%d overlap vertices at %.2f)" % (c - 0.03, len(lo), c - 0.03))
    check(len(hi) > 0, "probe at %.2f finds the hinge gaps (%d vertices), so it measures"
          % (c + 0.03, len(hi)))

    J = I["joints"]

    def joints_hit(pts):
        return {min(range(len(J)), key=lambda i: abs(J[i] - x)) for x, _, _ in pts}

    for tag, what in (("x", "forwards 2 mm"), ("z", "up 1.5 mm")):
        hit = joints_hit(real_points(read_stl("%s/%s_cap_%s.stl" % (OUT, name, tag))))
        check(hit == set(range(N + 1)), "every pin captured moving %s (%d of %d joints)"
              % (what, len(hit), N + 1))

    # -- closed colour parts do not overlap
    total = 0.0
    for col in want:
        path = "%s/%s_%s.stl" % (OUT, name, col[1:])
        total += volume(read_stl(path))
    union = volume(read_stl("%s/%s_all.stl" % (OUT, name)))
    check(abs(union - total) <= 0.001 * union,
          "colour parts do not overlap: union %.1f == sum of parts %.1f mm^3" % (union, total))

if failures:
    print("\nFAILED: %d check(s)" % len(failures))
    sys.exit(1)
print("\nOK: %d cases" % len(CASES))
PY
