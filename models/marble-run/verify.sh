#!/usr/bin/env bash
# Render models/marble-run with the defaults and every piece type, plus the
# edge cases of the sliders, then check concrete facts: two colour parts and
# nothing uncoloured, the bounding box the parameters imply, that everything
# sits on z=0, the z range of each closed colour part (rendered one colour at
# a time, the way ScadBuddy builds its parts), the peg/socket fit, and the
# marble path: at points sampled every millimetre along the channel, the entry
# drop and the exit hole, no surface of the piece may come closer to the
# marble's centre line than (marble_d + channel_clearance) / 2 — and the floor
# must actually be there, touching that radius.
#
# The checking runs on the host with python3 and the standard library only:
# the OpenSCAD image has no Python.
set -euo pipefail

cd "$(dirname "$0")"

BASE_IMAGE="${SCADBUDDY_OPENSCAD_IMAGE:-openscad/openscad:dev}"
FONTS_IMAGE="${SCADBUDDY_FONTS_IMAGE:-scadbuddy-verify:local}"
OUT="${OUT_DIR:-.verify}"

mkdir -p "$OUT"

# Render in the ScadBuddy image (built from the base image with the same font
# packages when it is missing). This model has no text, so any OpenSCAD with
# the Manifold backend would do; the image is used for consistency.
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
echo "==> rendering with $IMAGE"

IMAGE="$IMAGE" OUT="$OUT" python3 - <<'PY'
import math, os, re, shlex, struct, subprocess, sys, zipfile
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict

IMAGE, OUT = os.environ["IMAGE"], os.environ["OUT"]
NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
TOL = 0.01
PIECE, ACCENT = "#29B6F6", "#FFEE58"

# Defaults and hidden constants, mirrored from model.scad.
D = dict(type="spiral", tile=50, height=30, marble_d=16, channel_clearance=2,
         slope=6, peg=True, peg_clearance=0.3)
WALL, MIN_FLOOR, RIM_H, PEG_D, PEG_H, BOWL_DIP, SPIRAL_STEPS = 1.6, 1.6, 2, 5, 4, 3, 36

CASES = [
    ("defaults", {}),
    ("straight_drop", dict(type="straight_drop")),
    ("zigzag", dict(type="zigzag")),
    ("funnel_start", dict(type="funnel_start")),
    ("finish_cup", dict(type="finish_cup")),
    ("cross", dict(type="cross")),
    ("spiral-tile40", dict(tile=40)),
    ("zigzag-big-steep", dict(type="zigzag", tile=80, height=60, slope=12)),
    ("cross-marble25-short", dict(type="cross", marble_d=25, channel_clearance=4, height=20)),
    ("spiral-marble12-short", dict(marble_d=12, channel_clearance=1, height=20, tile=40)),
    ("finish-nopeg", dict(type="finish_cup", peg=False)),
    ("funnel-loosepeg", dict(type="funnel_start", peg_clearance=0.6, tile=60)),
    ("drop-tightpeg-tall", dict(type="straight_drop", peg_clearance=0.1, height=60)),
]


