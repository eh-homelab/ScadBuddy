#!/usr/bin/env bash
# Render models/battery-crate with the defaults and each major variation
# (every cell type, crate / solid block, stackable or not, handles, label) and
# check every 3MF against the dimensions the parameters imply.
#
# The XML checking runs on the host: openscad/openscad:dev has no Python.
set -euo pipefail

cd "$(dirname "$0")"

BASE_IMAGE="${SCADBUDDY_OPENSCAD_IMAGE:-openscad/openscad:dev}"
FONTS_IMAGE="${SCADBUDDY_FONTS_IMAGE:-scadbuddy-verify:local}"
OUT="${OUT_DIR:-.verify}"
FONT_FAMILY="DejaVu Sans"

mkdir -p "$OUT"

# The label face comes from the Debian trixie font packages the ScadBuddy
# image installs. If the family is missing, derive a throwaway image that has
# it -- otherwise OpenSCAD silently falls back and text widths change.
IMAGE="$BASE_IMAGE"
if docker image inspect "$FONTS_IMAGE" >/dev/null 2>&1; then
    IMAGE="$FONTS_IMAGE"
elif ! docker run --rm --label "scadbuddy-verify=${SCADBUDDY_VERIFY_LABEL:-local}" "$BASE_IMAGE" fc-list : family | grep -F "$FONT_FAMILY" >/dev/null; then
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

# name | -D overrides (;-separated) -- the expected values are computed from
# the same overrides by the checker below.
CASES=(
    'defaults|'
    'aaa|cell="AAA"'
    'c|cell="C"'
    'd|cell="D"'
    '9v|cell="9V"'
    '18650|cell="18650";label_text="LI-ION"'
    'cr2032|cell="CR2032";cols=8;rows=1'
    'solid-aa|style="solid_block"'
    'solid-9v-flat|cell="9V";style="solid_block";stackable=false'
    'solid-d-nohandles|cell="D";style="solid_block";handle_cutouts=false;cols=3;rows=2'
    'flat-crate|stackable=false;height_pct=30'
    'cr2032-too-low-for-label|cell="CR2032";stackable=false;height_pct=30'
    'one-cell|cols=1;rows=1;cell="AAA";clearance=1.5'
    'max|cell="D";cols=12;rows=8;height_pct=100;style="solid_block"'
    'd-crate-overflow|cell="D";cols=12;rows=8'
    'aa-12x8|cols=12;rows=8'
)

status=0
for c in "${CASES[@]}"; do
    name="${c%%|*}"
    defs="${c#*|}"
    args=()
    IFS=';' read -ra kv <<< "$defs"
    for d in "${kv[@]}"; do [ -n "$d" ] && args+=(-D "$d"); done
    echo "==> $name ${defs:-(defaults)}"
    start=$(date +%s%N)
    docker run --rm --label "scadbuddy-verify=${SCADBUDDY_VERIFY_LABEL:-local}" -v "$PWD":/w -w /w "$IMAGE" \
        openscad --backend=Manifold "${args[@]}" -o "$OUT/$name.3mf" model.scad \
        >"$OUT/$name.log" 2>&1 || { echo "  FAIL  openscad exited non-zero (see $OUT/$name.log)"; status=1; continue; }
    if grep -E 'WARNING|ERROR' "$OUT/$name.log"; then
        echo "  FAIL  OpenSCAD warnings (see $OUT/$name.log)"; status=1
    fi
    ms=$(( ($(date +%s%N) - start) / 1000000 ))
    printf '    render %d.%03ds\n' $((ms / 1000)) $((ms % 1000))
    python3 - "$OUT/$name.3mf" "$defs" <<'PY' || status=1
import sys, zipfile, xml.etree.ElementTree as ET
from collections import Counter

NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"

# Defaults and the cell table, mirrored from model.scad.
p = dict(cell="AA", clearance=0.6, cols=4, rows=2, height_pct=60, stackable="true",
         handle_cutouts="true", style="crate", label_text="",
         crate_color="#D23C2A", label_color="#FFFFFF")
for kv in filter(None, sys.argv[2].split(";")):
    k, v = kv.split("=", 1)
    p[k] = v.strip('"') if v.startswith('"') else (v if v in ("true", "false") else float(v))
