#!/usr/bin/env bash
# Render models/coaster-set with the defaults and each major variation (every
# pattern, every shape, face down, cork recess, holder, alternating colours,
# per-coaster monograms, SVG and PNG overlays, refused and missing overlay
# files, a set too big for the plate, the biggest coaster with its holder)
# and check each 3MF:
#
#   - no uncoloured geometry, the expected colour parts, and the colour parts
#     do not overlap (each colour rendered closed on its own through a
#     wrapper like ScadBuddy's adds up to the whole)
#   - on z=0, bounding box equal to the layout the parameters imply, and
#     inside the H2C plate
#   - inlays flush with the decorated face, on top (face up) or on the bed
#     (face down), and the solid volume equals the coasters' outline times
#     thickness minus the recess (so the inlays fill their pockets exactly)
#   - an overlay_file that is not a bare file name is refused before any
#     import()/surface() call; a missing file only drops the picture
#   - no OpenSCAD warnings other than for a deliberately missing file
#   - the overlay_type value "image_threshold" (the PNG choice's name before
#     #318 renamed it "png_threshold") still renders the same parts as the
#     new value, so saved presets and past outputs keep working
#
# The XML checking runs on the host with python3 and the standard library.
set -euo pipefail

cd "$(dirname "$0")"

BASE_IMAGE="${SCADBUDDY_OPENSCAD_IMAGE:-openscad/openscad:dev}"
FONTS_IMAGE="${SCADBUDDY_FONTS_IMAGE:-scadbuddy-verify:local}"
OUT="${OUT_DIR:-.verify}"
FONT_FAMILY="DejaVu Serif"
mkdir -p "$OUT"

# The monogram/text face comes from the Debian font packages the ScadBuddy
# image installs. If the family is missing, derive a throwaway image that has
# it -- otherwise OpenSCAD silently falls back and text widths change.
IMAGE="$BASE_IMAGE"
if ! docker run --rm --label "scadbuddy-verify=${SCADBUDDY_VERIFY_LABEL:-local}" "$BASE_IMAGE" fc-list : family | grep -F "$FONT_FAMILY" >/dev/null; then
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

scad() { docker run --rm --label "scadbuddy-verify=${SCADBUDDY_VERIFY_LABEL:-local}" -v "$PWD":/w -w /w "$IMAGE" openscad --backend=Manifold "$@"; }

# Values of a dropdown annotation: `name = "x"; // [a:Label, b, ...]` -> a b ...
options() {
    sed -n "s/^$1 = .*\/\/ \[\(.*\)\]\$/\1/p" model.scad | tr ',' '\n' | sed 's/:.*//; s/[" ]//g'
}
PATTERNS=$(options pattern)
SHAPES=$(options shape)
if [ -z "$PATTERNS" ] || [ -z "$SHAPES" ]; then
    echo "FAIL: could not read the dropdowns"; exit 1
fi

# An upper-case extension, to prove auto type detection ignores case. It has
# to be a bare name in the model directory; removed again on exit.
UPPER=verify-upper-case.PNG
cp sample-overlay.png "$UPPER"
trap 'rm -f verify-upper-case.PNG' EXIT

# name | -D overrides separated by ";"
CASES=('defaults|')
# Word-split on purpose: the positional parameters are the shapes list,
# rotated one step per pattern below.
# shellcheck disable=SC2086
set -- $SHAPES
for pat in $PATTERNS; do
    # Every pattern, cycling through the shapes.
    CASES+=("pattern-$pat-$1|pattern=\"$pat\";shape=\"$1\";count=2")
    set -- "${@:2}" "$1"
done
for s in $SHAPES; do
    CASES+=("recess-$s|shape=\"$s\";underside=\"recess\";pattern=\"monogram\";letters=\"R\";count=2")