def scad(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return '"%s"' % v
    return repr(v)


def defines(ov):
    return " ".join("-D %s" % shlex.quote("%s=%s" % (k, scad(v))) for k, v in ov.items())


def docker(script):
    r = subprocess.run(["docker", "run", "--rm", "-v", os.getcwd() + ":/w", "-w", "/w",
                        IMAGE, "bash", "-ec", script], capture_output=True, text=True)
    if r.returncode or "WARNING" in r.stderr or "ERROR" in r.stderr:
        print(r.stderr)
        sys.exit("FAIL: OpenSCAD reported errors or warnings")


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
    out = []
    for i in range(n):
        f = struct.unpack("<12f", data[84 + 50 * i: 84 + 50 * i + 48])
        out.append([f[3:6], f[6:9], f[9:12]])
    return out


def bbox(points):
    xs, ys, zs = zip(*points)
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


# ---- geometry helpers (point to triangle distance, Ericson 5.1.5)
def sub(a, b): return (a[0] - b[0], a[1] - b[1], a[2] - b[2])
def dot(a, b): return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def closest(p, a, b, c):
    ab, ac, ap = sub(b, a), sub(c, a), sub(p, a)
    d1, d2 = dot(ab, ap), dot(ac, ap)
    if d1 <= 0 and d2 <= 0: return a
    bp = sub(p, b); d3, d4 = dot(ab, bp), dot(ac, bp)
    if d3 >= 0 and d4 <= d3: return b
    vc = d1 * d4 - d3 * d2
    if vc <= 0 and d1 >= 0 and d3 <= 0:
        v = d1 / (d1 - d3); return (a[0] + v * ab[0], a[1] + v * ab[1], a[2] + v * ab[2])
    cp = sub(p, c); d5, d6 = dot(ab, cp), dot(ac, cp)
    if d6 >= 0 and d5 <= d6: return c
    vb = d5 * d2 - d1 * d6
    if vb <= 0 and d2 >= 0 and d6 <= 0:
        w = d2 / (d2 - d6); return (a[0] + w * ac[0], a[1] + w * ac[1], a[2] + w * ac[2])
    va = d3 * d6 - d5 * d4
    if va <= 0 and (d4 - d3) >= 0 and (d5 - d6) >= 0:
        w = (d4 - d3) / ((d4 - d3) + (d5 - d6))
        return (b[0] + w * (c[0] - b[0]), b[1] + w * (c[1] - b[1]), b[2] + w * (c[2] - b[2]))
    den = 1 / (va + vb + vc); v, w = vb * den, vc * den
    return (a[0] + ab[0] * v + ac[0] * w, a[1] + ab[1] * v + ac[1] * w, a[2] + ab[2] * v + ac[2] * w)


class Mesh:
    CELL = 3.0

    def __init__(self, verts, tris):
        self.tris = [(verts[t[0]], verts[t[1]], verts[t[2]]) for t in tris]
        self.grid = defaultdict(list)
        for i, (a, b, c) in enumerate(self.tris):
            lo = [int(math.floor(min(a[k], b[k], c[k]) / self.CELL)) for k in range(3)]
            hi = [int(math.floor(max(a[k], b[k], c[k]) / self.CELL)) for k in range(3)]
            for x in range(lo[0], hi[0] + 1):
                for y in range(lo[1], hi[1] + 1):
                    for z in range(lo[2], hi[2] + 1):
                        self.grid[(x, y, z)].append(i)

    def dist(self, p, reach):
        """Distance from p to the surface, or `reach` if nothing is nearer."""
        lo = [int(math.floor((p[k] - reach) / self.CELL)) for k in range(3)]
        hi = [int(math.floor((p[k] + reach) / self.CELL)) for k in range(3)]
        seen, best = set(), reach
        for x in range(lo[0], hi[0] + 1):
            for y in range(lo[1], hi[1] + 1):
                for z in range(lo[2], hi[2] + 1):
                    for i in self.grid.get((x, y, z), ()):
                        if i in seen:
                            continue
                        seen.add(i)
                        q = closest(p, *self.tris[i])
                        d = math.dist(p, q)
                        if d < best:
                            best = d
        return best

    def drop(self, p, reach):
        """Distance straight down from p to the first surface, or `reach`."""
        cx, cy = int(math.floor(p[0] / self.CELL)), int(math.floor(p[1] / self.CELL))
        z_hi = int(math.floor(p[2] / self.CELL))
        z_lo = int(math.floor((p[2] - reach) / self.CELL))
        best = reach
        for z in range(z_lo, z_hi + 1):
            for i in self.grid.get((cx, cy, z), ()):
                a, b, c = self.tris[i]
                den = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
                if abs(den) < 1e-12:
                    continue
                l1 = ((b[1] - c[1]) * (p[0] - c[0]) + (c[0] - b[0]) * (p[1] - c[1])) / den
                l2 = ((c[1] - a[1]) * (p[0] - c[0]) + (a[0] - c[0]) * (p[1] - c[1])) / den
                l3 = 1 - l1 - l2
                if min(l1, l2, l3) < -1e-9:
                    continue
                d = p[2] - (l1 * a[2] + l2 * b[2] + l3 * c[2])
                if -1e-6 <= d < best:
                    best = d
        return best


# ---- the model's derived dimensions, mirrored from model.scad
def derive(ov):
    P = dict(D, **ov)
    rc = (P["marble_d"] + P["channel_clearance"]) / 2
    T = P["tile"]
    H = max(P["height"], 2 * rc + MIN_FLOOR + 4)
    q = T / 2 - WALL - rc
    zs = H - 2 * rc
    rb = rc + 1
    xb = min(q, T / 2 - WALL - rb)
    t = P["type"]
    L = q * 1.5 * math.pi if t == "spiral" else (q + xb if t == "finish_cup" else 2 * q)
    dip = BOWL_DIP if t == "finish_cup" else 0
    theta = min(math.radians(P["slope"]), math.atan(max(0, zs - MIN_FLOOR - dip) / L))
    pc = (T / 2 - WALL + 1.5 + PEG_D / 2) / math.sqrt(2)
    return dict(P=P, rc=rc, T=T, H=H, q=q, zs=zs, rb=rb, xb=xb, L=L, theta=theta, pc=pc,
                top_pegs=P["peg"] and t != "funnel_start")


def polyline_samples(pts, z0, tan_t, step=1.0):
    """Marble-centre points every `step` mm along a channel polyline."""
    out, s = [], 0.0
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        seg = math.hypot(bx - ax, by - ay)
        n = max(1, int(math.ceil(seg / step)))
        for i in range(n + 1):
            f = i / n
            out.append((ax + f * (bx - ax), ay + f * (by - ay), z0 - (s + f * seg) * tan_t))
        s += seg
    return out


def marble_path(g):
    """(label, centre points) for every stretch of the marble's path."""
    t, q, rc, H, zs = g["P"]["type"], g["q"], g["rc"], g["H"], g["zs"]
    tan_t = math.tan(g["theta"])
    W, E, N, S = (-q, 0), (q, 0), (0, q), (0, -q)

    def lift(pts):  # channel floor -> marble centre line
        return [(x, y, z + rc) for x, y, z in pts]

    def column(p, z_lo, z_hi):
        n = max(1, int(math.ceil(z_hi - z_lo)))
        return [(p[0], p[1], z_lo + (z_hi - z_lo) * i / n) for i in range(n + 1)]

    entry = ("entry drop", column(W, zs + rc, H))
    if t == "straight_drop":
        return [("drop shaft", column(W, 0, H))]
    if t == "funnel_start":
        fr = g["T"] / 2 - WALL
        fz = max(H * 0.25, H - (fr + q - rc) * math.tan(math.radians(35)))
        return [("exit hole", column(W, 0, fz - 0.5))]
    if t == "zigzag":
        return [entry, ("channel", lift(polyline_samples([W, E], zs, tan_t))),
                ("exit hole", column(E, 0, zs - 2 * q * tan_t + rc))]
    if t == "cross":
        return [entry, ("channel W-E", lift(polyline_samples([W, E], zs, tan_t))),
                ("channel N", lift(polyline_samples([N, (0, 0)], zs, tan_t))),
                ("channel S", lift(polyline_samples([S, (0, 0)], zs, tan_t))),
                ("entry N", column(N, zs + rc, H)), ("entry S", column(S, zs + rc, H)),
                ("exit hole", column(E, 0, zs - 2 * q * tan_t + rc))]
    if t == "spiral":
        pts = [(q * math.cos(math.radians(180 - 270 * i / SPIRAL_STEPS)),
                q * math.sin(math.radians(180 - 270 * i / SPIRAL_STEPS)))
               for i in range(SPIRAL_STEPS + 1)]
        chain = polyline_samples(pts, zs, tan_t)
        return [entry, ("channel", lift(chain)),
                ("exit hole", column(S, 0, chain[-1][2] + rc))]
    if t == "finish_cup":
        return [entry, ("channel", lift(polyline_samples([W, (g["xb"], 0)], zs, tan_t)))]
    raise ValueError(t)


def exit_hole(g):
    """Centre and radius, in plan, of where the marble leaves the channel."""
    t, q = g["P"]["type"], g["q"]
    if t == "finish_cup":
        return g["xb"], 0, g["rb"]
    if t == "spiral":
        return 0, -q, g["rc"]
    if t in ("straight_drop", "funnel_start"):
        return -q, 0, g["rc"]
    return q, 0, g["rc"]


# ---- phase 1: combined renders
docker("\n".join("openscad --backend=Manifold %s -o %s/%s.3mf model.scad"
                 % (defines(ov), OUT, name) for name, ov in CASES))
combined = {name: read_3mf("%s/%s.3mf" % (OUT, name)) for name, _ in CASES}

# ---- phase 2: one closed render per colour (ScadBuddy's colour wrapper)
with open("%s/wrap.scad" % OUT, "w") as f:
    f.write('_sb_t = "";\nmodule color(c, alpha = 1) { if (c == _sb_t) children(); }\n'
            "include <../model.scad>\n")
docker("\n".join(
    "openscad --backend=Manifold %s -D '_sb_t=\"%s\"' -o %s/%s_%s.stl %s/wrap.scad"
    % (defines(ov), col, OUT, name, col[1:], OUT)
    for name, ov in CASES for col in (PIECE, ACCENT)))

# ---- checks
failures = []


def check(ok, msg):
    print(("  PASS  " if ok else "  FAIL  ") + msg)
    if not ok:
        failures.append(msg)


def near(a, b, tol=TOL):
    return abs(a - b) <= tol


for name, ov in CASES:
    g = derive(ov)
    T, H, rc = g["T"], g["H"], g["rc"]
    print("\n[%s] %s" % (name, " ".join("%s=%s" % kv for kv in ov.items()) or "defaults"))
    print("       level height %.1f, channel %.1f mm wide, slope %.2f deg, path %.1f mm"
          % (H, 2 * rc, math.degrees(g["theta"]), g["L"]))
    mats, verts, tris = combined[name]
    used = Counter(t[3] for t in tris)
    named = {col for i, (n, col) in enumerate(mats) if n != "Default" and used.get(i)}
    check(named == {PIECE, ACCENT}, "two parts %s (got %s)" % ([PIECE, ACCENT], sorted(named)))
    check(used.get(0, 0) == 0, "Default material has no triangles (got %d)" % used.get(0, 0))

    lo, hi = bbox(verts)
    top = H + (PEG_H if g["top_pegs"] else 0)
    exp = (-T / 2, -T / 2, 0, T / 2, T / 2, top)
    check(all(near(a, b) for a, b in zip(lo + hi, exp)),
          "bbox %s .. %s == %s .. %s" % (tuple(round(x, 2) for x in lo),
                                         tuple(round(x, 2) for x in hi),
                                         exp[:3], tuple(round(x, 2) for x in exp[3:])))
    check(near(lo[2], 0), "sits on z=0 (min z %.3f)" % lo[2])

    body = read_stl("%s/%s_%s.stl" % (OUT, name, PIECE[1:]))
    acc = read_stl("%s/%s_%s.stl" % (OUT, name, ACCENT[1:]))
    blo, bhi = bbox([v for t in body for v in t])
    alo, ahi = bbox([v for t in acc for v in t])
    check(near(blo[2], 0) and near(bhi[2], H - RIM_H),
          "piece part z %.3f .. %.3f (expected 0 .. %.3f)" % (blo[2], bhi[2], H - RIM_H))
    check(near(alo[2], H - RIM_H) and near(ahi[2], top),
          "accent part z %.3f .. %.3f (expected %.3f .. %.3f)" % (alo[2], ahi[2], H - RIM_H, top))

    # Peg / socket fit, read off the closed parts' vertices at a corner.
    pc = g["pc"]
    if g["P"]["peg"]:
        sock = [math.hypot(v[0] - pc, v[1] - pc) for t in body for v in t
                if abs(v[2]) < TOL and math.hypot(v[0] - pc, v[1] - pc) < PEG_D]
        rs = min(sock) if sock else 0
        want = PEG_D / 2 + g["P"]["peg_clearance"]
        check(near(rs, want), "socket radius %.3f == peg radius + clearance %.3f" % (rs, want))
        if g["top_pegs"]:
            pegs = [math.hypot(v[0] - pc, v[1] - pc) for t in acc for v in t
                    if H + TOL < v[2] < H + PEG_H - 0.8 + TOL
                    and math.hypot(v[0] - pc, v[1] - pc) < PEG_D]
            rp = max(pegs) if pegs else 0
            check(near(rp, PEG_D / 2), "peg radius %.3f == %.3f" % (rp, PEG_D / 2))
    else:
        corner = [v for t in body + acc for v in t if math.hypot(v[0] - pc, v[1] - pc) < PEG_D
                  and (abs(v[2]) < TOL or v[2] > H + TOL)]
        check(not corner, "peg=false: no pegs or sockets (%d stray vertices)" % len(corner))

    # Marble path clearance. FACET allows for the facets of a $fn=64 circle
    # sitting inside the true radius (0.02 mm at these sizes).
    FACET = 0.08
    mesh = Mesh(verts, tris)
    reach = rc + 1
    hx, hy, hr = exit_hole(g)
    for label, pts in marble_path(g):
        ds = [mesh.dist(p, reach) for p in pts]
        check(min(ds) >= rc - FACET,
              "%-11s %3d points: nearest surface %.3f, channel radius %.3f (facet tol %.2f)"
              % (label, len(pts), min(ds), rc, FACET))
        if label.startswith("channel"):
            # Until the marble is over the exit hole (or the finish bowl),
            # the floor must be right under it.
            fl = [mesh.drop(p, reach) for p in pts
                  if math.hypot(p[0] - hx, p[1] - hy) > hr]
            # Straight down from the centre line of a sloped round-bottomed
            # channel the floor is rc / cos(slope) away. Where two channels
            # meet, or at the lip of the finish bowl, it may dip a little.
            nom = rc / math.cos(g["theta"])
            check(all(nom - FACET <= f <= nom + 0.25 for f in fl),
                  "%-11s floor %.3f .. %.3f below the marble centre line (nominal %.3f) up to the exit"
                  % (label, min(fl, default=nom), max(fl, default=nom), nom))

    if g["P"]["type"] == "finish_cup":
        zb = g["zs"] - g["L"] * math.tan(g["theta"]) - BOWL_DIP
        c = (g["xb"], 0, zb + g["rb"])
        d = mesh.dist(c, g["rb"] + 1)
        check(abs(d - g["rb"]) <= 0.08, "finish bowl radius %.3f == %.3f" % (d, g["rb"]))
        check(zb >= MIN_FLOOR - TOL, "finish bowl bottom %.2f above min floor" % zb)

if failures:
    print("\nFAILED: %d check(s)" % len(failures))
    sys.exit(1)
print("\nOK: %d cases" % len(CASES))
PY