CELLS = {"AAA": (10.5, 10.5, 44.5), "AA": (14.5, 14.5, 50.5), "C": (26.2, 26.2, 50.0),
         "D": (34.2, 34.2, 61.5), "9V": (26.5, 17.5, 48.5), "18650": (18.6, 18.6, 65.2),
         "CR2032": (3.2, 20.0, 20.0)}
a, b, length = CELLS[p["cell"]]
cx, cy = a + p["clearance"], b + p["clearance"]
crate = p["style"] == "crate"
stack = p["stackable"] == "true"
t = 1.2 if crate else 1.6
ow = 3.2 if stack else 2.4
end_t = ow + 5 if (p["handle_cutouts"] == "true" and not crate) else ow
# The model caps the grid to the most cells that fit the 300 x 320 mm plate.
BED_X, BED_Y = 300, 320
cols = max(1, min(int(p["cols"]), int((BED_X - 2 * end_t + t) // (cx + t))))
rows = max(1, min(int(p["rows"]), int((BED_Y - 2 * ow + t) // (cy + t))))
ex = cols * cx + (cols - 1) * t + 2 * end_t
ey = rows * cy + (rows - 1) * t + 2 * ow
grid_top = 1.6 + length * p["height_pct"] / 100
ez = 1.6 + length + 1 + 4 if stack else grid_top
rim_z = ez - 4 if stack else ez
z0 = 4 + 1.9 if stack else 0
l_bot, l_top = z0 + 2, rim_z - 2
has_label = (l_top - l_bot) >= 4 and (ex - 2 * end_t - 4) >= 10
n_expected = len({p["crate_color"].upper(), p["label_color"].upper()}) if has_label else 1

root = ET.fromstring(zipfile.ZipFile(sys.argv[1]).read("3D/3dmodel.model"))
mats = [(b.get("name"), (b.get("displaycolor") or "")[:7].upper()) for b in root.iter(NS + "base")]
verts = [(float(v.get("x")), float(v.get("y")), float(v.get("z")))
         for v in root.iter(NS + "vertex")]
tris = [(int(t.get("v1")), int(t.get("v2")), int(t.get("v3")), int(t.get("p1") or 0))
        for t in root.iter(NS + "triangle")]
counts = Counter(t[3] for t in tris)
xs, ys, zs = zip(*verts)
dx, dy, dz = max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)

fails = []
def check(ok, msg):
    print(("  PASS  " if ok else "  FAIL  ") + msg)
    if not ok:
        fails.append(msg)

TOL = 0.02
named = [i for i, (n, _) in enumerate(mats) if n != "Default" and counts.get(i)]
check(len(named) == n_expected,
      "%d non-empty material(s) besides Default (got %d: %s)"
      % (n_expected, len(named), ", ".join(mats[i][1] for i in named)))
dflt = sum(counts.get(i, 0) for i, (n, _) in enumerate(mats) if n == "Default")
check(dflt == 0, "Default material carries no geometry (got %d triangles)" % dflt)
check(abs(dx - ex) <= TOL, "X %.3f == %.3f" % (dx, ex))
check(abs(dy - ey) <= TOL, "Y %.3f == %.3f" % (dy, ey))
check(abs(dz - ez) <= TOL, "Z %.3f == %.3f" % (dz, ez))
check(abs(min(zs)) <= 1e-3, "sits on z=0 (min z %.4f)" % min(zs))
check(dx <= BED_X and dy <= BED_Y, "fits the %d x %d mm plate (%.1f x %.1f)" % (BED_X, BED_Y, dx, dy))

# Label: flush in the front (-Y) face, clear of the foot chamfer and the rim.
if has_label:
    li = [i for i in named if mats[i][1] == p["label_color"].upper()]
    if li:
        lv = [verts[v] for tr in tris if tr[3] == li[0] for v in tr[:3]]
        ly = [v[1] for v in lv]
        lz = [v[2] for v in lv]
        check(abs(min(ly) + ey / 2) <= 1e-3, "label flush with the front face (min y %.3f)" % min(ly))
        check(min(lz) >= l_bot - 1e-3 and max(lz) <= l_top + 1e-3,
              "label within z %.2f .. %.2f (got %.2f .. %.2f)" % (l_bot, l_top, min(lz), max(lz)))
    else:
        check(False, "label material present")

sys.exit(1 if fails else 0)
PY
done

if [ "$status" -ne 0 ]; then
    echo; echo "FAILED"; exit 1
fi
echo; echo "OK"
