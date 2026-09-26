#!/usr/bin/env bash
# Render models/storage-box with the defaults and each major variation (lid
# type, footprint shape, dividers, handle, text, colours) and check every 3MF
# against the dimensions the parameters imply.
#
# The XML checking runs on the host: openscad/openscad:dev has no Python.
set -euo pipefail

cd "$(dirname "$0")"

BASE_IMAGE="${SCADBUDDY_OPENSCAD_IMAGE:-openscad/openscad:dev}"
FONTS_IMAGE="${SCADBUDDY_FONTS_IMAGE:-scadbuddy-verify:local}"
OUT="${OUT_DIR:-.verify}"
FONT_FAMILY="DejaVu Sans"

mkdir -p "$OUT"

# The lid text face comes from the Debian trixie font packages the ScadBuddy
# image installs. If the family is missing, derive a throwaway image that has
# it -- otherwise OpenSCAD silently falls back and text widths change.
IMAGE="$BASE_IMAGE"
if ! docker run --rm "$BASE_IMAGE" fc-list : family | grep -F "$FONT_FAMILY" >/dev/null; then
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
    'friction-text|lid_text="SCREWS"'
    'sliding-dividers-knob|lid_type="sliding";shape="rectangle";dividers_x=2;dividers_y=1;lid_text="BITS";handle="knob"'
    'sliding-portrait-notch|lid_type="sliding";inner_l=60;inner_w=100;handle="finger_notch";lid_text="AB"'
    'sliding-hexagon-fallback|lid_type="sliding";shape="hexagon"'
    'snap-hexagon-knob|lid_type="snap";shape="hexagon";handle="knob";lid_text="HEX"'
    'snap-round-3colour|lid_type="snap";shape="round";lid_color="#E0A030";lid_text="OVAL";dividers_x=1'
    'none-rectangle-dividers|lid_type="none";shape="rectangle";dividers_x=6;dividers_y=6'
    'portrait-hexagon-notch|shape="hexagon";inner_l=40;inner_w=120;handle="finger_notch"'
    'max-size|inner_l=250;inner_w=250;inner_h=200;wall=4;floor=4;lid_h=40;corner_r=30;dividers_x=6;dividers_y=6;handle="knob";lid_text="BIG"'
    'min-size|inner_l=20;inner_w=20;inner_h=10;wall=1.2;floor=1;lid_h=4;tolerance=0.6;lid_type="snap"'
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
    docker run --rm -v "$PWD":/w -w /w "$IMAGE" \
        openscad --backend=Manifold "${args[@]}" -o "$OUT/$name.3mf" model.scad \
        >"$OUT/$name.log" 2>&1 || { echo "  FAIL  openscad exited non-zero (see $OUT/$name.log)"; status=1; continue; }
    ms=$(( ($(date +%s%N) - start) / 1000000 ))
    printf '    render %d.%03ds\n' $((ms / 1000)) $((ms % 1000))
    python3 - "$OUT/$name.3mf" "$defs" <<'PY' || status=1
import math, sys, zipfile, xml.etree.ElementTree as ET
from collections import Counter

NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"

# Defaults, mirrored from model.scad.
p = dict(inner_l=80, inner_w=60, inner_h=40, wall=2, floor=1.6, shape="rounded",
         lid_type="friction", lid_h=10, tolerance=0.25, handle="none", lid_text="",
         box_color="#4A7FB5", lid_color="#4A7FB5", lid_text_color="#FFFFFF")
for kv in filter(None, sys.argv[2].split(";")):
    k, v = kv.split("=", 1)
    p[k] = v.strip('"') if v.startswith('"') else float(v)

L, W, H, wall, fl = p["inner_l"], p["inner_w"], p["inner_h"], p["wall"], p["floor"]
lid = p["lid_type"]
if lid == "sliding" and p["shape"] not in ("rectangle", "rounded"):
    lid = "friction"
plug = lid in ("friction", "snap")
knob = p["handle"] == "knob" and lid != "none"
hexl = wall / math.sin(math.radians(60))
box_x = L + 2 * (hexl if p["shape"] == "hexagon" and L >= W else wall)
box_y = W + 2 * (hexl if p["shape"] == "hexagon" and W > L else wall)
box_h = fl + H + (2 if lid == "sliding" else 0)
lip_d = min(6, H / 2)
gap, knob_d, knob_h, slide_t = 10, 18, 8, 2

if lid == "none":
    ex = box_x
    ez = box_h
elif plug:
    ex = 2 * box_x + gap + (gap + knob_d if knob else 0)
    ez = max(box_h, p["lid_h"] + lip_d, knob_h + fl if knob else 0)
else:
    g = min(max(wall / 2, p["tolerance"] + 0.4), wall - 0.4)
    sl, sw = max(L, W), min(L, W)
    if L >= W:
        lid_x = (sl / 2 + wall) + (sl / 2 + g - p["tolerance"])
    else:
        lid_x = 2 * (sw / 2 + g - p["tolerance"])
    ex = box_x + gap + lid_x
    ez = max(box_h, slide_t + (5 if knob else 0))
ey = box_y

colours = [p["box_color"]]
if lid != "none":
    colours.append(p["lid_color"])
    if p["lid_text"]:
        colours.append(p["lid_text_color"])
n_expected = len({c.upper() for c in colours})

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

# Lid text: on the bed face of a flipped plug lid, on the top face of a
# sliding lid -- visible from above once the lid is on the box either way.
if lid != "none" and p["lid_text"]:
    ti = [i for i in named if mats[i][1] == p["lid_text_color"].upper()]
    if ti:
        tz = [verts[v][2] for t in tris if t[3] == ti[0] for v in t[:3]]
        if plug:
            check(abs(min(tz)) <= 1e-3, "lid text reaches the bed face (min z %.3f)" % min(tz))
        else:
            check(abs(max(tz) - slide_t) <= 1e-3,
                  "lid text flush with the sliding lid top (max z %.3f)" % max(tz))
    else:
        check(False, "lid text material present")

sys.exit(1 if fails else 0)
PY
done

if [ "$status" -ne 0 ]; then
    echo; echo "FAILED"; exit 1
fi
echo; echo "OK"
