#!/usr/bin/env bash
# Render models/desk-organizer with the defaults, every shape x pattern,
# every cup compartment count, single pieces, the stacked layout, extreme
# sizes and the clamps (tall thin cup, thin wall with a deep inlay, a tray
# wider than long), and check each 3MF:
#
#   - no uncoloured geometry; exactly the expected colour parts
#   - every colour rendered closed on its own (ScadBuddy's per-colour
#     wrapper, spec §6.3) adds up to the whole: no overlapping parts
#   - on z=0, the height and bounding box the parameters imply, inside the
#     300 x 320 mm plate
#   - wall thickness, measured: a horizontal ray through the back wall of
#     each piece (for stacked rings, at a trough and at a crest) crosses
#     exactly `wall` of material (at least `wall` for the rings)
#   - stability: the cup's footprint (the convex hull of its vertices on the
#     bed) is at least half its height wide in every direction, and the empty
#     cup's centre of mass sits over it with at least 25 degrees of tip
#     margin
#   - the name is flush: its text part reaches the front face exactly; it
#     is centred on the piece and at text_z, fits inside the face box
#     (face_w x face_h) and fills it in one direction; and, measured on
#     the mesh rather than from those formulas, the surface under both ends
#     of the name faces forward (within 40 degrees sideways); `NOTE:` lines report
#     every clamp
#
# The checking runs on the host with python3 and the standard library.
set -euo pipefail

cd "$(dirname "$0")"

BASE_IMAGE="${SCADBUDDY_OPENSCAD_IMAGE:-openscad/openscad:dev}"
FONTS_IMAGE="${SCADBUDDY_FONTS_IMAGE:-scadbuddy-verify:local}"
OUT="${OUT_DIR:-.verify}"
FONT_FAMILY="DejaVu Sans"
mkdir -p "$OUT"

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
echo "==> rendering with $IMAGE"

scad() { docker run --rm --label "scadbuddy-verify=${SCADBUDDY_VERIFY_LABEL:-local}" -v "$PWD":/w -w /w "$IMAGE" openscad --backend=Manifold "$@"; }

options() {
    sed -n "s/^$1 = .*\/\/ \[\(.*\)\]\$/\1/p" model.scad | tr ',' '\n' | sed 's/:.*//; s/[" ]//g'
}
SHAPES=$(options shape)
PATTERNS=$(options pattern)
COMPARTMENTS=$(options cup_compartments)
if [ -z "$SHAPES" ] || [ -z "$PATTERNS" ] || [ -z "$COMPARTMENTS" ]; then
    echo "FAIL: could not read the dropdowns"; exit 1
fi

# name | -D overrides separated by ";"
CASES=('defaults|')
for s in $SHAPES; do
    for p in $PATTERNS; do
        CASES+=("$s-$p|shape=\"$s\";pattern=\"$p\";pattern_colors=4")
    done
done
for n in $COMPARTMENTS; do
    CASES+=("cup-$n-compartments|pieces=\"cup\";cup_compartments=$n;name_on=\"cup\"")
