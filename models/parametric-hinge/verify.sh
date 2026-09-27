#!/usr/bin/env bash
# Render models/parametric-hinge with the defaults and each pin type / leaf
# shape, and check each 3MF: part count, no uncoloured geometry, sits on z=0,
# a bounding box matching the parameters, and -- the one that matters for a
# print-in-place hinge -- that the two leaves are separate bodies with at
# least `clearance` of air between them everywhere.
#
# The XML checking runs on the host: openscad/openscad:dev has no Python.
set -euo pipefail

cd "$(dirname "$0")"

BASE_IMAGE="${SCADBUDDY_OPENSCAD_IMAGE:-openscad/openscad:dev}"
FONTS_IMAGE="${SCADBUDDY_FONTS_IMAGE:-scadbuddy-verify:local}"
OUT="${OUT_DIR:-.verify}"

mkdir -p "$OUT"

# No text in this model, so any OpenSCAD image will do; prefer the ScadBuddy
# fonts image when it exists so every template verifies in the same image.
IMAGE="$BASE_IMAGE"
if docker image inspect "$FONTS_IMAGE" >/dev/null 2>&1; then
    IMAGE="$FONTS_IMAGE"
fi
echo "==> rendering with $IMAGE"

# name | -D overrides (;-separated) |
#   expected: leaf_w leaf_l thickness pin_d clearance pin_type knuckles materials bodies
CASES=(
    'defaults||30 40 3 3 0.35 print_in_place 5 1 2'
    'separate_rounded|pin_type="separate_pin";leaf_shape="rounded"|30 40 3 3 0.35 separate_pin 5 1 3'
    'filament_tapered_2col|pin_type="filament_pin";leaf_shape="tapered";color2="#C0392B"|30 40 3 3 0.35 filament_pin 5 2 2'
    'pip_small_thick|leaf_w=15;leaf_l=20;thickness=6;knuckles=11;pin_d=6;clearance=0.6;screw_holes=4;leaf_shape="tapered";color2="#2E86DE"|15 20 6 6 0.6 print_in_place 11 2 2'
    'pip_large_thin|leaf_w=80;leaf_l=150;thickness=2;knuckles=11;pin_d=2;clearance=0.2;screw_holes=4;countersink=false;leaf_shape="rounded"|80 150 2 2 0.2 print_in_place 11 1 2'
)

status=0
for c in "${CASES[@]}"; do
    IFS='|' read -r name defs expect <<<"$c"
    read -ra expect_args <<<"$expect"
    args=()
    IFS=';' read -ra kv <<<"$defs"
    for d in "${kv[@]}"; do [ -n "$d" ] && args+=(-D "$d"); done
    echo
    echo "==> $name ${args[*]:-}"
    start=$(date +%s%N)
    docker run --rm -v "$PWD":/w -w /w "$IMAGE" \
        openscad --backend=Manifold "${args[@]}" -o "$OUT/$name.3mf" model.scad >/dev/null 2>&1
    echo "    rendered in $(( ($(date +%s%N) - start) / 1000000 )) ms"
    python3 - "$OUT/$name.3mf" "${expect_args[@]}" <<'PY' || status=1
import sys, math, zipfile, xml.etree.ElementTree as ET
from collections import Counter, defaultdict

NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
path = sys.argv[1]
W, L, T, pin_d, c = map(float, sys.argv[2:7])
pin_type = sys.argv[7]
knuckles, want_mats, want_bodies = int(sys.argv[8]), int(sys.argv[9]), int(sys.argv[10])

# Must match the [Hidden] constants in model.scad.
WALL, PIN_GAP, PIN_HEAD_L = 1.2, 5.0, 1.5
bore_d = 1.75 + c if pin_type == "filament_pin" else pin_d + 2 * c
D = max(2 * T, bore_d + 2 * WALL)
exp_x, exp_y, exp_z = 2 * W, L, D
if pin_type == "separate_pin":
    exp_x += PIN_GAP + min(D, pin_d + 2)
    exp_y = PIN_HEAD_L + L + 0.5
EPS = 0.02

root = ET.fromstring(zipfile.ZipFile(path).read("3D/3dmodel.model"))
mats = [(b.get("name"), (b.get("displaycolor") or "")[:7]) for b in root.iter(NS + "base")]
V = [(float(v.get("x")), float(v.get("y")), float(v.get("z"))) for v in root.iter(NS + "vertex")]
tris = [(int(t.get("v1")), int(t.get("v2")), int(t.get("v3")), int(t.get("p1") or 0))
        for t in root.iter(NS + "triangle")]
counts = Counter(t[3] for t in tris)

failures = []


def check(ok, msg):
    print(("  PASS  " if ok else "  FAIL  ") + msg)
    if not ok:
        failures.append(msg)


def bbox(vs):
    xs, ys, zs = zip(*(V[v] for v in vs))
    return min(xs), max(xs), min(ys), max(ys), min(zs), max(zs)


# Connected bodies: union-find over shared vertex indices, all materials.
parent = list(range(len(V)))


def find(a):
    while parent[a] != a:
        parent[a] = parent[parent[a]]
        a = parent[a]
    return a


for t in tris:
    for a, b in ((t[0], t[1]), (t[1], t[2])):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
