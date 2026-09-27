#!/usr/bin/env bash
# Render models/spinning-top-pip with the defaults and each major variation,
# then check concrete facts:
#   - the plate has exactly the colour parts the parameters imply, nothing on
#     the Default material, the bounding box the parameters imply, on z=0;
#   - from one closed render per colour of the top alone (the way ScadBuddy
#     builds its parts): the parts do not overlap, and every colour's centre of
#     mass, and the whole top's, lies on the spin axis within 0.05 mm;
#   - from one closed render per printed body (top, ring, launcher, rack):
#     the top is one connected piece, the bodies printed in place are at least
#     `clearance` apart everywhere, and no body has a downward-facing surface
#     steeper than 45 degrees from vertical above the bed, other than the flat
#     bridge roofing the rack channel;
#   - the launcher releases the top (nothing of the housing reaches into the
#     gear's through-hole, and the top is no wider than its gear inside the
#     housing), and the gyro ring is captive (its ridge reaches inside the
#     top's rim).
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
import math, os, re, shlex, struct, subprocess, sys, time, zipfile
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict

IMAGE, OUT = os.environ["IMAGE"], os.environ["OUT"]
NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
TOL = 0.01
AXIS_TOL = 0.05          # centre of mass distance from the spin axis, mm
COS45 = math.cos(math.radians(45))

# Defaults, mirrored from model.scad.
D0 = dict(variant="one_piece", diameter=50, style="classic_cone", stem_length=8, stem_d=7,
          tip="ball", clearance=0.4, gear_module=1.5, pull_length=100, pattern="spiral",
          name="")
COL = dict(body="#1E88E5", pattern="#FFEB3B", pattern2="#FFFFFF", name="#FFFFFF",
           rim="#E53935", stem="#E53935", tip="#E53935", ring="#43A047",
           launcher="#FB8C00", rack="#8E24AA")
RIM_W, NAME_SIZE, H_RACK, ROOF, RING_RO = 2, 3.5, 6, 1.6, 12

CASES = [
    ("defaults", {}),
    ("one-ufo-dots-point-name", dict(style="ufo_disc", pattern="dots", tip="point", name="MAYA")),
    ("one-flower-rays", dict(style="flower", pattern="rays", pattern2_color="#EC407A")),
    ("one-small-fat-longname", dict(diameter=30, stem_d=12, stem_length=20, name="Maximilian12",
                                    pattern="rings")),
    ("one-big-thin-none", dict(diameter=80, stem_d=5, stem_length=20, tip="point",
                               pattern="none", rim_color="#1E88E5", tip_color="#FFEB3B")),
    ("launcher", dict(variant="with_launcher")),
    ("launcher-ufo-rays-m2-tight", dict(variant="with_launcher", style="ufo_disc", pattern="rays",
                                        gear_module=2, pull_length=60, clearance=0.2,
                                        tip="point")),
    ("launcher-flower-small-loose", dict(variant="with_launcher", style="flower", diameter=35,
                                         gear_module=1.25, pull_length=160, clearance=0.6,
                                         pattern="dots", name="LEO", stem_length=20)),
    # Tightest launcher fit: the smallest top on the coarsest gear, where the dome
    # narrows closest to the gear root.
    ("launcher-smallest-coarsest", dict(variant="with_launcher", diameter=30, gear_module=2,
                                        clearance=0.2, pattern="spiral")),
    ("launcher-big-rings-name", dict(variant="with_launcher", diameter=80, pattern="rings",
                                     name="SAMANTHA", stem_color="#FFEB3B")),
    ("gyro", dict(variant="gyro_ring")),
    ("gyro-flower-tight", dict(variant="gyro_ring", style="flower", clearance=0.2, name="LEO",
                               pattern="rays")),
    ("gyro-ufo-small-loose", dict(variant="gyro_ring", style="ufo_disc", diameter=30,
                                  clearance=0.6, pattern="none")),
]