done
CASES+=(
    'tray-only-6-colours|pieces="tray";pattern_colors=6;tray_compartments=6;tray_height=60;stripe_height=10'
    'rings-tray-only-blocks|pieces="tray";shape="stacked_rings";pattern="blocks";pattern_colors=6;tray_compartments=4'
    'stacked-layout|tray_length=250;cup_width=130;shape="hex";tray_width=150;cup_height=120'
    'tall-thin-cup-clamped|pieces="cup";cup_width=50;cup_height=200;name="Pens"'
    'tall-thin-rings-clamped|pieces="cup";shape="stacked_rings";cup_width=50;cup_height=200;ring_height=6'
    'thin-wall-deep-inlay|wall=1.2;text_depth=2;shape="square"'
    'thin-wall-inlay-fits|wall=1.2;text_depth=0.4;shape="square"'
    'wide-tray-clamped|pieces="tray";tray_length=80;tray_width=150;shape="hex";tray_compartments=2'
    'no-name|name="";pattern="solid"'
    'name-tray-only-long|name_on="tray";name="Workshop desk tools";shape="round"'
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
    printf '==> %-30s %6d ms\n' "$name" "$ms"

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
BED_X, BED_Y = 300, 320

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


def stl(path):
    data = open(path, "rb").read()
    n = struct.unpack("<I", data[80:84])[0]
    return [[struct.unpack("<3f", data[96 + 50 * k + 12 * j: 108 + 50 * k + 12 * j]) for j in range(3)] for k in range(n)]


def hits(tris, o, d):
    """Distances along the ray o + t d (t > 0) at which it crosses a triangle."""
    out = []
    for a, b, c in tris:
        e1 = [b[i] - a[i] for i in range(3)]
        e2 = [c[i] - a[i] for i in range(3)]
        p = [d[1] * e2[2] - d[2] * e2[1], d[2] * e2[0] - d[0] * e2[2], d[0] * e2[1] - d[1] * e2[0]]
        det = sum(e1[i] * p[i] for i in range(3))
        if abs(det) < 1e-12:
            continue
        s = [o[i] - a[i] for i in range(3)]
        u = sum(s[i] * p[i] for i in range(3)) / det
        if u < 0 or u > 1:
            continue
        q = [s[1] * e1[2] - s[2] * e1[1], s[2] * e1[0] - s[0] * e1[2], s[0] * e1[1] - s[1] * e1[0]]
        v = sum(d[i] * q[i] for i in range(3)) / det
        if v < 0 or u + v > 1:
            continue
        t = sum(e2[i] * q[i] for i in range(3)) / det
        if t > 0:
            out.append(t)
    out.sort()
    # Merge crossings on shared edges.
    return [t for i, t in enumerate(out) if i == 0 or t - out[i - 1] > 1e-4]


def first_hit_normal(tris, o, d):
    """Unit normal of the first triangle the ray o + t d (t > 0) meets."""
    best = None
    for a, b, c in tris:
        e1 = [b[i] - a[i] for i in range(3)]
        e2 = [c[i] - a[i] for i in range(3)]
        p = [d[1] * e2[2] - d[2] * e2[1], d[2] * e2[0] - d[0] * e2[2], d[0] * e2[1] - d[1] * e2[0]]
        det = sum(e1[i] * p[i] for i in range(3))
        if abs(det) < 1e-12:
            continue
        s = [o[i] - a[i] for i in range(3)]
        u = sum(s[i] * p[i] for i in range(3)) / det
        q = [s[1] * e1[2] - s[2] * e1[1], s[2] * e1[0] - s[0] * e1[2], s[0] * e1[1] - s[1] * e1[0]]
        v = sum(d[i] * q[i] for i in range(3)) / det
        if u < 0 or v < 0 or u + v > 1:
            continue
        t = sum(e2[i] * q[i] for i in range(3)) / det
        if t > 0 and (best is None or t < best[0]):
            n = [e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0]]
            ln = math.sqrt(sum(x * x for x in n))
            best = (t, [x / ln for x in n])
    return best[1] if best else None


def hull(pts):
    pts = sorted(set(pts))
    if len(pts) < 3:
        return pts
    def cross(o, a, b): return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lo, hi = [], []
    for p in pts:
        while len(lo) >= 2 and cross(lo[-2], lo[-1], p) <= 0: lo.pop()
        lo.append(p)
    for p in reversed(pts):
        while len(hi) >= 2 and cross(hi[-2], hi[-1], p) <= 0: hi.pop()
        hi.append(p)
    return lo[:-1] + hi[:-1]


