#!/usr/bin/env bash
# Render models/alphabet-tiles with the defaults and each major variation, then
# check concrete facts:
#
#   - exactly the colour parts the parameters imply, nothing on the Default
#     material, on z=0, the height the parameters imply, and on the bed;
#   - one separate tile per non-space character, laid out in rows of the
#     width and height the parameters imply;
#   - every letter lies inside its tile's letter room (sized to fit, not
#     clipped), and every border ring inside its tile;
#   - rendered once per colour the way ScadBuddy builds its closed parts, the
#     colour parts do not overlap;
#   - magnets: a magnet of the nominal size fits its seat without touching
#     the tile; a slide-in magnet only meets the detent at the slot's mouth on
#     its way in, and a slightly smaller one meets nothing; the roof over the
#     magnet is at least 1.2 mm under the inlay.
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
# falls back silently and the letters would render in the wrong face.
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
if ! docker run --rm --label "scadbuddy-verify=${SCADBUDDY_VERIFY_LABEL:-local}" "$IMAGE" fc-list : family | grep -F "$FONT_FAMILY" >/dev/null; then
    echo "FAIL: $IMAGE has no '$FONT_FAMILY'" >&2
    exit 1
fi
echo "==> rendering with $IMAGE"

IMAGE="$IMAGE" OUT="$OUT" python3 - <<'PY'
import math, os, re, shlex, struct, subprocess, sys, zipfile
import xml.etree.ElementTree as ET
from collections import Counter

IMAGE, OUT = os.environ["IMAGE"], os.environ["OUT"]
NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
BED_X, BED_Y = 300, 320
TOL = 0.01

# Defaults, mirrored from model.scad.
D = dict(text="ABC123", font="DejaVu Sans:style=Bold", letter_scale=75, underline_6_9=True, shape="rounded_square",
         tile_size=30, thickness=5, corner_radius=5, edge_round=1.2, letter_style="inlay",
         letter_depth=0.6, gap=5, border=False, border_width=2, border_inset=1.5,
         magnet="none", mount="glue_in", magnet_clearance=0.2)
TILE, LETTER, BORDER = "#FFD54F", "#1565C0", "#E53935"
MAGNETS = {"6x2": (6, 2), "8x3": (8, 3)}
SKIN, ROOF = 0.6, 1.2

TRICKY = "WQg69&"
CASES = [
    ("defaults", {}),
    ("circle-border-slide", dict(shape="circle", text=TRICKY, border=True, magnet="8x3",
                                 mount="slide_in")),
    ("hexagon-border-slide", dict(shape="hexagon", text=TRICKY, border=True, magnet="6x2",
                                  mount="slide_in")),
    ("scalloped-border-glue", dict(shape="scalloped", text=TRICKY, border=True, magnet="8x3")),
    ("heart-border-slide", dict(shape="heart", text=TRICKY, border=True, magnet="8x3",
                                mount="slide_in")),
    ("raised-circle-glue", dict(shape="circle", letter_style="raised", letter_depth=1.2,
                                magnet="6x2", text="Mm", border=True)),
    # 24 of the biggest tiles: five rows of five, on the bed.
    ("max-heart", dict(shape="heart", tile_size=50, text="ABCDEFGHIJKLMNOPQRSTUVWX",
                       border=True, border_width=5, border_inset=5)),
    # Smallest, thinnest tile with the deepest inlay and the biggest magnet:
    # the thickness has to rise.
    ("min-thick-magnet", dict(tile_size=20, thickness=3, edge_round=2, letter_depth=2,
                              magnet="8x3", mount="slide_in", magnet_clearance=0.4,
                              letter_scale=95, text="W8")),
    ("square-sharp", dict(corner_radius=0, edge_round=0, text="hello world", gap=2,
                          letter_scale=40, magnet="6x2", magnet_clearance=0.1)),
    ("all-spaces", dict(text="   ")),
    # No magnet: the edge rounding and the deep inlay raise the thickness.
    ("thin-edge-raised", dict(thickness=3, edge_round=2, letter_depth=2)),
    # The smallest tiles with the widest, furthest-in border: the border is
    # narrowed and moved out so every tile still gets its letter.
    ("small-max-border", dict(tile_size=20, border=True, border_width=5, border_inset=5,
                              text="WQg8")),
    ("small-max-border-heart", dict(shape="heart", tile_size=20, border=True, border_width=5,
                                    border_inset=5, edge_round=2, text="W6")),
    # 24 big tiles with a wide gap run off the bed: the gap shrinks until
    # they fit: at 13-15 mm it is 4 columns x 6 rows (365+ mm deep), at 12 mm
    # 5 x 5 (298 mm).
    ("gap-shrinks", dict(tile_size=50, gap=15, text="ABCDEFGHIJKLMNOPQRSTUVWX")),
    ("serif-face", dict(text="Zoe", shape="scalloped", letter_style="raised",
                        font="DejaVu Serif:style=Bold")),
]