body_tris = defaultdict(list)
for t in tris:
    body_tris[find(t[0])].append(t[:3])
bodies = sorted(body_tris.values(), key=len, reverse=True)
body_verts = [sorted({v for t in b for v in t}) for b in bodies]


def sub(a, b): return (a[0] - b[0], a[1] - b[1], a[2] - b[2])
def dot(a, b): return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


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


def min_gap(verts_a, tris_b, reach):
    """Smallest distance from a vertex of A to a triangle of B, searched out
    to `reach` (returns reach if nothing is closer)."""
    cell = 1.0
    grid = defaultdict(list)
    for t in tris_b:
        pts = [V[i] for i in t]
        lo = [min(p[k] for p in pts) - reach for k in range(3)]
        hi = [max(p[k] for p in pts) + reach for k in range(3)]
        for i in range(math.floor(lo[0] / cell), math.floor(hi[0] / cell) + 1):
            for j in range(math.floor(lo[1] / cell), math.floor(hi[1] / cell) + 1):
                for k in range(math.floor(lo[2] / cell), math.floor(hi[2] / cell) + 1):
                    grid[(i, j, k)].append(t)
    best = reach
    for vi in verts_a:
        p = V[vi]
        for t in grid.get((math.floor(p[0] / cell), math.floor(p[1] / cell), math.floor(p[2] / cell)), ()):
            q = closest_on_tri(p, V[t[0]], V[t[1]], V[t[2]])
            d = math.dist(p, q)
            if d < best:
                best = d
    return best


named = [i for i, (n, _) in enumerate(mats) if n != "Default" and counts.get(i)]
for i in named:
    print("  material [%d] %s triangles=%d" % (i, mats[i][1], counts[i]))
x0, x1, y0, y1, z0, z1 = bbox(range(len(V)))
print("  bbox X %.3f  Y %.3f  Z %.3f   (expected %.3f x %.3f x %.3f)"
      % (x1 - x0, y1 - y0, z1 - z0, exp_x, exp_y, exp_z))
for k, bv in enumerate(body_verts):
    b = bbox(bv)
    print("  body %d: x %.2f .. %.2f  y %.2f .. %.2f  z %.2f .. %.2f" % ((k,) + b))

check(len(named) == want_mats, "%d non-empty material(s) besides Default (got %d)" % (want_mats, len(named)))
check(counts.get(0, 0) == 0, "Default material carries no geometry (%d triangles)" % counts.get(0, 0))
check(abs(z0) <= 1e-3, "sits on z=0 (min z %.4f)" % z0)
check(abs((x1 - x0) - exp_x) <= EPS, "X == %.3f (got %.3f)" % (exp_x, x1 - x0))
check(abs((y1 - y0) - exp_y) <= EPS, "Y == %.3f (got %.3f)" % (exp_y, y1 - y0))
check(abs((z1 - z0) - exp_z) <= EPS, "Z == knuckle diameter %.3f (got %.3f)" % (exp_z, z1 - z0))
check(len(bodies) == want_bodies, "%d separate bodies (got %d)" % (want_bodies, len(bodies)))

if len(bodies) >= 2:
    # The two leaves are the two bodies that span the axis; the loose pin, if
    # any, sits entirely to the right.
    leaves = sorted(range(len(bodies)), key=lambda k: bbox(body_verts[k])[0])[:2]
    a, b = leaves
    ba, bb = bbox(body_verts[a]), bbox(body_verts[b])
    check(abs(ba[0] + W) <= EPS and abs(bb[1] - W) <= EPS,
          "leaf 1 reaches x=-%.0f and leaf 2 reaches x=+%.0f" % (W, W))
    for k in leaves:
        bz = bbox(body_verts[k])
        check(abs(bz[4]) <= 1e-3, "leaf body %d is on the bed (min z %.4f)" % (k, bz[4]))
    reach = 2 * c
    gap = min(min_gap(body_verts[a], bodies[b], reach), min_gap(body_verts[b], bodies[a], reach))
    # Facets of a $fn polygon sit cos(180/$fn) inside the true circle; allow 2%.
    check(gap >= 0.98 * c,
          "leaves never closer than clearance %.2f (min vertex-to-face gap %.3f)" % (c, gap))
    if pin_type == "print_in_place":
        # Leaf 2's cones must actually reach into leaf 1: the leaves' Y-overlap
        # at the axis is guaranteed by the knuckles, so check the leaves are
        # interlocked by requiring leaf 2 to have material inside leaf 1's
        # outer knuckle's Y range at the axis (the cone tip past the gap).
        n = max(3, min(knuckles, 2 * math.floor((L / 4 - 1) / 2) + 1))
        pitch = L / n
        ya1 = pitch - c / 2          # end of leaf 1's first knuckle
        inside = [V[v] for v in body_verts[b]
                  if abs(V[v][0]) < 0.5 and V[v][1] < ya1 - 0.05]
        check(bool(inside), "leaf 2's cone reaches into leaf 1's first knuckle (%d vertices past its face)"
              % len(inside))

if failures:
    print("FAIL %s: %d check(s)" % (path, len(failures)))
    sys.exit(1)
print("PASS %s" % path)
PY
done

echo
if [ "$status" -ne 0 ]; then
    echo "FAILED"
    exit 1
fi
echo "OK: all cases passed"