done
CASES+=(
    'face-down-text|face="down";pattern="text";text="Cheers!";shape="rounded_square"'
    'monograms-holder-alternate|pattern="monogram";letters="ABCD";shape="hexagon";holder=true;alternate_colors=true;holder_color="#8D6E63"'
    'holder-round-6|holder=true;count=6;pattern="rings";holder_color="#8D6E63"'
    'no-border-plain|border_width=0;pattern="none";count=1'
    'overlay-svg|pattern="rings";overlay_file="sample-overlay.svg"'
    'overlay-png-auto|pattern="stripes";overlay_file="sample-overlay.png";count=1;shape="square"'
    "overlay-png-upper-ext|pattern=\"dots\";overlay_file=\"$UPPER\";count=1"
    'overlay-png-forced|overlay_file="sample-overlay.png";overlay_type="png_threshold";image_threshold=30;count=1'
    # The value before #318 renamed it; must render exactly as overlay-png-forced.
    'overlay-png-legacy-value|overlay_file="sample-overlay.png";overlay_type="image_threshold";image_threshold=30;count=1'
    'overlay-invert-face-down|overlay_file="sample-overlay.svg";overlay_invert=true;overlay_scale=80;overlay_rotation=20;face="down";count=2'
    'overlay-keeps-pattern|overlay_file="sample-overlay.svg";overlay_clears_pattern=false;pattern="checker";count=1'
    'overlay-missing|overlay_file="no-such-file.svg";count=1'
    'overlay-refused-parent|overlay_file="../coaster-set/sample-overlay.svg";count=1'
    'overlay-refused-absolute|overlay_file="/etc/hostname";count=1'
    'overlay-refused-subdir|overlay_file=".verify/sample-overlay.svg";count=1'
    'overlay-refused-dotfile|overlay_file=".gitignore";count=1'
    'overlay-refused-backslash|overlay_file="..\\sample-overlay.svg";count=1'
    'too-many-for-plate|count=12;size=150'
    'twelve-small-holder|count=12;size=60;holder=true;pattern="sunburst";holder_color="#8D6E63"'
    # Too big for two holder-sized cells: the coaster sits above the holder.
    'holder-biggest-stacked|count=4;size=150;holder=true;holder_clearance=3;gap=20;holder_color="#8D6E63"'
    'thin-recess-clamped|thickness=3;underside="recess";recess_depth=4;inlay_depth=1;count=2;shape="square"'
    'thin-recess-none|thickness=3;underside="recess";inlay_depth=2;count=2;shape="round"'
    'text-empty-note|pattern="text";text="";count=1'
    'monogram-blank-note|pattern="monogram";letters=" ";count=1'
    'fine-stripes-big|pattern="stripes";spacing=5;line_width=0.8;size=150;count=2;pattern_rotation=30'
)

: > "$OUT/cases.txt"
for c in "${CASES[@]}"; do
    name="${c%%|*}"
    # ONLY=<regex> runs a subset while iterating.
    if [ -n "${ONLY:-}" ] && ! grep -Eq "$ONLY" <<< "$name"; then continue; fi
    defs=()
    IFS=";" read -ra kvs <<< "${c#*|}"
    for d in "${kvs[@]}"; do [ -n "$d" ] && defs+=(-D "$d"); done
    start=$(date +%s%N)
    if ! scad "${defs[@]}" -o "$OUT/$name.3mf" model.scad >"$OUT/$name.log" 2>&1; then
        tail -20 "$OUT/$name.log"; echo "FAIL: $name did not render"; exit 1
    fi
    ms=$(( ($(date +%s%N) - start) / 1000000 ))
    printf '%s\t%s\t%s\n' "$name" "$ms" "${c#*|}" >> "$OUT/cases.txt"
    printf '==> %-28s %6d ms\n' "$name" "$ms"

    # Every colour on its own, closed (ScadBuddy's per-colour wrapper, §6.3).
    colours=$(python3 - "$OUT/$name.3mf" <<'PY'
import sys, zipfile, xml.etree.ElementTree as ET
NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
root = ET.fromstring(zipfile.ZipFile(sys.argv[1]).read("3D/3dmodel.model"))
used = {int(t.get("p1") or 0) for t in root.iter(NS + "triangle")}
mats = [b.get("displaycolor")[:7] for b in root.iter(NS + "base")]
print(" ".join(mats[i] for i in sorted(used) if i != 0))
PY
)
    [ "$(wc -w <<< "$colours")" -gt 1 ] || continue
    for col in $colours; do
        printf '_t = "%s";\nmodule color(c, alpha = 1) { if (is_string(c) && (c == _t || c == "%s")) children(); }\ninclude <../model.scad>\n' \
            "$col" "$(tr 'A-F' 'a-f' <<< "$col")" > "$OUT/wrap.scad"
        scad "${defs[@]}" --export-format binstl -o "$OUT/$name@${col#\#}.stl" "$OUT/wrap.scad" \
            >"$OUT/$name@${col#\#}.log" 2>&1 || { echo "FAIL: $name colour $col did not render"; exit 1; }
    done
