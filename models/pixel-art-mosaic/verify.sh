#!/usr/bin/env bash
# Render models/pixel-art-mosaic with the defaults, every built-in pattern,
# the sample PNGs in brightness-band and threshold modes (exact and
# resampled), every background and mount choice, a mosaic too big for the
# plate, and missing / refused picture names, and check each 3MF:
#
#   - no uncoloured geometry; exactly the expected colour parts
#   - every colour rendered closed on its own (ScadBuddy's per-colour
#     wrapper, spec §6.3) adds up to the whole: the bands do not overlap
#   - each pixel colour's volume is (its pixel count) x pixel_size^2 x
#     pixel_height, the counts read from the pattern strings in model.scad or
#     decoded from the PNG itself: every band is its own part, and every
#     pixel landed in the band its brightness says
#   - on z=0, as tall as the layers imply, the bounding box the grid, frame
#     and mount imply, inside the 300 x 320 mm plate
#   - a refused name never reaches surface(); a missing file leaves only the
#     backing; "image_threshold" (the PNG choice's name before #318) renders
#     the same parts as "png_threshold"
#
# The checking runs on the host with python3 and the standard library.
set -euo pipefail

cd "$(dirname "$0")"

IMAGE="${SCADBUDDY_OPENSCAD_IMAGE:-openscad/openscad:dev}"
OUT="${OUT_DIR:-.verify}"
mkdir -p "$OUT"
echo "==> rendering with $IMAGE"

# ScadBuddy's file widget stores an upload beside model.scad under a
# generated bare name; stand one in. An upper-case extension proves nothing
# depends on the case.
UPLOAD=_scadbuddy_solid_asset_0123456789abcdef.PNG
cp sample-cat.png "$UPLOAD"
trap 'rm -f "$UPLOAD"' EXIT

scad() { docker run --rm --label "scadbuddy-verify=${SCADBUDDY_VERIFY_LABEL:-local}" -v "$PWD":/w -w /w "$IMAGE" openscad --backend=Manifold "$@"; }

options() {
    sed -n "s/^$1 = .*\/\/ \[\(.*\)\]\$/\1/p" model.scad | tr ',' '\n' | sed 's/:.*//; s/[" ]//g'
}

# name | -D overrides separated by ";"
CASES=('defaults|')
for p in $(options pattern); do
    CASES+=("pattern-$p|pattern=\"$p\"")
done
CASES+=(
    'heart-empty-bg-grooves|pattern="heart";background_mode="empty";pixel_gap=0.4'
    'star-cut-keyring|pattern="star";background_mode="cut";mount="keyring";frame_width=2'
    'smiley-magnets-4|pattern="smiley";mount="magnet";magnet_count=4;magnet_diameter=8;magnet_thickness=2'
    'no-frame-no-mount|pattern="blocky_face";frame_width=0;mount="none";pixel_size=8'
    'cat-5-bands-cut-magnet|image_file="sample-cat.png";bands=5;png_background="lightest";background_mode="cut";mount="magnet"'
    'cat-5-bands-fill|image_file="sample-cat.png";bands=5;png_background="lightest"'
    'cat-5-bands-invert|image_file="sample-cat.png";bands=5;invert=true'
    'cat-3-bands-darkest-bg|image_file="sample-cat.png";bands=3;png_background="darkest"'
    'cat-threshold|image_file="sample-cat.png";image_type="png_threshold";image_threshold=40'
    # The value before #318 renamed it; must render exactly as cat-threshold.
    'cat-threshold-legacy|image_file="sample-cat.png";image_type="image_threshold";image_threshold=40'
    "upload-name|image_file=\"$UPLOAD\";bands=5;png_background=\"lightest\";mount=\"none\""
    'sunset-8-bands|image_file="sample-sunset.png";bands=8;columns=24;rows=16'
    'sunset-resampled-grooves|image_file="sample-sunset.png";bands=8;columns=48;rows=32;pixel_gap=0.4;mount="keyring"'
    'too-big-for-plate|image_file="sample-sunset.png";columns=64;rows=64;pixel_size=15;bands=6'
    'missing-file|image_file="no-such-file.png"'
    'refused-parent|image_file="../pixel-art-mosaic/sample-cat.png"'
    'refused-absolute|image_file="/etc/hostname"'
    'refused-dotfile|image_file=".gitignore"'
)

