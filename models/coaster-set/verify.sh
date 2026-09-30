#!/usr/bin/env bash
# Render models/coaster-set with the defaults and each major variation (every
# pattern, every shape, face down, cork recess, holder, alternating colours,
# per-coaster monograms, SVG and PNG overlays, refused and missing overlay
# files, a set too big for the plate, the biggest coaster with its holder,
# large sets with a holder) and check each 3MF:
#
#   - no uncoloured geometry, the expected colour parts, and the colour parts
#     do not overlap (each colour rendered closed on its own through a
#     wrapper like ScadBuddy's adds up to the whole)
#   - the plate split into its connected pieces (#422): exactly one piece per
#     coaster, of a coaster's footprint and thickness, plus the holder; no
#     two footprints closer than the gap (or the reduced gap the log gives);
#     all inside the 300 x 320 H2C plate, on z=0. The grid is not
#     re-derived; a few cases pin how many coasters fit (#411)
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
    # #411: coaster cells are coaster-sized and the holder goes beside, below or
    # at the end of the last row; with holder-sized cells these fitted 5 and 8.
    'holder-95-eight|count=8;holder=true'
    'holder-70-twelve|count=12;size=70;shape="square";holder=true;holder_color="#8D6E63"'
    # Too big to sit beside or below its holder with the gap: one coaster,
    # above the holder, gap cut to fit (#410).
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