# Cases where the gap has to shrink to fit the bed, and what it becomes.
EXPECT_GAP = {"gap-shrinks": 12}


def scad(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return '"%s"' % v
    return repr(v)


def defines(ov):
    return " ".join("-D %s" % shlex.quote("%s=%s" % (k, scad(v))) for k, v in ov.items())


def docker(script):
    r = subprocess.run(["docker", "run", "--rm", "--label", "scadbuddy-verify=" + os.environ.get("SCADBUDDY_VERIFY_LABEL", "local"), "-v", os.getcwd() + ":/w", "-w", "/w",
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


def components_stl(tris):
    """Connected pieces of an STL, joining triangles that share a vertex position."""
    ids = {}
    idx = [tuple(ids.setdefault(tuple(round(c, 4) for c in v), len(ids)) for v in t)
           for t in tris]
    return components([(a, b, c, 0) for a, b, c in idx])


# ---- renders
with open("%s/wrap.scad" % OUT, "w") as f:
    f.write('_sb_t = "";\nmodule color(c, alpha = 1) { if (c == _sb_t) children(); }\n'
            "include <../model.scad>\n")

info = {}
for name, ov in CASES:
    p = dict(D, **ov)
    d = defines(ov)
    jobs = ["openscad --backend=Manifold %s -o %s/%s.3mf model.scad" % (d, OUT, name),
            "openscad --backend=Manifold %s -o %s/%s_all.stl model.scad" % (d, OUT, name)]
    for col in (TILE, LETTER, BORDER):
        jobs.append("openscad --backend=Manifold %s -D '_sb_t=\"%s\"' -o %s/%s_%s.stl %s/wrap.scad"
                    " 2>&1 | grep -v 'Current top level object is empty' >&2 || true"
                    % (d, col, OUT, name, col[1:], OUT))
    if p["magnet"] != "none":
        for pm in ("seat", "sweep", "sweep_small"):
            jobs.append("openscad --backend=Manifold %s -D 'probe_magnet=\"%s\"' -o %s/%s_%s.stl"
                        " model.scad" % (d, pm, OUT, name, pm))
    log = docker("\n".join(jobs))
    m = re.search(r"SB_TILES n=(\d+) cols=(\d+) rows=(\d+) gap=(\S+) thickness=(\S+) inset=(\S+) "
                  r"border_width=(\S+) "
                  r"margin=(\S+) room=(\S+) heart_k=(\S+)\"", log)
    if not m:
        print(log[-2000:])
        sys.exit("FAIL: no SB_TILES echo for %s" % name)
    info[name] = dict(n=int(m.group(1)), cols=int(m.group(2)), rows=int(m.group(3)),
                      gap=float(m.group(4)), T=float(m.group(5)), inset=float(m.group(6)),
                      bw=float(m.group(7)), margin=float(m.group(8)), room=float(m.group(9)),
                      heart_k=float(m.group(10)), log=log)

failures = []


def check(ok, msg):
    print(("  PASS  " if ok else "  FAIL  ") + msg)
    if not ok:
        failures.append(msg)


def near(a, b, tol=TOL):
    return abs(a - b) <= tol


for name, ov in CASES:
    p = dict(D, **ov)
    I = info[name]
    S = p["tile_size"]
    gap = EXPECT_GAP.get(name, p["gap"])
    check(near(I["gap"], gap), "gap between tiles %.0f mm (expected %.0f)" % (I["gap"], gap))
    chars = [c for c in p["text"] if c != " "]
    n = len(chars)
    print("\n[%s] %s" % (name, " ".join("%s=%s" % kv for kv in ov.items()) or "defaults"))

    # -- thickness the parameters imply
    inlay = p["letter_depth"] if p["letter_style"] == "inlay" else 0
    if p["magnet"] != "none":
        md, mh = MAGNETS[p["magnet"]]
        ph = mh + p["magnet_clearance"]
        needs = ph + ROOF + inlay + (SKIN if p["mount"] == "slide_in" else 0)
    else:
        needs = 0
    T = max(p["thickness"], needs, p["edge_round"] + inlay + 1)
    check(near(I["T"], T), "tile thickness %.2f (expected %.2f)" % (I["T"], T))
    # Every value the model changes is reported, and nothing else is.
    for note, want in (("NOTE: thickness raised", T > p["thickness"]),
                       ("NOTE: gap reduced", gap < p["gap"]),
                       ("NOTE: border set to", p["border"] and (I["bw"] < p["border_width"]
                                                             or not near(I["inset"], p["border_inset"], 1e-6)))):
        check((note in I["log"]) == want, "%s %s" % ("logs" if want else "no", note))
    if T > p["thickness"]:
        why = "mm magnet" if needs >= p["edge_round"] + inlay + 1 else "for the edge rounding and inlay"
        check("NOTE: thickness raised" in I["log"] and why in I["log"], "the thickness note says: %s" % why)
    if p["magnet"] != "none":
        top = (SKIN if p["mount"] == "slide_in" else 0) + ph
        check(T - inlay - top >= ROOF - 1e-6,
              "roof over the magnet %.2f mm >= %.1f" % (T - inlay - top, ROOF))

    # -- plate
    mats, verts, tris = read_3mf("%s/%s.3mf" % (OUT, name))
    used = Counter(t[3] for t in tris)
    named = {col for i, (nm, col) in enumerate(mats) if nm != "Default" and used.get(i)}
    want = {TILE} | ({LETTER} if n else set()) | ({BORDER} if p["border"] else set())
    check(named == want, "parts are %s (got %s)" % (sorted(want), sorted(named)))
    check(used.get(0, 0) == 0, "Default material has no triangles (got %d)" % used.get(0, 0))
    xs, ys, zs = zip(*verts)
    ztop = T + (p["letter_depth"] if p["letter_style"] == "raised" else 0)
    check(near(min(zs), 0) and near(max(zs), ztop),
          "z %.3f .. %.3f (expected 0 .. %.3f)" % (min(zs), max(zs), ztop))
    tiles = max(n, 1)
    check(I["n"] == n, "%d characters become tiles (echo %d)" % (n, I["n"]))
    # Count the tile colour's closed solid: in the plate mesh an inlaid tile's
    # top is cut into islands by its letter.
    ntiles = components_stl(read_stl("%s/%s_%s.stl" % (OUT, name, TILE[1:])))
    check(ntiles == tiles, "%d separate tiles (got %d)" % (tiles, ntiles))
    cols = max(1, (BED_X + gap) // (S + gap))
    used_cols = min(tiles, cols)
    rows = -(-tiles // cols)
    rb = S / 2 * 0.23
    shape_h = {"hexagon": S * math.cos(math.radians(30)),
               "scalloped": 2 * ((S / 2 - rb) * math.sin(math.radians(72)) + rb),
               "heart": S * ((1 / (2 * math.sqrt(2)) + 0.5) + 1 / math.sqrt(2))
               / (1 / math.sqrt(2) + 1)}.get(p["shape"], S)
    ex = used_cols * (S + gap) - gap
    ey = (rows - 1) * (S + gap) + shape_h
    sx, sy = max(xs) - min(xs), max(ys) - min(ys)
    # The heart's point is rounded off, so it comes up a little short.
    heart = p["shape"] == "heart"
    check(near(sx, ex, 0.05) and (ey - 1.5 <= sy <= ey + 0.05 if heart else near(sy, ey, 0.05)),
          "layout %.2f x %.2f (expected %.2f x %.2f: %d x %d tiles)" % (sx, sy, ex, ey,
                                                                     used_cols, rows))
    check(sx <= BED_X and sy <= BED_Y, "fits the %dx%d bed" % (BED_X, BED_Y))

    # -- letters inside their room, borders inside their tile
    def centre(j):
        return ((j % cols) * (S + gap) + S / 2, -(j // cols) * (S + gap) - S / 2)

    def owner(x, y):
        return min(range(tiles), key=lambda j: math.hypot(x - centre(j)[0], y - centre(j)[1]))

    room = I["room"]
    # A border never takes more than 60 % of the letter room.
    base = room + I["margin"]
    check(room >= 0.4 * base - 1e-6, "letter room %.2f mm is >= 40 %% of %.2f" % (room, base))
    if p["border"]:
        check(1 - 1e-6 <= I["bw"] <= p["border_width"] + 1e-6,
              "border %.2f mm wide (asked %s)" % (I["bw"], p["border_width"]))
    by = I["heart_k"] * 0.1 if p["shape"] == "heart" else 0
    if n:
        worst = 0.0
        for tri in read_stl("%s/%s_%s.stl" % (OUT, name, LETTER[1:])):
            for x, y, _ in tri:
                cx, cy = centre(owner(x, y))
                dx, dy = x - cx, y - cy - by
                if p["shape"] == "rounded_square":
                    worst = max(worst, abs(dx) - room, abs(dy) - room)
                else:
                    worst = max(worst, math.hypot(dx, dy) - room)
        check(worst <= 0.05, "every letter inside its room of %.2f mm (worst excess %.3f)"
              % (room, worst))
        # Every tile has its letter, and it is not a sliver: its ink spans at
        # least half the height letter_scale asks for (lower case is shorter).
        span = {}
        for tri in read_stl("%s/%s_%s.stl" % (OUT, name, LETTER[1:])):
            for x, y, _ in tri:
                j = owner(x, y)
                lo_, hi_ = span.get(j, (y, y))
                span[j] = (min(lo_, y), max(hi_, y))
        least = room * p["letter_scale"] / 100
        short = [j for j in range(n) if j not in span or span[j][1] - span[j][0] < least]
        check(not short, "every tile has a letter at least %.1f mm tall (short: %s)"
              % (least, short))
    if p["border"]:
        # Outermost border point, from the tile centre: as a box for the
        # square, across for the heart, as a radius for the rest.
        reach = 0.0
        for tri in read_stl("%s/%s_%s.stl" % (OUT, name, BORDER[1:])):
            for x, y, _ in tri:
                cx, cy = centre(owner(x, y))
                dx, dy = x - cx, y - cy
                reach = max(reach, max(abs(dx), abs(dy)) if p["shape"] == "rounded_square"
                            else abs(dx) if heart else math.hypot(dx, dy))
        check(reach <= S / 2 - I["inset"] + 0.05,
              "borders %.2f mm in from the tile edge (reach %.2f <= %.2f)"
              % (S / 2 - reach, reach, S / 2 - I["inset"]))

    # -- closed colour parts do not overlap
    total = sum(volume(read_stl("%s/%s_%s.stl" % (OUT, name, col[1:])))
                for col in want)
    union = volume(read_stl("%s/%s_all.stl" % (OUT, name)))
    check(abs(union - total) <= 0.001 * union,
          "colour parts do not overlap: union %.1f == sum of parts %.1f mm^3" % (union, total))

    # -- magnets
    if p["magnet"] != "none":
        seat = real_points(read_stl("%s/%s_seat.stl" % (OUT, name)))
        check(not seat, "a %s magnet fits its seat without touching (%d overlap vertices)"
              % (p["magnet"], len(seat)))
        if p["mount"] == "slide_in":
            sweep = real_points(read_stl("%s/%s_sweep.stl" % (OUT, name)))
            small = real_points(read_stl("%s/%s_sweep_small.stl" % (OUT, name)))
            depth = [y - centre(owner(x, y))[1] for x, y, _ in sweep]
            check(bool(sweep), "pushed in through the slot, the magnet meets the detent")
            lo = min(depth) if depth else 0
            check(bool(depth) and lo >= 0,
                  "and only at the mouth, in the top half of the tile (from y %+.2f)" % lo)
            check(not small, "a magnet 0.3 mm smaller slides in freely (%d overlap vertices)"
                  % len(small))

if failures:
    print("\nFAILED: %d check(s)" % len(failures))
    sys.exit(1)
print("\nOK: %d cases" % len(CASES))
PY