for line in open(os.path.join(OUT, "cases.txt")):
    name, ms, defs = line.rstrip("\n").split("\t")
    p = dict(DEFAULTS)
    for kv in filter(None, defs.split(";")):
        k, v = kv.split("=", 1)
        p[k] = v.strip('"') if v.startswith('"') else (v == "true" if v in ("true", "false") else float(v))
    print("\n%s (%s ms): %s" % (name, ms, defs or "(defaults)"))
    log = open(os.path.join(OUT, name + ".log")).read()

    # Mirrored from model.scad.
    rings = p["shape"] == "stacked_rings"
    rr = p["ring_height"] / 2
    ring_count = lambda h: max(2, math.floor(h / p["ring_height"] + 0.5))  # OpenSCAD round()
    W, L = p["cup_width"], p["tray_length"]
    TW = min(p["tray_width"], L)
    foot = W - 2 * (0.586 * rr if rings else 0)
    hmax = 2 * foot
    ask = min(p["cup_height"], hmax)
    if rings:
        cup_h = ring_count(ask) * p["ring_height"]
        if cup_h > hmax:
            cup_h -= p["ring_height"]
    else:
        cup_h = ask
    tray_h = ring_count(p["tray_height"]) * p["ring_height"] if rings else p["tray_height"]
    td = min(p["text_depth"], p["wall"] - 0.8)
    nc = 1 if p["pattern"] == "solid" else int(p["pattern_colors"])
    show_cup, show_tray = p["pieces"] != "tray", p["pieces"] != "cup"
    cup_x = W / math.cos(math.radians(30)) if p["shape"] == "hex" else W
    row = not (show_cup and show_tray) or cup_x + 10 + L <= BED_X
    if not (show_cup and show_tray):
        cup_at = tray_at = (0, 0)
    elif row:
        cup_at, tray_at = (-(cup_x + 10 + L) / 2 + cup_x / 2, 0), ((cup_x + 10 + L) / 2 - L / 2, 0)
    else:
        cup_at, tray_at = (0, (W + 10 + TW) / 2 - W / 2), (0, -(W + 10 + TW) / 2 + TW / 2)
    has_name = p["name"] not in ("", " ")
    named = lambda k: has_name and p["name_on"] in ("both", k)

    m = re.search(r"ECHO: ORGANIZER = \[([^\]]*)\]", log)
    echo = [float(x) for x in m.group(1).split(",")] if m else []
    check(name, len(echo) == 7 and abs(echo[0] - cup_h) < 1e-6 and abs(echo[2] - tray_h) < 1e-6 and abs(echo[3] - td) < 1e-6,
          "cup %.1f mm, tray %.1f mm tall, inlay %.2f mm deep" % (cup_h, tray_h, td))
    if show_cup and p["cup_height"] > hmax:
        check(name, "NOTE: cup_height reduced" in log, "the log says the cup was shortened for stability")
    if has_name and p["name_on"] != "none" and td < p["text_depth"] - 1e-6:
        check(name, "NOTE: text_depth reduced" in log, "the log says the inlay was made shallower")
    else:
        check(name, "NOTE: text_depth reduced" not in log, "no text_depth note when the inlay depth is unchanged")
    if show_tray and TW < p["tray_width"]:
        check(name, "NOTE: tray_width reduced" in log, "the log says the tray was made narrower")
    check(name, "WARNING" not in log and "ERROR" not in log, "no OpenSCAD warnings or errors")

    mats, V, T = load(os.path.join(OUT, name + ".3mf"))
    counts = Counter(t[3] for t in T)
    named_mats = [i for i, (nm, _) in enumerate(mats) if nm != "Default" and counts.get(i)]
    by_col = {mats[i][1]: i for i in named_mats}
    xs, ys, zs = zip(*V)

    # Expected colours: the stripes/rings/panels each piece actually has.
    colours = [p["color_%d" % i].upper() for i in range(1, 7)]
    exp = set()
    for kind, show, h in (("cup", show_cup, cup_h), ("tray", show_tray, tray_h)):
        if not show:
            continue
        if p["pattern"] == "stripes":
            n = ring_count(h) if rings else math.ceil(h / p["stripe_height"])
            exp |= {colours[k % nc] for k in range(n)}
        else:
            exp |= set(colours[:nc])
        if named(kind):
            exp.add(p["text_color"].upper())
    check(name, counts.get(0, 0) == 0, "Default material carries no geometry (%d triangles)" % counts.get(0, 0))
    check(name, set(by_col) == exp, "colour parts %s (got %s)" % (sorted(exp), sorted(by_col)))

    # Bounding box and plate.
    boxes = []
    if show_cup:
        boxes.append((cup_at[0] - cup_x / 2, cup_at[0] + cup_x / 2, cup_at[1] - W / 2, cup_at[1] + W / 2))
    if show_tray:
        boxes.append((tray_at[0] - L / 2, tray_at[0] + L / 2, tray_at[1] - TW / 2, tray_at[1] + TW / 2))
    ex = (min(b[0] for b in boxes), max(b[1] for b in boxes), min(b[2] for b in boxes), max(b[3] for b in boxes))
    got = (min(xs), max(xs), min(ys), max(ys))
    check(name, all(abs(a - b) <= 0.05 for a, b in zip(got, ex)),
          "bbox x %.2f..%.2f y %.2f..%.2f == x %.2f..%.2f y %.2f..%.2f" % (got + ex))
    top = max(cup_h if show_cup else 0, tray_h if show_tray else 0)
    check(name, abs(min(zs)) <= 1e-4 and abs(max(zs) - top) <= 1e-3, "z 0..%.2f == 0..%.2f" % (max(zs), top))
    check(name, got[1] - got[0] <= BED_X and got[3] - got[2] <= BED_Y, "fits the %dx%d plate" % (BED_X, BED_Y))

    # Closed colour parts do not overlap.
    whole = sum(tetvol(V[a], V[b], V[c]) for a, b, c, _ in T)
    parts = {}
    if len(named_mats) > 1:
        for i in named_mats:
            path = os.path.join(OUT, "%s@%s.stl" % (name, mats[i][1][1:]))
            if os.path.exists(path):
                parts[mats[i][1]] = stl(path)
        total = sum(sum(tetvol(*t) for t in tris) for tris in parts.values())
        check(name, len(parts) == len(named_mats), "each of the %d colours rendered closed on its own" % len(named_mats))
        check(name, abs(total - whole) <= 1e-3 * whole,
              "colour parts do not overlap: %.1f mm3 summed, %.1f mm3 whole" % (total, whole))

    tris = [(V[a], V[b], V[c]) for a, b, c, _ in T]

    # Wall thickness: a ray from behind each piece, towards its front.
    def wall_at(cx, cy, back, z):
        hs = hits(tris, (cx, cy + back + 5, z), (0, -1, 0))
        return hs[1] - hs[0] if len(hs) >= 2 else None

    for kind, show, (cx, cy), back, h in (("cup", show_cup, cup_at, W / 2, cup_h),
                                           ("tray", show_tray, tray_at, TW / 2, tray_h)):
        if not show:
            continue
        n = int(p["tray_compartments"]) if kind == "tray" else 1
        # Clear of any divider: tray dividers sit at multiples of L/n, cup
        # dividers radiate from the axis (one along +Y for 2, 3, 4 and 6).
        off = L / (4 * n) if kind == "tray" and n % 2 == 0 else 0
        if kind == "cup" and int(p["cup_compartments"]) > 1:
            off = W / 8
        x = cx + off
        if rings:
            # A trough (thinnest) and a crest, above the floor.
            k = max(1, ring_count(h) // 2)
            z_trough = k * p["ring_height"] - 0.0636 * rr
            z_crest = rr + k * p["ring_height"]
            tt, tc = wall_at(x, cy, back, z_trough), wall_at(x, cy, back, z_crest)
            check(name, tt is not None and tt >= p["wall"] - 0.05,
                  "%s wall at a trough %.2f mm >= %.2f" % (kind, tt or 0, p["wall"]))
            check(name, tc is not None and tc >= p["wall"] + rr * 0.6,
                  "%s wall at a crest %.2f mm (ring adds %.1f)" % (kind, tc or 0, tc - p["wall"] if tc else 0))
        else:
            z = (p["floor_thickness"] + h) / 2
            t = wall_at(x, cy, back, z)
            # Off the axis of a round cup the ray crosses the wall as a chord.
            want = p["wall"]
            if kind == "cup" and p["shape"] == "round" and off:
                want = math.sqrt((W / 2) ** 2 - off ** 2) - math.sqrt((W / 2 - p["wall"]) ** 2 - off ** 2)
            check(name, t is not None and abs(t - want) <= 0.05,
                  "%s wall %.3f mm == %.3f (%.2f mm wall)" % (kind, t or 0, want, p["wall"]))

    # Stability: footprint and tip margin of the cup.
    if show_cup:
        cx, cy = cup_at
        near = lambda v: abs(v[0] - cx) <= cup_x / 2 + 1 and abs(v[1] - cy) <= W / 2 + 1
        foot_pts = hull([(v[0], v[1]) for v in V if abs(v[2]) < 1e-4 and near(v)])
        widths = []
        for i in range(len(foot_pts)):
            a, b = foot_pts[i], foot_pts[(i + 1) % len(foot_pts)]
            ln = math.hypot(b[0] - a[0], b[1] - a[1])
            nx, ny = (b[1] - a[1]) / ln, -(b[0] - a[0]) / ln
            proj = [q[0] * nx + q[1] * ny for q in foot_pts]
            widths.append(max(proj) - min(proj))
        fw = min(widths)
        check(name, fw >= cup_h / 2 - 0.1, "cup footprint %.1f mm wide >= half its %.1f mm height" % (fw, cup_h))
        # Centre of mass of the empty cup (all colours) and its tip margin.
        cup_tris = [t for t in tris if all(near(v) for v in t)]
        vol = sum(tetvol(*t) for t in cup_tris)
        com = [sum(tetvol(*t) * (t[0][k] + t[1][k] + t[2][k]) / 4 for t in cup_tris) / vol for k in range(3)]
        d = min(abs((b[1] - a[1]) * (com[0] - a[0]) - (b[0] - a[0]) * (com[1] - a[1])) / math.hypot(b[0] - a[0], b[1] - a[1])
                for a, b in zip(foot_pts, foot_pts[1:] + foot_pts[:1]))
        ang = math.degrees(math.atan2(d, com[2]))
        check(name, ang >= 25, "cup tips only past %.1f degrees (centre of mass %.1f mm up, %.1f mm from the edge)" % (ang, com[2], d))

    # Flush name: the text part reaches the front face of each named piece.
    tc = p["text_color"].upper()
    if tc in parts:
        tv = [v for t in parts[tc] for v in t]
        for kind, show, (cx, cy), front in (("cup", show_cup, cup_at, W / 2), ("tray", show_tray, tray_at, TW / 2)):
            if not (show and named(kind)):
                continue
            mine = [v for v in tv if abs(v[0] - cx) <= (cup_x if kind == "cup" else L) / 2 + 1 and abs(v[1] - cy) <= front + 1]
            check(name, len(mine) > 0 and abs(min(v[1] for v in mine) - (cy - front)) <= 1e-3,
                  "%s name flush with the front face (y %.3f == %.3f)" % (kind, min(v[1] for v in mine) if mine else 0, cy - front))
            if not mine:
                continue
            # Fitted to the face (face_w / face_h / text_z in model.scad):
            # centred on the piece and on text_z, never wider than face_w or
            # taller than face_h, and filling one of the two.
            h = cup_h if kind == "cup" else tray_h
            if kind == "cup":
                fwid = (W / math.sqrt(3) * 0.8 if p["shape"] == "hex" else (W - 8) * 0.9 if p["shape"] == "square" else W * 0.6)
            else:
                c = min(TW / 2 * math.tan(math.radians(30)), L / 4)
                fwid = ((L - 2 * c) * 0.85 if p["shape"] == "hex" else (L - 8) * 0.9 if p["shape"] == "square"
                        else max((L - TW) * 0.9, TW * 0.6))
            fhgt = min(p["text_size"], 0.8 * p["ring_height"] if rings else h * (0.4 if kind == "cup" else 0.6))
            tz = rr + (ring_count(h) // 2) * p["ring_height"] if rings else h / 2
            x0, x1 = min(v[0] for v in mine), max(v[0] for v in mine)
            z0, z1 = min(v[2] for v in mine), max(v[2] for v in mine)
            # valign=center centres the glyphs' box, and the fit bar is centred
            # on it, so the text's middle is text_z; allow for glyph asymmetry.
            check(name, abs((x0 + x1) / 2 - cx) <= 0.05 * fwid and abs((z0 + z1) / 2 - tz) <= 0.15 * fhgt,
                  "%s name centred at x %.1f z %.1f (piece x %.1f, text_z %.1f)" % (kind, (x0 + x1) / 2, (z0 + z1) / 2, cx, tz))
            # Independent of those formulas: measured on the mesh, both ends
            # of the name (and its middle) sit on a part of the front
            # surface that faces forward, within 40 degrees of straight
            # ahead in plan -- never round a corner or a curved end.
            for xe in (x0 + 0.05, (x0 + x1) / 2, x1 - 0.05):
                for ze in (z0 + 0.3, z1 - 0.3):
                    nrm = first_hit_normal(tris, (xe, cy - front - 5, ze), (0, 1, 0))
                    # Sideways only: a ring's crest tilts up and down by design.
                    side = math.degrees(math.atan2(abs(nrm[0]), -nrm[1])) if nrm and nrm[1] < 0 else 180
                    check(name, side <= 40,
                          "%s front faces forward under the name at x %.1f z %.1f (%.0f degrees sideways)"
                          % (kind, xe, ze, side))
            check(name, x1 - x0 <= fwid + 0.05 and z1 - z0 <= fhgt + 0.05
                  and (x1 - x0 >= 0.97 * fwid or z1 - z0 >= 0.6 * fhgt),
                  "%s name %.1f x %.1f mm fits the %.1f x %.1f mm face" % (kind, x1 - x0, z1 - z0, fwid, fhgt))

if failures:
    print("\nFAILED: %d check(s)" % len(failures))
    for f in failures:
        print("  " + f)
    sys.exit(1)
print("\nOK: all cases passed")
PY
