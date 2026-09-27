#!/usr/bin/env bash
# Render models/bag-clip with the defaults and the main variations and check
# each 3MF: part count, no uncoloured geometry, one connected clip, sits on
# z=0, and a bounding box that matches what the parameters imply.
#
# The XML checking runs on the host: openscad/openscad:dev has no Python.
set -euo pipefail

cd "$(dirname "$0")"

BASE_IMAGE="${SCADBUDDY_OPENSCAD_IMAGE:-openscad/openscad:dev}"
FONTS_IMAGE="${SCADBUDDY_FONTS_IMAGE:-scadbuddy-verify:local}"
OUT="${OUT_DIR:-.verify}"
FONT_FAMILY="DejaVu Sans"

mkdir -p "$OUT"

# DejaVu ships in openscad/openscad:dev, but prefer the image with the same
# font packages as ScadBuddy so text metrics match production.
IMAGE="$BASE_IMAGE"
if docker image inspect "$FONTS_IMAGE" >/dev/null 2>&1; then
    IMAGE="$FONTS_IMAGE"
elif ! docker run --rm "$BASE_IMAGE" fc-list : family | grep -F "$FONT_FAMILY" >/dev/null; then
    echo "==> $BASE_IMAGE has no '$FONT_FAMILY'; building $FONTS_IMAGE with the image's font packages"
    docker build -q -t "$FONTS_IMAGE" - <<DOCKERFILE