def scad(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return '"%s"' % v
    return repr(v)


def defines(ov):
    return " ".join("-D %s" % shlex.quote("%s=%s" % (k, scad(v))) for k, v in ov.items())


# ---- geometry the parameters imply, mirrored from model.scad
def geom(ov):
    p = dict(D0, **ov)
    g = dict(p=p)
    d = p["diameter"]
    R = g["R"] = d / 2
    c = g["c"] = p["clearance"]
    launcher, ring = p["variant"] == "with_launcher", p["variant"] == "gyro_ring"
    ufo = p["style"] == "ufo_disc"
    tip_r = min(4, max(2, d * 0.06)) if p["tip"] == "ball" else 0.6
    tip_h = 2 * tip_r + 0.5 if p["tip"] == "ball" else 4
    rim_t = max(max(3.5, d * 0.08) if ufo else max(4, d * 0.1), 2 * (c + 1) + 1.6 if ring else 0)
    r_s = min(p["stem_d"] / 2, R * 0.25)
    Dw = max(3, min(p["stem_length"], R - RIM_W - 4 - r_s))
    m = p["gear_module"]
    r_p = 6 * m
    g.update(r_p=r_p, r_tip=r_p + m, r_root=r_p - 1.25 * m, m=m)
    t_l = g["t_l"] = H_RACK + c + ROOF
    L_g = max(p["stem_length"], t_l + 2)
    under_h = max(d * (0.16 if ufo else 0.34), tip_h + 2, 0 if launcher else tip_h + 2 + Dw - rim_t)
    H_r = under_h + rim_t
    H_f = H_r + R - (g["r_root"] - c)
    g["H_tot"] = H_f + L_g if launcher else H_r
    g["r_ch"] = g["r_tip"] + c / 2
    g["half_y"] = 0.7 * R * math.sin(math.radians(60)) + 0.3 * R if p["style"] == "flower" else R
    g["ring_w"] = max(3, d * 0.07)
    g["launcher"], g["ring"] = launcher, ring
    g["bridge_z"] = [H_RACK + c] if launcher else []

    # decor presence (mirrors pattern_ok / name_ok)
    flower_in = 0.78 * R
    face_r = (flower_in if p["style"] == "flower" else R) - RIM_W - 1
    r0 = g["r_tip"] + 2 if launcher else r_s + Dw + 1.5
    name_ok = False
    r1 = R - RIM_W - 1
    if p["name"]:
        name_r = face_r - NAME_SIZE / 2
        s = min(NAME_SIZE, math.pi * name_r * 0.8 / (len(p["name"]) * 0.78))
        name_ok = s >= 1.5 and name_r - s > r0 + 1
        if name_ok:
            r1 = name_r - s / 2 - 1.5
    g["pattern_ok"] = p["pattern"] != "none" and r1 - r0 >= 3
    g["name_ok"] = name_ok

    # bounding box of the plate
    if launcher:
        pitch = math.pi * m
        k0 = -math.ceil(p["pull_length"] / pitch)
        x_tail = k0 * pitch - pitch
        y_root = -r_p - 1.25 * m
        y_back = y_root - 5
        x_h = g["r_ch"] + 14
        x_rc = x_h + 4 + RING_RO
        y_rc = (y_back + y_root) / 2
        g["lo"] = (x_tail + c / 2, min(-g["half_y"], y_rc - RING_RO, y_back - 3), 0)
        g["hi"] = (x_rc + RING_RO, max(g["half_y"], R + 25), g["H_tot"])
    else:
        e = R + c + g["ring_w"] if ring else R
        ey = e if ring else g["half_y"]
        g["lo"], g["hi"] = (-e, -ey, 0), (e, ey, g["H_tot"])
    return g


def colours(ov):
    """The top's colour parts, and the whole plate's."""
    g = geom(ov)
    p = g["p"]
    col = {k: p.get(k + "_color", v) for k, v in COL.items()}
    top = {col["body"], col["rim"], col["stem"], col["tip"]}
    if g["pattern_ok"]:
        top |= {col["pattern"], col["pattern2"]}
    if g["name_ok"]:
        top.add(col["name"])
    plate = set(top)
    if g["ring"]:
        plate.add(col["ring"])
    if g["launcher"]:
        plate |= {col["launcher"], col["rack"]}
    return sorted(top), sorted(plate)


def bodies(g):
    return ["top"] + (["ring"] if g["ring"] else []) + (["launcher", "rack"] if g["launcher"] else [])


# ---- file readers and mesh maths
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


def components(tris):
    """Connected pieces of a triangle soup, welding vertices by position."""
    key = lambda p: (round(p[0], 4), round(p[1], 4), round(p[2], 4))
    ids = {}
    parent = []

    def vid(p):
        k = key(p)
        if k not in ids:
            ids[k] = len(parent)
            parent.append(len(parent))
        return ids[k]

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for t in tris:
        a, b, c = (vid(p) for p in t)
        for x, y in ((a, b), (b, c)):
            rx, ry = find(x), find(y)
            if rx != ry:
                parent[rx] = ry
    return len({find(i) for i in range(len(parent))})


def sub(a, b): return (a[0] - b[0], a[1] - b[1], a[2] - b[2])
def dot(a, b): return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
def cross(a, b): return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def closest_on_tri(p, a, b, c_):
    # Ericson, Real-Time Collision Detection 5.1.5.
    ab, ac, ap = sub(b, a), sub(c_, a), sub(p, a)
    d1, d2 = dot(ab, ap), dot(ac, ap)
    if d1 <= 0 and d2 <= 0:
        return a
    bp = sub(p, b)
    d3, d4 = dot(ab, bp), dot(ac, bp)
    if d3 >= 0 and d4 <= d3:
        return b
    vc = d1 * d4 - d3 * d2
    if vc <= 0 and d1 >= 0 and d3 <= 0:
        v = d1 / (d1 - d3)
        return (a[0] + v * ab[0], a[1] + v * ab[1], a[2] + v * ab[2])
    cp = sub(p, c_)
    d5, d6 = dot(ab, cp), dot(ac, cp)
    if d6 >= 0 and d5 <= d6:
        return c_
    vb = d5 * d2 - d1 * d6
    if vb <= 0 and d2 >= 0 and d6 <= 0:
        w = d2 / (d2 - d6)
        return (a[0] + w * ac[0], a[1] + w * ac[1], a[2] + w * ac[2])
    va = d3 * d6 - d5 * d4
    if va <= 0 and (d4 - d3) >= 0 and (d5 - d6) >= 0:
        w = (d4 - d3) / ((d4 - d3) + (d5 - d6))
        return (b[0] + w * (c_[0] - b[0]), b[1] + w * (c_[1] - b[1]), b[2] + w * (c_[2] - b[2]))
    denom = 1.0 / (va + vb + vc)
    v, w = vb * denom, vc * denom
    return (a[0] + ab[0] * v + ac[0] * w, a[1] + ab[1] * v + ac[1] * w, a[2] + ab[2] * v + ac[2] * w)


def min_gap(tris_a, tris_b, reach):
    """Smallest distance from a vertex of A to a triangle of B, searched out to
    `reach` (returns reach if nothing is closer)."""
    cell = 1.0
    grid = defaultdict(list)
    for t in tris_b:
        lo = [min(p[k] for p in t) - reach for k in range(3)]
        hi = [max(p[k] for p in t) + reach for k in range(3)]
        for i in range(math.floor(lo[0] / cell), math.floor(hi[0] / cell) + 1):
            for j in range(math.floor(lo[1] / cell), math.floor(hi[1] / cell) + 1):
                for k in range(math.floor(lo[2] / cell), math.floor(hi[2] / cell) + 1):
                    grid[(i, j, k)].append(t)
    best = reach
    for p in {v for t in tris_a for v in t}:
        for t in grid.get((math.floor(p[0] / cell), math.floor(p[1] / cell),
                           math.floor(p[2] / cell)), ()):
            d = math.dist(p, closest_on_tri(p, *t))
            if d < best:
                best = d
    return best


def overhangs(tris, bridge_z):
    """Downward-facing area steeper than 45 degrees from vertical that is not
    on the bed: (bad area, bridge area)."""
    bad = bridge = 0.0
    for a, b, c in tris:
        n = cross(sub(b, a), sub(c, a))
        ln = math.sqrt(dot(n, n))
        if ln < 1e-9:
            continue
        nz = n[2] / ln
        if nz >= -COS45 - 1e-3:
            continue
        zmax = max(a[2], b[2], c[2])
        if zmax <= 1e-3:
            continue                      # on the bed
        area = ln / 2
        if nz < -0.9999 and any(abs(zmax - z) <= 1e-3 for z in bridge_z):
            bridge += area
        else:
            bad += area
    return bad, bridge


# ---- renders: the plate, one closed STL per body, one per colour of the top
with open("%s/wrap_col.scad" % OUT, "w") as f:
    f.write('_sb_t = "";\nmodule color(c, alpha = 1) { if (c == _sb_t) children(); }\n'
            "include <../model.scad>\n")
with open("%s/wrap_all.scad" % OUT, "w") as f:
    f.write("module color(c, alpha = 1) { children(); }\ninclude <../model.scad>\n")
jobs = []
for name, ov in CASES:
    d = defines(ov)
    g = geom(ov)
    top_cols, _ = colours(ov)
    jobs.append("openscad --backend=Manifold %s -o %s/%s.3mf model.scad" % (d, OUT, name))
    for body in bodies(g):
        jobs.append("openscad --backend=Manifold %s -D 'part=\"%s\"' -o %s/%s_b_%s.stl %s/wrap_all.scad"
                    % (d, body, OUT, name, body, OUT))
    for col in top_cols:
        jobs.append("openscad --backend=Manifold %s -D 'part=\"top\"' -D '_sb_t=\"%s\"' "
                    "-o %s/%s_c_%s.stl %s/wrap_col.scad" % (d, col, OUT, name, col[1:], OUT))
with open("%s/jobs.txt" % OUT, "w") as f:
    f.write("\n".join(jobs) + "\n")
t0 = time.time()
r = subprocess.run(["docker", "run", "--rm", "-v", os.getcwd() + ":/w", "-w", "/w", IMAGE, "bash", "-c",
                    "tr '\\n' '\\0' < %s/jobs.txt | xargs -0 -P 8 -I{} bash -c '{} 2>&1' "
                    "| grep -E 'WARNING|ERROR' ; true" % OUT],
                   capture_output=True, text=True)
if r.returncode or r.stdout.strip() or r.stderr.strip():
    print(r.stdout, r.stderr)
    sys.exit("FAIL: OpenSCAD reported errors or warnings")
print("    %d renders in %.1f s" % (len(jobs), time.time() - t0))

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
    c = g["c"]
    top_cols, plate_cols = colours(ov)

    # -- plate
    mats, verts, tris = read_3mf("%s/%s.3mf" % (OUT, name))
    used = Counter(t[3] for t in tris)
    named = {col for i, (n, col) in enumerate(mats) if n != "Default" and used.get(i)}
    check(named == set(plate_cols), "parts are %s (got %s)" % (plate_cols, sorted(named)))
    check(used.get(0, 0) == 0, "Default material has no triangles (got %d)" % used.get(0, 0))
    lo, hi = bbox(verts)
    check(all(near(a, b) for a, b in zip(lo + hi, g["lo"] + g["hi"])),
          "plate bbox %s .. %s == %s .. %s"
          % (tuple(round(x, 2) for x in lo), tuple(round(x, 2) for x in hi),
             tuple(round(x, 2) for x in g["lo"]), tuple(round(x, 2) for x in g["hi"])))
    check(near(lo[2], 0), "sits on z=0 (min z %.3f)" % lo[2])

    # -- the top alone, one closed part per colour: balance and no overlap
    total_v, mx, my = 0.0, 0.0, 0.0
    for col in top_cols:
        v, (cx, cy, _) = mass_props(read_stl("%s/%s_c_%s.stl" % (OUT, name, col[1:])))
        off = math.hypot(cx, cy)
        check(off <= AXIS_TOL, "%s centre of mass %.4f mm off the axis (volume %.0f mm^3)"
              % (col, off, v))
        total_v, mx, my = total_v + v, mx + v * cx, my + v * cy
    check(math.hypot(mx / total_v, my / total_v) <= AXIS_TOL,
          "whole top: centre of mass %.4f mm off the axis" % math.hypot(mx / total_v, my / total_v))

    # -- bodies
    body = {b: read_stl("%s/%s_b_%s.stl" % (OUT, name, b)) for b in bodies(g)}
    union_v, _ = mass_props(body["top"])
    check(abs(union_v - total_v) <= 0.001 * total_v,
          "colour parts do not overlap: top %.1f == sum of parts %.1f mm^3" % (union_v, total_v))
    for b, t in body.items():
        n = components(t)
        check(n == 1, "%s is one connected piece (%d)" % (b, n))
        bad, bridge = overhangs(t, g["bridge_z"] if b == "launcher" else [])
        check(bad < 0.01, "%s: no overhang steeper than 45 deg off the bed (%.2f mm^2%s)"
              % (b, bad, ", flat bridge roof %.0f mm^2" % bridge if bridge else ""))
    names = list(body)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            reach = 2 * c
            gap = min(min_gap(body[a], body[b], reach), min_gap(body[b], body[a], reach))
            # Facets of a $fn polygon sit cos(180/$fn) inside the true circle; allow 2%.
            check(gap >= 0.98 * c, "%s and %s at least clearance %.2f apart (min gap %.3f%s)"
                  % (a, b, c, gap, "+" if gap >= reach else ""))
    if g["launcher"]:
        top_in = max(math.hypot(p[0], p[1]) for t in body["top"] for p in t if p[2] <= g["t_l"] + 1e-3)
        housing_in = min(math.hypot(p[0], p[1]) for t in body["launcher"] for p in t)
        check(top_in <= g["r_tip"] - c / 2 + 1e-3 and housing_in >= g["r_ch"] - 0.02,
              "top releases: gear %.2f wide inside the housing, through-hole %.2f clear"
              % (2 * top_in, 2 * housing_in))
    if g["ring"]:
        ring_in = min(math.hypot(p[0], p[1]) for t in body["ring"] for p in t)
        top_out = max(math.hypot(p[0], p[1]) for t in body["top"] for p in t)
        check(ring_in < top_out - 0.99 and ring_in > g["R"] - 2,
              "ring is captive: its ridge reaches r=%.2f inside the rim's r=%.2f" % (ring_in, top_out))

if failures:
    print("\nFAILED: %d check(s)" % len(failures))
    sys.exit(1)
print("\nOK: %d cases" % len(CASES))
PY