: > "$OUT/cases.txt"
for c in "${CASES[@]}"; do
    name="${c%%|*}"
    if [ -n "${ONLY:-}" ] && ! grep -Eq "$ONLY" <<< "$name"; then continue; fi
    defs=()
    IFS=";" read -ra kvs <<< "${c#*|}"
    for d in "${kvs[@]}"; do [ -n "$d" ] && defs+=(-D "$d"); done
    rm -f "$OUT/$name"@*.stl
    start=$(date +%s%N)
    if ! scad "${defs[@]}" -o "$OUT/$name.3mf" model.scad >"$OUT/$name.log" 2>&1; then
        tail -20 "$OUT/$name.log"; echo "FAIL: $name did not render"; exit 1
    fi
    ms=$(( ($(date +%s%N) - start) / 1000000 ))
    printf '%s\t%s\t%s\n' "$name" "$ms" "${c#*|}" >> "$OUT/cases.txt"
    printf '==> %-28s %6d ms\n' "$name" "$ms"

    colours=$(python3 - "$OUT/$name.3mf" <<'PY'
import sys, zipfile, xml.etree.ElementTree as ET
NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
root = ET.fromstring(zipfile.ZipFile(sys.argv[1]).read("3D/3dmodel.model"))
used = {int(t.get("p1") or 0) for t in root.iter(NS + "triangle")}
mats = [b.get("displaycolor")[:7] for b in root.iter(NS + "base")]
print(" ".join(mats[i] for i in sorted(used) if i != 0))
PY
)
    for col in $colours; do
        printf '_t = "%s";\nmodule color(c, alpha = 1) { if (is_string(c) && (c == _t || c == "%s")) children(); }\ninclude <../model.scad>\n' \
            "$col" "$(tr 'A-F' 'a-f' <<< "$col")" > "$OUT/wrap.scad"
        scad "${defs[@]}" --export-format binstl -o "$OUT/$name@${col#\#}.stl" "$OUT/wrap.scad" \
            >"$OUT/$name@${col#\#}.log" 2>&1 || { echo "FAIL: $name colour $col did not render"; exit 1; }
    done
done

python3 - "$OUT" <<'PY'
import math, os, re, struct, sys, zipfile, zlib, xml.etree.ElementTree as ET
from collections import Counter

NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
OUT = sys.argv[1]
BED_X, BED_Y = 300, 320
SRC = open("model.scad").read()

DEFAULTS = {}
for line in SRC.splitlines():
    m = re.match(r'^(\w+) = ("[^"]*"|[-\d.]+|true|false);', line)
    if m:
        v = m.group(2)
        DEFAULTS[m.group(1)] = v.strip('"') if v.startswith('"') else (v == "true" if v in ("true", "false") else float(v))

# The built-in patterns, straight from the source.
PATTERNS = {m.group(1): re.findall(r'"([.1-8]+)"', m.group(2))
            for m in re.finditer(r'\["(\w+)", \[(.*?)\]\]', SRC, re.S)}

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
    return sum(tetvol(*[struct.unpack("<3f", data[84 + 50 * k + 12 * j: 96 + 50 * k + 12 * j]) for j in (1, 2, 3)])
               for k in range(n))


def png_grey(path):
    """8-bit greyscale or RGB(A), non-interlaced PNG -> rows of brightness 0..100."""
    data = open(path, "rb").read()
    pos, idat, w = 8, b"", 0
    while pos < len(data):
        n, typ = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        if typ == b"IHDR":
            w, h, depth, ctype = struct.unpack(">IIBB", body[:10])
            assert depth == 8 and ctype in (0, 2, 6), "unsupported PNG"
            bpp = {0: 1, 2: 3, 6: 4}[ctype]
        elif typ == b"IDAT":
            idat += body
        pos += 12 + n
    raw = zlib.decompress(idat)
    stride, prev, rows = w * bpp, bytearray(w * bpp), []
    for y in range(h):
        f, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b, c = prev[i], prev[i - bpp] if i >= bpp else 0
            p = a + b - c
            pred = [0, a, b, (a + b) // 2,
                    a if abs(p - a) <= abs(p - b) and abs(p - a) <= abs(p - c) else b if abs(p - b) <= abs(p - c) else c][f]
            line[i] = (line[i] + pred) & 255
        prev = line
        px = [line[i * bpp:i * bpp + 3] for i in range(w)]
        rows.append([100 * (p[0] if bpp == 1 else 0.2126 * p[0] + 0.7152 * p[1] + 0.0722 * p[2]) / 255 for p in px])
    return rows