done

python3 - "$OUT" <<'PY'
import math, os, re, struct, sys, zipfile, xml.etree.ElementTree as ET
from collections import Counter

NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
OUT = sys.argv[1]
BED_X, BED_Y = 300, 320   # H2C plate using both nozzles

# Defaults straight from the model source, so the checks follow the file.
DEFAULTS = {}
for line in open("model.scad"):
    m = re.match(r'^(\w+) = ("[^"]*"|[-\d.]+|true|false);', line)
    if m:
        v = m.group(2)
        DEFAULTS[m.group(1)] = v.strip('"') if v.startswith('"') else (v == "true" if v in ("true", "false") else float(v))

failures = []


def check(case, ok, msg):
    print(("  PASS  " if ok else "  FAIL  ") + msg)
    if not ok:
        failures.append("%s: %s" % (case, msg))


def load(path):
    root = ET.fromstring(zipfile.ZipFile(path).read("3D/3dmodel.model"))
    mats = [(b.get("name"), (b.get("displaycolor") or "")[:7].upper()) for b in root.iter(NS + "base")]
    V = [(float(v.get("x")), float(v.get("y")), float(v.get("z"))) for v in root.iter(NS + "vertex")]
    T = [(int(t.get("v1")), int(t.get("v2")), int(t.get("v3")), int(t.get("p1") or 0))
         for t in root.iter(NS + "triangle")]
    return mats, V, T


def tetvol(a, b, c):
    return (a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0])
            + a[2] * (b[0] * c[1] - b[1] * c[0])) / 6


def stl_volume(path):
    data = open(path, "rb").read()
    n = struct.unpack("<I", data[80:84])[0]
    vol = 0.0
    for k in range(n):
        f = struct.unpack("<12f", data[84 + 50 * k: 84 + 50 * k + 48])
        vol += tetvol(f[3:6], f[6:9], f[9:12])
    return vol


def area(shape, size, cr, o):
    """Area of the coaster outline grown by o (mirrors shape_2d)."""
    if shape == "round":
        return math.pi * ((size + 2 * o) / 2) ** 2
    if shape == "square":
        return (size + 2 * o) ** 2
    if shape == "hexagon":
        return math.sqrt(3) / 2 * (size + 2 * o) ** 2
    r = cr + o
    s = size + 2 * o
    return s * s - (4 - math.pi) * r * r if r > 0 else s * s