def components(T):
    """Triangles grouped into connected pieces (union-find over shared vertices)."""
    parent = list(range(1 + max(max(t[:3]) for t in T)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    # Joining v0-v1 and v1-v2 already puts all three corners in one set;
    # a v2-v0 union would always be a no-op.
    for t in T:
        for a, b in ((t[0], t[1]), (t[1], t[2])):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb
    groups = {}
    for t in T:
        groups.setdefault(find(t[0]), set()).update(t[:3])
    return list(groups.values())


def piece_box(V, verts):
    """[x0, x1, y0, y1, z0, z1] of one piece."""
    xs, ys, zs = zip(*(V[i] for i in verts))
    return [min(xs), max(xs), min(ys), max(ys), min(zs), max(zs)]


def is_box(b, w, h, top):
    return (abs(b[1] - b[0] - w) <= 0.02 and abs(b[3] - b[2] - h) <= 0.02
            and abs(b[4]) <= 1e-4 and abs(b[5] - top) <= 0.02)


# Cases that pin how many coasters fit. A holder used to size every grid cell
# for itself (#411): 95 mm coasters fitted 5 with it, 70 mm ones 8.
EXPECT_FIT = {"holder-95-eight": 7, "holder-70-twelve": 12, "holder-biggest-stacked": 1}

# Where each holder case's holder goes, and the coaster grid (columns, rows)
# around it. Every holder case must be listed, so a tie between modes that
# resolves differently after an edit (twelve-small-holder: 3 columns with the
# holder beside and 4 with it below are equally square) cannot pass silently.
EXPECT_HOLDER = {
    "monograms-holder-alternate": ("below", 2, 2),
    "holder-round-6": ("below", 3, 2),
    "twelve-small-holder": ("beside", 3, 4),
    "holder-95-eight": ("row end", 3, 3),
    "holder-70-twelve": ("below", 4, 3),
    "holder-biggest-stacked": ("stacked", 1, 1),
}


def holder_mode(hb, cs):
    """Where the holder box hb sits relative to the coaster boxes cs."""
    if all(hb[3] < c[2] for c in cs):
        return "stacked" if len(cs) == 1 else "below"
    if all(hb[0] > c[1] for c in cs):
        return "beside"
    # Row end: the holder shares the last row, right of that row's coasters,
    # and every other coaster is in a row above it.
    same_row = [c for c in cs if c[2] < hb[3] and hb[2] < c[3]]
    if (same_row and all(hb[0] > c[1] for c in same_row)
            and all(c in same_row or c[2] >= hb[3] for c in cs)):
        return "row end"
    return "elsewhere"


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

    # Sizes of one coaster and of the holder. The layout itself is not
    # re-derived here: the rendered pieces are checked directly (#422).
    size, th, d, gap = p["size"], p["thickness"], p["inlay_depth"], p["gap"]
    ext_x = size / math.cos(math.radians(30)) if p["shape"] == "hexagon" else size
    ext_y = size
    hold = p["holder"]
    grow = 2 * (p["holder_clearance"] + 2.4)
    grow_x = grow / math.cos(math.radians(30)) if p["shape"] == "hexagon" else grow
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
    # The plate, split into its separate printed pieces (#422): every
    # coaster is one piece (its inlays touch it and are part of the same
    # solid), the holder another. Pieces that overlapped would have merged,
    # so the count of coaster-sized pieces is the number of coasters that
    # really sit apart on the plate.
    pieces = [piece_box(V, comp) for comp in components(T)]
    coasters = [b for b in pieces if is_box(b, ext_x, ext_y, th)]
    holders = [b for b in pieces if hold and is_box(b, ext_x + grow_x, ext_y + grow, h_height)]
    n = len(coasters)
    odd = len(pieces) - n - len(holders)
    check(name, odd == 0 and len(holders) == (1 if hold else 0),
          "%d separate pieces: %d coaster(s) of %.1f x %.1f%s%s"
          % (len(pieces), n, ext_x, ext_y, " + %d holder(s)" % len(holders) if hold else "",
             ", %d of another size (merged by an overlap?)" % odd if odd else ""))
    if name in EXPECT_FIT:
        check(name, n == EXPECT_FIT[name], "%d coaster(s) on the plate (want %d)" % (n, EXPECT_FIT[name]))
    m = re.search(r"ECHO: COASTERS = \[(\d+), (\d+), (\d+), ([-\d.e]+), ([-\d.e]+)", log)
    check(name, m is not None and int(m.group(1)) == n, "the model reports the %d coaster(s) it placed" % n)
    # Columns and rows of coasters, counted from the pieces' centres (the
    # holder is not a row or column of its own), against what the model reports.
    gcols = len({round((c[0] + c[1]) / 2, 1) for c in coasters})
    grows = len({round((c[2] + c[3]) / 2, 1) for c in coasters})
    check(name, m is not None and (int(m.group(2)), int(m.group(3))) == (gcols, grows),
          "coasters in %d column(s) x %d row(s), as the model reports" % (gcols, grows))
    if hold:
        h = re.search(r'ECHO: HOLDER = "([a-z ]+)"', log)
        want = EXPECT_HOLDER.get(name)
        check(name, want is not None, "the holder case pins its layout in EXPECT_HOLDER")
        if want and len(holders) == 1:
            got = holder_mode(holders[0], coasters)
            check(name, h is not None and h.group(1) == want[0] and got == want[0],
                  "holder %s (model says %s, the pieces show %s)" % (want[0], h and h.group(1), got))
            check(name, (gcols, grows) == want[1:], "grid %d x %d (want %d x %d)" % ((gcols, grows) + want[1:]))
    if p["underside"] == "recess" and p["recess_depth"] > recess_max + 1e-9:
        check(name, "NOTE: recess reduced" in log and (recess > 0 or "no recess cut" in log),
              "the log says the recess was reduced to %.2f mm" % recess)
    if n < p["count"]:
        check(name, "NOTE: only %d coaster%s of" % (n, "" if n == 1 else "s") in log, "the log says how many fit")
    else:
        check(name, n == p["count"] and "NOTE: only" not in log, "all %d coasters placed" % p["count"])
    # No two pieces' footprints overlap: every pair is at least the gap apart
    # along x or y, or the reduced gap the log admits to.
    g = re.search(r"NOTE: gap reduced from [\d.]+ to ([\d.]+) mm", log)
    min_gap = float(g.group(1)) if g else gap
    if g:
        check(name, min_gap < gap, "the log says the gap was cut to %.1f mm" % min_gap)
    seps = [max(a[0] - b[1], b[0] - a[1], a[2] - b[3], b[2] - a[3])
            for i, a in enumerate(pieces) for b in pieces[i + 1:]]
    check(name, all(s >= min_gap - 1e-3 for s in seps),
          "no two footprints overlap: pieces at least %.1f mm apart (closest %s)"
          % (min_gap, "%.2f" % min(seps) if seps else "-"))
    check(name, abs(min(zs)) <= 1e-4, "sits on z=0 (min z %.4f)" % min(zs))
    check(name, m is not None and abs(dx - float(m.group(4))) <= 0.02 and abs(dy - float(m.group(5))) <= 0.02,
          "plate %.2f x %.2f is the size the model reports" % (dx, dy))
    check(name, abs(dz - top) <= 0.02, "height %.2f == %.2f" % (dz, top))
    check(name, dx <= BED_X + 1e-3 and dy <= BED_Y + 1e-3, "all pieces inside the %dx%d plate" % (BED_X, BED_Y))

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