for line in open(os.path.join(OUT, "cases.txt")):
    name, ms, defs = line.rstrip("\n").split("\t")
    p = dict(DEFAULTS)
    for kv in filter(None, defs.split(";")):
        k, v = kv.split("=", 1)
        p[k] = v.strip('"') if v.startswith('"') else (v == "true" if v in ("true", "false") else float(v))
    print("\n%s (%s ms): %s" % (name, ms, defs or "(defaults)"))
    log = open(os.path.join(OUT, name + ".log")).read()

    f = p["image_file"]
    safe = f != "" and "/" not in f and "\\" not in f and not f.startswith(".")
    use_image = safe
    loaded = safe and os.path.exists(f)
    colours = [p["color_%d" % i].upper() for i in range(1, 9)]

    # Pixel counts per colour index (0 = background), mirrored from model.scad.
    counts = Counter()
    exact = True
    if use_image:
        G, R = int(p["columns"]), int(p["rows"])
        thr = p["image_type"] in ("png_threshold", "image_threshold")
        N = 2 if thr else int(p["bands"])
        t = lambda b: -1 if b <= 0 else 101 if b >= N else (p["image_threshold"] if thr else 100 * b / N)
        bg = N if p["png_background"] == "lightest" else 1 if p["png_background"] == "darkest" else 0
        order = [b for b in range(1, N + 1) if b != bg]
        if p["invert"]:
            order.reverse()
        colour_of = {b: (0 if b == bg else order.index(b) + 1) for b in range(1, N + 1)}
        if loaded:
            img = png_grey(f)
            exact = len(img) == R and len(img[0]) == G
            if exact:
                for row in img:
                    for v in row:
                        b = next(b for b in range(1, N + 1) if t(b - 1) < v <= t(b))
                        counts[colour_of[b]] += 1
            used = {colour_of[b] for b in range(1, N + 1)}
        else:
            used = set()
    else:
        art = PATTERNS[p["pattern"]]
        G, R = len(art[0]), len(art)
        for row in art:
            for ch in row:
                counts[0 if ch == "." else int(ch)] += 1
        used = set(counts)
    # Colours that appear: exact counts where known, else all mapped bands
    # (a resampled picture can leave a band empty).
    present = {c for c in used if counts[c] > 0} if exact else None

    tab_reach = (p["hole_diameter"] / 2 + 1.2 + p["hole_diameter"] / 2 + 2.5) if p["mount"] in ("hanger", "keyring") else 0
    fw = p["frame_width"]
    ps = min(p["pixel_size"], (BED_X - 2 * fw - 2 * tab_reach) / G, (BED_Y - 2 * fw - 2 * tab_reach) / R)
    B = max(p["base_thickness"], p["magnet_thickness"] + 0.8) if p["mount"] == "magnet" else p["base_thickness"]
    top = B + p["pixel_height"] + (p["frame_raise"] if fw > 0 else 0)
    GW, GH = G * ps, R * ps

    mats, V, T = load(os.path.join(OUT, name + ".3mf"))
    tri = Counter(tt[3] for tt in T)
    named = [i for i, (nm, _) in enumerate(mats) if nm != "Default" and tri.get(i)]
    by_col = {mats[i][1]: i for i in named}
    xs, ys, zs = zip(*V)

    exp_cols = {p["base_color"].upper()}
    fill_bg = p["background_mode"] == "fill"
    if loaded or not use_image:
        cand = present if present is not None else used
        exp_cols |= {colours[c - 1] for c in cand if c > 0}
        if fill_bg and 0 in cand:
            exp_cols.add(p["background_color"].upper())
    check(name, tri.get(0, 0) == 0, "Default material carries no geometry (%d triangles)" % tri.get(0, 0))
    if exact:
        check(name, set(by_col) == exp_cols, "colour parts %s (got %s)" % (sorted(exp_cols), sorted(by_col)))
    else:
        check(name, set(by_col) <= exp_cols and p["base_color"].upper() in by_col and len(by_col) >= 3,
              "colour parts %s within %s" % (sorted(by_col), sorted(exp_cols)))
    m = re.search(r"ECHO: MOSAIC = \[(\d+), (\d+), ([\d.e-]+)", log)
    check(name, m is not None and int(m.group(1)) == G and int(m.group(2)) == R and abs(float(m.group(3)) - ps) < 1e-4,
          "grid %d x %d pixels of %.3f mm" % (G, R, ps))
    if ps < p["pixel_size"]:
        check(name, "NOTE: pixel_size reduced" in log, "the log says the pixels were shrunk to fit")
    check(name, abs(min(zs)) <= 1e-4, "sits on z=0 (min z %.4f)" % min(zs))
    check(name, abs(max(zs) - top) <= 1e-3, "height %.2f == %.2f" % (max(zs), top))
    check(name, max(xs) - min(xs) <= BED_X and max(ys) - min(ys) <= BED_Y, "fits the %dx%d plate" % (BED_X, BED_Y))
    if p["background_mode"] != "cut" and (loaded or not use_image):
        x0, x1, y0, y1 = -GW / 2 - fw, GW / 2 + fw, -GH / 2 - fw, GH / 2 + fw
        rt = p["hole_diameter"] / 2 + 2.5
        d = 1.2 + p["hole_diameter"] / 2
        if p["mount"] == "hanger":
            y1 = max(y1, GH / 2 + d + rt)
        if p["mount"] == "keyring":
            x0 = min(x0, -GW / 2 - d / math.sqrt(2) - rt)
            y1 = max(y1, GH / 2 + d / math.sqrt(2) + rt)
        got = (min(xs), max(xs), min(ys), max(ys))
        check(name, all(abs(a - b) <= 0.05 for a, b in zip(got, (x0, x1, y0, y1))),
              "bbox x %.2f..%.2f y %.2f..%.2f == x %.2f..%.2f y %.2f..%.2f" % (got + (x0, x1, y0, y1)))

    if f and not safe:
        check(name, "NOTE: image_file" in log and "Can't open" not in log and "couldn't be opened" not in log,
              "unsafe image_file %r refused before surface()" % f)
    elif f and not loaded:
        check(name, len(by_col) == 1, "missing picture leaves only the backing")
    else:
        check(name, "WARNING" not in log and "ERROR" not in log, "no OpenSCAD warnings or errors")

    # Closed parts: no overlap, and every colour holds exactly its pixels.
    whole = sum(tetvol(V[a], V[b], V[c]) for a, b, c, _ in T)
    vols = {}
    for i in named:
        path = os.path.join(OUT, "%s@%s.stl" % (name, mats[i][1][1:]))
        if os.path.exists(path):
            vols[mats[i][1]] = stl_volume(path)
    check(name, len(vols) == len(named), "each of the %d colours rendered closed on its own" % len(named))
    total = sum(vols.values())
    check(name, abs(total - whole) <= 1e-3 * whole,
          "colour parts do not overlap: %.1f mm3 summed, %.1f mm3 whole" % (total, whole))
    if exact and p["pixel_gap"] == 0 and (loaded or not use_image):
        cell = ps * ps * p["pixel_height"]
        per = Counter()
        for c, n in counts.items():
            if c == 0 and not fill_bg:
                continue
            per[p["background_color"].upper() if c == 0 else colours[c - 1]] += n
        for col, n in sorted(per.items()):
            got = vols.get(col, 0.0)
            check(name, abs(got - n * cell) <= 1e-3 * n * cell + 1e-6,
                  "%s holds %d pixels: %.1f mm3 == %.1f mm3" % (col, n, got, n * cell))

# #318: the legacy value renders the same parts as the new one.
def part_volumes(name):
    mats, V, T = load(os.path.join(OUT, name + ".3mf"))
    vols = Counter()
    for tt in T:
        vols[mats[tt[3]][1]] += tetvol(V[tt[0]], V[tt[1]], V[tt[2]])
    return {c: v for c, v in vols.items() if abs(v) > 1e-9}


old, new = "cat-threshold-legacy", "cat-threshold"
if all(os.path.exists(os.path.join(OUT, n + ".3mf")) for n in (old, new)):
    print("\n%s vs %s" % (old, new))
    a, b = part_volumes(old), part_volumes(new)
    check(old, len(b) >= 3 and set(a) == set(b) and all(abs(a[c] - b[c]) <= 1e-6 * max(1.0, abs(b[c])) for c in b),
          "the legacy value renders the same %d parts as the new one" % len(b))

if failures:
    print("\nFAILED: %d check(s)" % len(failures))
    for f in failures:
        print("  " + f)
    sys.exit(1)
print("\nOK: all cases passed")
PY