for line in open(os.path.join(OUT, "cases.txt")):
    name, ms, defs = line.rstrip("\n").split("\t")
    p = dict(DEFAULTS)
    for kv in filter(None, defs.split(";")):
        k, v = kv.split("=", 1)
        p[k] = v.strip('"') if v.startswith('"') else (v == "true" if v in ("true", "false") else float(v))
    print("\n%s (%s ms): %s" % (name, ms, defs or "(defaults)"))
    log = open(os.path.join(OUT, name + ".log")).read()

    # Layout, mirrored from model.scad.
    size, th, d, gap = p["size"], p["thickness"], p["inlay_depth"], p["gap"]
    ext_x = size / math.cos(math.radians(30)) if p["shape"] == "hexagon" else size
    ext_y = size
    hold = p["holder"]
    grow = 2 * (p["holder_clearance"] + 2.4)
    grow_x = grow / math.cos(math.radians(30)) if p["shape"] == "hexagon" else grow
    cell_x, cell_y = (ext_x + grow_x, ext_y + grow) if hold else (ext_x, ext_y)
    px, py, extra = cell_x + gap, cell_y + gap, 1 if hold else 0

    def pw(c, n): return min(c, n + extra) * px - gap
    def ph(c, n): return math.ceil((n + extra) / c) * py - gap
    n = int(p["count"])
    while True:
        cs = [c for c in range(1, n + extra + 1) if pw(c, n) <= BED_X and ph(c, n) <= BED_Y]
        if cs or n == 1:
            break
        n -= 1
    cols = min(cs, key=lambda c: max(pw(c, n), ph(c, n))) if cs else 1
    W, H = pw(cols, n), ph(cols, n)
    # Items sit centred in their cells; the holder's cell is its own size.
    def pos(i): return (-W / 2 + cell_x / 2 + (i % cols) * px, H / 2 - cell_y / 2 - (i // cols) * py)
    boxes = [(pos(i), ext_x / 2, ext_y / 2) for i in range(n)]
    if hold:
        boxes.append((pos(n), cell_x / 2, cell_y / 2))
    # Not even one coaster fits beside its holder in two holder-sized cells:
    # the coaster sits in its own-size cell above the holder, gap cut to fit.
    stacked = hold and not cs
    s_gap = min(gap, BED_Y - ext_y - cell_y)
    if stacked:
        W, H = max(ext_x, cell_x), ext_y + s_gap + cell_y
        boxes = [((0, H / 2 - ext_y / 2), ext_x / 2, ext_y / 2), ((0, -H / 2 + cell_y / 2), cell_x / 2, cell_y / 2)]
    bw = max(c[0] + hx for c, hx, hy in boxes) - min(c[0] - hx for c, hx, hy in boxes)
    bh = max(c[1] + hy for c, hx, hy in boxes) - min(c[1] - hy for c, hx, hy in boxes)
    face_down = p["face"] == "down" or p["underside"] == "recess"
    recess_max = max(0.0, th - d - 1.2)
    recess = min(p["recess_depth"], recess_max) if p["underside"] == "recess" else 0
    h_height = 2.4 + max(10, 0.7 * p["count"] * th)
    top = max(th, h_height if hold else 0)

    f = p["overlay_file"]
    safe = f != "" and "/" not in f and "\\" not in f and not f.startswith(".")
    overlay = safe and os.path.exists(f)

    mats, V, T = load(os.path.join(OUT, name + ".3mf"))
    counts = Counter(t[3] for t in T)
    named = [i for i, (nm, _) in enumerate(mats) if nm != "Default" and counts.get(i)]
    by_col = {mats[i][1]: i for i in named}
    xs, ys, zs = zip(*V)
    dx, dy, dz = max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)

    cols_expected = {p["coaster_color"].upper()}
    blank = (p["pattern"] == "monogram" and p["letters"].strip() == "") or (p["pattern"] == "text" and p["text"] == "")
    check(name, ("NOTE: pattern is" in log) == blank, "%s the empty-pattern note" % ("logs" if blank else "no"))
    if p["pattern"] != "none" and not blank:
        cols_expected.add(p["pattern_color"].upper())
    if p["border_width"] > 0:
        cols_expected.add(p["border_color"].upper())
    if overlay:
        cols_expected.add(p["overlay_color"].upper())
    if hold:
        cols_expected.add(p["holder_color"].upper())

    check(name, counts.get(0, 0) == 0, "Default material carries no geometry (%d triangles)" % counts.get(0, 0))
    check(name, set(by_col) == cols_expected,
          "colour parts %s (got %s)" % (sorted(cols_expected), sorted(by_col)))
    m = re.search(r"ECHO: COASTERS = \[(\d+), (\d+)", log)
    check(name, m is not None and int(m.group(1)) == n and int(m.group(2)) == cols,
          "%d coaster(s) in %d column(s)%s" % (n, cols, "" if n == p["count"] else " (only %d fit)" % n))
    if p["underside"] == "recess" and p["recess_depth"] > recess_max + 1e-9:
        check(name, "NOTE: recess reduced" in log and (recess > 0 or "no recess cut" in log),
              "the log says the recess was reduced to %.2f mm" % recess)
    if n < p["count"]:
        check(name, "NOTE: only %d coaster%s of" % (n, "" if n == 1 else "s") in log, "the log says how many fit")
    if stacked and s_gap < gap:
        check(name, "NOTE: gap reduced from" in log, "the log says the gap was cut to %.1f mm" % s_gap)
    check(name, abs(min(zs)) <= 1e-4, "sits on z=0 (min z %.4f)" % min(zs))
    check(name, abs(dx - bw) <= 0.02 and abs(dy - bh) <= 0.02, "plate %.2f x %.2f == %.2f x %.2f" % (dx, dy, bw, bh))
    check(name, abs(dz - top) <= 0.02, "height %.2f == %.2f" % (dz, top))
    check(name, dx <= BED_X and dy <= BED_Y, "fits the %dx%d plate" % (BED_X, BED_Y))

    # Volume: every coaster is outline x thickness minus its recess.
    whole = sum(tetvol(V[t[0]], V[t[1]], V[t[2]]) for t in T)
    if not hold:
        cr = min(p["corner_radius"], size / 2 - 1)
        exp = n * (area(p["shape"], size, cr, 0) * th - area(p["shape"], size, cr, -p["recess_rim"]) * recess)
        check(name, abs(whole - exp) <= 0.004 * exp,
              "solid volume %.0f mm3 == %.0f mm3 (%s, recess %.2f mm)" % (whole, exp, p["shape"], recess))

    if f and not safe:
        check(name, "overlay off" in log and "Can't open" not in log and "couldn't be opened" not in log,
              "unsafe overlay_file %r refused before any import()/surface()" % f)
    elif f and not overlay:
        check(name, "no-such-file" in log, "missing overlay file is reported and the render still completes")
    if not (f and not overlay and safe):
        check(name, "WARNING" not in log and "ERROR" not in log, "no OpenSCAD warnings or errors")

    if len(named) > 1:
        stls = ["%s@%s.stl" % (name, mats[i][1][1:]) for i in named]
        have = [s for s in stls if os.path.exists(os.path.join(OUT, s))]
        total = sum(stl_volume(os.path.join(OUT, s)) for s in have)
        check(name, len(have) == len(named), "each of the %d colours rendered on its own" % len(named))
        check(name, abs(total - whole) <= 1e-3 * whole,
              "colour parts do not overlap: %.1f mm3 summed, %.1f mm3 whole" % (total, whole))
        # Inlays flush with the decorated face: each inlay colour, closed,
        # spans exactly the inlay depth at the top (face up) or on the bed.
        if not p["alternate_colors"]:
            inlay = {p["pattern_color"].upper(), p["border_color"].upper(), p["overlay_color"].upper()}
            inlay -= {p["coaster_color"].upper()} | ({p["holder_color"].upper()} if hold else set())
            lo, hi = (0, d) if face_down else (th - d, th)
            for c in sorted(inlay & set(by_col)):
                path = os.path.join(OUT, "%s@%s.stl" % (name, c[1:]))
                data = open(path, "rb").read()
                k = struct.unpack("<I", data[80:84])[0]
                zz = [struct.unpack("<12f", data[84 + 50 * j: 84 + 50 * j + 48])[z]
                      for j in range(k) for z in (5, 8, 11)]
                check(name, abs(min(zz) - lo) < 1e-3 and abs(max(zz) - hi) < 1e-3,
                      "%s inlay spans z %.2f..%.2f, flush with the %s face" % (c, lo, hi, "bed" if face_down else "top"))

# #318: the PNG choice's value was "image_threshold" until it was renamed
# "png_threshold"; saved presets and past outputs still hold the old value.
# The legacy case must render the same colour parts, of the same volume, as
# its twin with the new value. (Without the fallback the old value is not
# "auto", so the PNG goes to import() instead and the picture is lost.)
def part_volumes(name):
    mats, V, T = load(os.path.join(OUT, name + ".3mf"))
    vols = Counter()
    for t in T:
        vols[mats[t[3]][1]] += tetvol(V[t[0]], V[t[1]], V[t[2]])
    return {c: v for c, v in vols.items() if abs(v) > 1e-9}


for old, new in [("overlay-png-legacy-value", "overlay-png-forced")]:
    if not all(os.path.exists(os.path.join(OUT, n + ".3mf")) for n in (old, new)):
        continue  # ONLY= skipped one of them
    print("\n%s vs %s" % (old, new))
    a, b = part_volumes(old), part_volumes(new)
    for key in ("overlay_color",):
        check(old, DEFAULTS[key].upper() in b, "%s renders the %s part %s" % (new, key, DEFAULTS[key].upper()))
    check(old, set(a) == set(b) and all(abs(a[c] - b[c]) <= 1e-6 * max(1.0, abs(b[c])) for c in b),
          "the legacy value renders the same parts as the new one (%s vs %s)"
          % (", ".join("%s %.1f" % kv for kv in sorted(a.items())),
             ", ".join("%s %.1f" % kv for kv in sorted(b.items()))))

if failures:
    print("\nFAILED: %d check(s)" % len(failures))
    for f in failures:
        print("  " + f)
    sys.exit(1)
print("\nOK: all cases passed")
PY