FROM $BASE_IMAGE
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      fonts-lobster fonts-lobstertwo fonts-dejavu fonts-noto-core \
 && fc-cache -f \
 && rm -rf /var/lib/apt/lists/*
DOCKERFILE
    IMAGE="$FONTS_IMAGE"
fi
echo "==> rendering with $IMAGE"

# name | -D overrides (;-separated) | expected: length width thickness has_text
CASES=(
    'defaults||100 10 6 0'
    'wave_text|style="wave_grip";text="COFFEE"|100 10 6 1'
    'short_thick|length=50;thickness=10;width=8;hinge_t=0.8;style="wave_grip"|50 8 10 0'
    'long_thin|length=200;thickness=4;width=20;hinge_t=2;latch_tol=0.6;text="A VERY LONG LABEL FOR A CLIP"|200 20 4 1'
)

status=0
for c in "${CASES[@]}"; do
    IFS='|' read -r name defs expect <<<"$c"
    read -ra expect_args <<<"$expect"
    args=()
    IFS=';' read -ra kv <<<"$defs"
    for d in "${kv[@]}"; do [ -n "$d" ] && args+=(-D "$d"); done
    # latch_tol is needed for the Y extent; pull it out of the overrides.
    tol=0.3
    [[ "$defs" =~ latch_tol=([0-9.]+) ]] && tol="${BASH_REMATCH[1]}"
    echo
    echo "==> $name ${args[*]:-}"
    start=$(date +%s%N)
    docker run --rm -v "$PWD":/w -w /w "$IMAGE" \
        openscad --backend=Manifold "${args[@]}" -o "$OUT/$name.3mf" model.scad >"$OUT/$name.log" 2>&1 \
        || { echo "  FAIL  openscad exited non-zero (see $OUT/$name.log)"; status=1; continue; }
    if grep -E 'WARNING|ERROR' "$OUT/$name.log"; then
        echo "  FAIL  OpenSCAD warnings (see $OUT/$name.log)"; status=1
    fi
    echo "    rendered in $(( ($(date +%s%N) - start) / 1000000 )) ms"
    python3 - "$OUT/$name.3mf" "${expect_args[@]}" "$tol" <<'PY' || status=1
import sys, math, zipfile, xml.etree.ElementTree as ET
from collections import Counter, defaultdict

NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
path = sys.argv[1]
length, width, t = map(float, sys.argv[2:5])
has_text = sys.argv[5] == "1"
tol = float(sys.argv[6])

# Must match the [Hidden] constants in model.scad.
OPEN, PIVOT_GAP, POST_W, TAB_W, TEXT_H = 20.0, 1.0, 2.4, 2.5, 0.6
R = t + PIVOT_GAP / 2
Lu = length - R - POST_W - TAB_W - tol
exp_y = R + R * math.cos(math.radians(OPEN)) + Lu * math.sin(math.radians(OPEN))
exp_z = width + (TEXT_H if has_text else 0)
EPS = 0.02

root = ET.fromstring(zipfile.ZipFile(path).read("3D/3dmodel.model"))
mats = [(b.get("name"), (b.get("displaycolor") or "")[:7]) for b in root.iter(NS + "base")]
verts = [(float(v.get("x")), float(v.get("y")), float(v.get("z"))) for v in root.iter(NS + "vertex")]
tris = [(int(t.get("v1")), int(t.get("v2")), int(t.get("v3")), int(t.get("p1") or 0))
        for t in root.iter(NS + "triangle")]
counts = Counter(t[3] for t in tris)
per_mat = defaultdict(set)
for tr in tris:
    per_mat[tr[3]].update(tr[:3])

failures = []


def check(ok, msg):
    print(("  PASS  " if ok else "  FAIL  ") + msg)
    if not ok:
        failures.append(msg)


def bbox(vs):
    xs, ys, zs = zip(*(verts[v] for v in vs))
    return min(xs), max(xs), min(ys), max(ys), min(zs), max(zs)


def components(mat=None):
    parent = {}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for tr in tris:
        if mat is not None and tr[3] != mat:
            continue
        for v in tr[:3]:
            parent.setdefault(v, v)
        for a, b in ((tr[0], tr[1]), (tr[1], tr[2])):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb
    return len({find(v) for v in parent})


named = [i for i, (n, _) in enumerate(mats) if n != "Default" and counts.get(i)]
for i in named:
    print("  material [%d] %s triangles=%d" % (i, mats[i][1], counts[i]))
x0, x1, y0, y1, z0, z1 = bbox(range(len(verts)))
print("  bbox X %.3f  Y %.3f  Z %.3f   (expected %.3f x %.3f x %.3f)"
      % (x1 - x0, y1 - y0, z1 - z0, length, exp_y, exp_z))

check(len(named) == (2 if has_text else 1),
      "%d non-empty material(s) besides Default (got %d)" % (2 if has_text else 1, len(named)))
check(counts.get(0, 0) == 0, "Default material carries no geometry (%d triangles)" % counts.get(0, 0))
check(abs(z0) <= 1e-3, "sits on z=0 (min z %.4f)" % z0)
check(abs((x1 - x0) - length) <= EPS, "X == length %.1f (got %.3f)" % (length, x1 - x0))
check(abs((y1 - y0) - exp_y) <= EPS, "Y == %.3f for the arm opened %g deg (got %.3f)" % (exp_y, OPEN, y1 - y0))
check(abs((z1 - z0) - exp_z) <= 1e-3, "Z == %.2f (got %.3f)" % (exp_z, z1 - z0))

if named:
    clip = named[0]
    cb = bbox(per_mat[clip])
    check(abs(cb[5] - width) <= 1e-3, "clip is %.1f mm tall (got %.3f)" % (width, cb[5]))
    # Over every triangle: with text, the clip's own top face has islands
    # (letter counters) that only connect through the text's vertices.
    n = components()
    check(n == 1, "clip%s one connected piece (%d component(s))"
          % (" and text are" if has_text else " is", n))
if has_text and len(named) == 2:
    tb = bbox(per_mat[named[1]])
    check(abs(tb[4] - width) <= 1e-3 and abs(tb[5] - width - TEXT_H) <= 1e-3,
          "text is raised %.1f mm on the top face (z %.3f .. %.3f)" % (TEXT_H, tb[4], tb[5]))
    check(tb[2] >= 0 and tb[3] <= t, "text stays on the lower arm, y %.2f .. %.2f within 0 .. %.1f"
          % (tb[2], tb[3], t))

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
