#!/usr/bin/env bash
# Render models/wifi-qr-plaque with the defaults and the major variations and
# check that the QR code is correct, not just that the plaque has the right
# size:
#
#   1. host python3 (stdlib only): run the renders, parse each 3MF, check the
#      parts, bounding boxes and heights, and rasterise the code part's top
#      faces back into a module grid, which must equal the matrix the model
#      echoes;
#   2. a sweep of 40 echo-only runs covering versions 1-10 at every error
#      correction level;
#   3. uv + python-qrcode + OpenCV: every matrix must equal python-qrcode's for the same
#      payload, version, level and mask, OpenSCAD's version must be the one
#      python-qrcode picks, the eight mask penalties must equal an independent Python
#      scoring, and OpenCV must decode each rendered code back to the payload.
set -euo pipefail

cd "$(dirname "$0")"

BASE_IMAGE="${SCADBUDDY_OPENSCAD_IMAGE:-openscad/openscad:dev}"
FONTS_IMAGE="${SCADBUDDY_FONTS_IMAGE:-scadbuddy-verify:local}"
OUT="${OUT_DIR:-.verify}"
UV="${UV:-$(command -v uv || echo "$HOME/.local/bin/uv")}"

mkdir -p "$OUT"

# Captions use DejaVu, which the base image has, but the ScadBuddy image is
# the reference; derive a throwaway one with its font packages if needed.
IMAGE="$FONTS_IMAGE"
if ! docker image inspect "$FONTS_IMAGE" >/dev/null 2>&1; then
    echo "==> building $FONTS_IMAGE from $BASE_IMAGE with the image's font packages"
    docker build -q -t "$FONTS_IMAGE" - <<DOCKERFILE
FROM $BASE_IMAGE
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      fonts-lobster fonts-lobstertwo fonts-dejavu fonts-noto-core \
 && fc-cache -f \
 && rm -rf /var/lib/apt/lists/*
DOCKERFILE
fi

echo "==> stage 1: renders and geometry ($IMAGE)"
python3 - "$IMAGE" "$OUT" <<'PY'
import json, os, re, subprocess, sys, time, zipfile, random
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict

IMAGE, OUT = sys.argv[1], sys.argv[2]
NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
EPS = 0.001

DEFAULTS = dict(mode="wifi", ssid="MyWiFi", password="secret", security="WPA",
                hidden_ssid=False, url="https://example.com", text="",
                error_correction="M", size=80, shape="square", caption="Scan for WiFi",
                show_credentials=False, magnet_pockets=False, magnet_diameter=10.2,
                magnet_depth=2.2, stand="none", stand_clearance=0.4, thickness=3,
                module_height=0.6, code_style="raised", plate_color="#FFFFFF",
                code_color="#000000", caption_color="#000000")

LOREM = ("The quick brown fox jumps over the lazy dog while the five boxing wizards "
         "jump quickly; pack my box with five dozen liquor jugs. Sphinx of black quartz, "
         "judge my vow. How vexingly quick daft zebras jump! 0123456789")

VARIANTS = [
    ("defaults", {}),
    ("url-rounded-inlay-magnets", dict(mode="url", url="https://github.com/eh-homelab/ScadBuddy",
                                        shape="rounded", code_style="inlay", error_correction="H",
                                        magnet_pockets=True, caption="Docs",
                                        caption_color="#0047BB")),
    ("text-round-stand", dict(mode="text", text=LOREM[:120], shape="round", stand="desk_stand",
                              size=100, caption="Read me")),
    ("wifi-credentials-round", dict(ssid='Caf;e,"x":\\', password="p@ss:w;rd", security="WEP",
                                    hidden_ssid=True, show_credentials=True, error_correction="Q",
                                    shape="round", size=90)),
    ("wifi-nopass-stand-magnets", dict(security="nopass", ssid="Guest", stand="desk_stand",
                                       magnet_pockets=True, code_style="inlay", thickness=4,
                                       show_credentials=True, shape="rounded")),
    ("v10-small-quietzone", dict(mode="text", text=LOREM[:200], size=50, caption="",
                                 error_correction="M")),
    ("long-text-ec-fallback", dict(mode="text", text=(LOREM * 2)[:250], error_correction="H",
                                   caption="", code_color="#1B3A6B")),
    # Wide capitals used to overflow the plaque: the old average-advance
    # estimate put 30 W's 152 mm wide on an 80 mm plaque.
    ("wide-caption", dict(caption="W" * 30, caption_color="#E53935")),
    ("wide-credentials-round", dict(caption="WIFI GUEST ACCESS MMMMMMM", show_credentials=True,
                                    ssid="W" * 32, password="W" * 63, size=50, shape="round",
                                    caption_color="#E53935")),
]


def scad_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def parse_echo(text):
    info, pen, rows = None, None, {}
    for line in text.splitlines():
        m = re.search(r'ECHO: "(QR\w+);(.*)"$', line)
        if not m:
            continue
        kind, rest = m.groups()
        if kind == "QRINFO":
            f = rest.split(";")
            info = dict(version=int(f[0]), ecl=f[1], mask=int(f[2]), nbytes=int(f[3]),
                        nbytes_all=int(f[4]), qz=int(f[5]), ms=float(f[6]), x0=float(f[7]),
                        y0=float(f[8]), n=int(f[9]), top_z=float(f[10]))
        elif kind == "QRPEN":
            pen = json.loads(rest)
        elif kind == "QRROW":
            y, bits = rest.split(";")
            rows[int(y)] = bits
    matrix = [rows[y] for y in range(len(rows))]
    return info, pen, matrix


def load_3mf(path):
    root = ET.fromstring(zipfile.ZipFile(path).read("3D/3dmodel.model"))
    mats = [(b.get("name"), (b.get("displaycolor") or "")[:7].upper()) for b in root.iter(NS + "base")]
    verts = [(float(v.get("x")), float(v.get("y")), float(v.get("z"))) for v in root.iter(NS + "vertex")]
    tris = [(int(t.get("v1")), int(t.get("v2")), int(t.get("v3")), int(t.get("p1") or 0))
            for t in root.iter(NS + "triangle")]
    return mats, verts, tris


def rasterise(verts, tris, mat, info):
    """Module grid from the code part's upward faces at its top height."""
    n, ms, x0, y0, zt = info["n"], info["ms"], info["x0"], info["y0"], info["top_z"]
    dark = set()
    for a, b, c, p in tris:
        if p != mat:
            continue
        A, B, C = verts[a], verts[b], verts[c]
        if any(abs(v[2] - zt) > EPS for v in (A, B, C)):
            continue
        cross = (B[0] - A[0]) * (C[1] - A[1]) - (B[1] - A[1]) * (C[0] - A[0])
        if cross <= 0:
            continue
        xs, ys = (A[0], B[0], C[0]), (A[1], B[1], C[1])
        i0 = max(0, int((min(xs) - x0) / ms) - 1); i1 = min(n - 1, int((max(xs) - x0) / ms) + 1)
        j0 = max(0, int((y0 - max(ys)) / ms) - 1); j1 = min(n - 1, int((y0 - min(ys)) / ms) + 1)
        for j in range(j0, j1 + 1):
            for i in range(i0, i1 + 1):
                if (i, j) in dark:
                    continue
                px, py = x0 + (i + 0.5) * ms, y0 - (j + 0.5) * ms
                d1 = (B[0] - A[0]) * (py - A[1]) - (B[1] - A[1]) * (px - A[0])
                d2 = (C[0] - B[0]) * (py - B[1]) - (C[1] - B[1]) * (px - B[0])
                d3 = (A[0] - C[0]) * (py - C[1]) - (A[1] - C[1]) * (px - C[0])
                if d1 >= 0 and d2 >= 0 and d3 >= 0:
                    dark.add((i, j))
    return ["".join("1" if (i, j) in dark else "0" for i in range(n)) for j in range(n)]


failures = []


def check(ok, msg):
    print(("  PASS  " if ok else "  FAIL  ") + msg)
    if not ok:
        failures.append(msg)


results = {"variants": [], "sweep": []}
cwd = os.getcwd()

for name, over in VARIANTS:
    p = dict(DEFAULTS, **over)
    args = []
    for k, v in over.items():
        args += ["-D", "%s=%s" % (k, scad_value(v))]
    t0 = time.time()
    r = subprocess.run(["docker", "run", "--rm", "--label", "scadbuddy-verify=" + os.environ.get("SCADBUDDY_VERIFY_LABEL", "local"), "-v", cwd + ":/w", "-w", "/w", IMAGE,
                        "openscad", "--backend=Manifold", *args,
                        "-o", "%s/%s.3mf" % (OUT, name), "model.scad"],
                       capture_output=True, text=True)
    dt = time.time() - t0
    print("\n[%s] rendered in %.1f s (incl. container start)" % (name, dt))
    if r.returncode != 0:
        print(r.stderr[-2000:])
        check(False, "%s: openscad exited %d" % (name, r.returncode))
        continue
    check("ERROR" not in r.stderr, "%s: no ERROR in the OpenSCAD log" % name)
    for w in re.findall(r"WARNING: .*", r.stderr):
        print("  note  " + w)
    info, pen, matrix = parse_echo(r.stderr)
    mats, verts, tris = load_3mf("%s/%s.3mf" % (OUT, name))
    counts = Counter(t[3] for t in tris)
    by_col = {col: i for i, (nm, col) in enumerate(mats) if nm != "Default" and counts.get(i)}
    print("  QR version %d-%s mask %d, %d bytes, %d modules of %.3f mm, quiet zone %d"
          % (info["version"], info["ecl"], info["mask"], info["nbytes"], info["n"], info["ms"], info["qz"]))

    # Level: the requested one, stepped down only when version 10 cannot hold
    # the payload at it.
    caps10 = {"L": 271, "M": 213, "Q": 151, "H": 119}
    want = p["error_correction"]
    while want != "L" and caps10[want] < info["nbytes_all"]:
        want = "LMQH"["LMQH".index(want) - 1]
    check(info["ecl"] == want, "%s: error correction %s (requested %s, payload %d bytes)"
          % (name, info["ecl"], p["error_correction"], info["nbytes_all"]))

    cols = {p["plate_color"].upper(), p["code_color"].upper()}
    has_text = p["caption"] != "" or (p["mode"] == "wifi" and p["show_credentials"])
    if has_text:
        cols.add(p["caption_color"].upper())
    check(len(by_col) == len(cols), "%s: %d non-empty materials besides Default (got %d: %s)"
          % (name, len(cols), len(by_col), sorted(by_col)))
    check(counts.get(0, 0) == 0 or mats[0][0] != "Default",
          "%s: Default material carries no geometry" % name)

    W, th, mh = p["size"], p["thickness"], p["module_height"]
    inlay = p["code_style"] == "inlay"
    stand = p["stand"] == "desk_stand"
    stand_depth = max(30, 9 + 0.15 * W + 8)
    stand_h = 6 + 4
    xs, ys, zs = zip(*verts)
    exp_x = W
    exp_y = W + (6 + stand_depth if stand else 0)
    exp_z = max(th if inlay else th + mh, stand_h if stand else 0)
    check(abs(max(xs) - min(xs) - exp_x) < 0.01, "%s: X %.3f == %.3f" % (name, max(xs) - min(xs), exp_x))
    check(abs(max(ys) - min(ys) - exp_y) < 0.01, "%s: Y %.3f == %.3f" % (name, max(ys) - min(ys), exp_y))
    check(abs(max(zs) - min(zs) - exp_z) < 0.001, "%s: Z %.3f == %.3f" % (name, max(zs) - min(zs), exp_z))
    check(abs(min(zs)) < 0.001, "%s: sits on z=0 (min z %.4f)" % (name, min(zs)))

    per = defaultdict(list)
    for t in tris:
        per[t[3]].extend(t[:3])
    plate_i = by_col.get(p["plate_color"].upper())
    code_i = by_col.get(p["code_color"].upper())
    pz = [verts[v][2] for v in per[plate_i]]
    py = [verts[v][1] for v in per[plate_i]]
    cz = [verts[v][2] for v in per[code_i]]
    cz_exp = (th - mh, th) if inlay else (th, th + mh)
    # The 3MF's parts are split from one union, so faces shared by two colours
    # are gone: an inlaid code keeps only its top faces. Check the pocket floor
    # on the plate instead.
    check(abs(min(pz)) < 0.001 and abs(max(pz) - max(th, stand_h if stand else 0)) < 0.001,
          "%s: plate part z 0 .. %.1f" % (name, max(th, stand_h if stand else 0)))
    if inlay:
        check(abs(min(cz) - th) < 0.001 and abs(max(cz) - th) < 0.001,
              "%s: inlaid code flush with the plate top at z=%.1f (got %.3f .. %.3f)" % (name, th, min(cz), max(cz)))
    else:
        check(abs(min(cz) - cz_exp[0]) < 0.001 and abs(max(cz) - cz_exp[1]) < 0.001,
              "%s: code part z %.1f .. %.1f (got %.3f .. %.3f)" % (name, cz_exp[0], cz_exp[1], min(cz), max(cz)))
    if stand:
        check(min(py) < -W / 2 - 5, "%s: stand laid out in front of the plaque" % name)
    if p["magnet_pockets"]:
        depth = min(p["magnet_depth"], th - (mh if inlay else 0) - 0.6)
        check(any(abs(z - depth) < 0.001 for z in pz),
              "%s: magnet pocket ceiling at z=%.2f" % (name, depth))
    check(info["qz"] >= 2, "%s: quiet zone >= 2 modules (got %d)" % (name, info["qz"]))
    # The quiet zone must be clear of the plaque's outline and of the text.
    n, ms, x0, y0 = info["n"], info["ms"], info["x0"], info["y0"]
    q = info["qz"] * ms
    if p["shape"] == "round":
        r_needed = max(((x0 - q) ** 2 + (y0 + q) ** 2) ** 0.5, ((x0 - q) ** 2 + (y0 - n * ms - q) ** 2) ** 0.5)
        check(r_needed <= W / 2 + 1e-3, "%s: quiet-zone square inside the round plaque (%.2f <= %.2f)"
              % (name, r_needed, W / 2))
    else:
        check(x0 - q >= -W / 2 - 1e-3 and y0 + q <= W / 2 + 1e-3, "%s: quiet-zone square inside the plaque" % name)
    if has_text:
        ti = by_col.get(p["caption_color"].upper())
        text_top = max(verts[v][1] for v in per[ti] if abs(verts[v][2] - cz_exp[1]) < 0.001
                       and ti != code_i) if ti != code_i else None
        if text_top is None:
            # merged with the code part: take the highest vertex below the quiet zone
            below = [verts[v][1] for v in per[code_i] if verts[v][1] < y0 - n * ms - 0.01]
            text_top = max(below) if below else None
        if ti != code_i:
            # Every line shrinks to the text band: 0.9 x size (square),
            # 0.8 x size (rounded), the code block's side (round).
            tw = {"square": 0.9 * W, "rounded": 0.8 * W}.get(p["shape"], ms * (n + 2 * info["qz"]))
            tx = [abs(verts[v][0]) for v in per[ti]]
            # 0.1 mm slack: halign=center centres the advance, not the ink.
            check(max(tx) <= tw / 2 + 0.1, "%s: text within the %.2f mm text band (widest half %.2f)"
                  % (name, tw, max(tx)))
        if text_top is not None:
            check(text_top <= y0 - n * ms - q + 1e-3,
                  "%s: text stays below the quiet zone (text top %.2f, zone bottom %.2f)"
                  % (name, text_top, y0 - n * ms - q))
    # Closed parts, the way ScadBuddy's closed-part path renders them: a
    # wrapper whose color() keeps only one colour's subtree. Each must be
    # watertight, the code part must span its expected heights, and the parts'
    # volumes must add up to the union's (so no two colours overlap).
    def render_only(target, tag):
        wrap = "%s/%s-%s.scad" % (OUT, name, tag)
        with open(wrap, "w") as fh:
            fh.write('_sb_target = %s;\nmodule color(c, alpha = 1) { if (_sb_target == "" || c == _sb_target) children(); }\n'
                     'include <../model.scad>\n' % scad_value(target))
        out = "%s/%s-%s.3mf" % (OUT, name, tag)
        rr = subprocess.run(["docker", "run", "--rm", "--label", "scadbuddy-verify=" + os.environ.get("SCADBUDDY_VERIFY_LABEL", "local"), "-v", cwd + ":/w", "-w", "/w", IMAGE,
                             "openscad", "--backend=Manifold", *args, "-o", out, wrap],
                            capture_output=True, text=True)
        if rr.returncode != 0:
            return None
        _, vv, tt = load_3mf(out)
        return vv, tt

    def volume(vv, tt):
        s6 = 0.0
        for a, b, c, _ in tt:
            A, B, C = vv[a], vv[b], vv[c]
            s6 += (A[0] * (B[1] * C[2] - B[2] * C[1]) - A[1] * (B[0] * C[2] - B[2] * C[0])
                   + A[2] * (B[0] * C[1] - B[1] * C[0]))
        return s6 / 6

    def watertight(vv, tt):
        edges = Counter()
        for a, b, c, _ in tt:
            for e in ((a, b), (b, c), (c, a)):
                edges[e] += 1
        return all(edges[(b, a)] == k for (a, b), k in edges.items())

    union = render_only("", "all")
    part_vol = 0.0
    for col in sorted(cols):
        got = render_only(col, "part" + col[1:])
        if got is None:
            check(False, "%s: closed part %s renders" % (name, col))
            continue
        vv, tt = got
        v_ = volume(vv, tt)
        part_vol += v_
        check(watertight(vv, tt) and v_ > 0, "%s: closed part %s is watertight (%.1f mm^3)" % (name, col, v_))
        if col == p["code_color"].upper():
            zz = [q[2] for q in vv]
            check(abs(min(zz) - cz_exp[0]) < 0.001 and abs(max(zz) - cz_exp[1]) < 0.001,
                  "%s: closed code part z %.1f .. %.1f (got %.3f .. %.3f)"
                  % (name, cz_exp[0], cz_exp[1], min(zz), max(zz)))
    if union:
        uv = volume(*union)
        check(abs(part_vol - uv) <= 1e-4 * uv,
              "%s: part volumes sum to the union's (%.2f vs %.2f mm^3): no overlap" % (name, part_vol, uv))

    raster = rasterise(verts, tris, code_i, info)
    check(raster == matrix, "%s: code part's top faces rasterise to the echoed %dx%d matrix" % (name, n, n))
    results["variants"].append(dict(name=name, params=p, info=info, penalties=pen,
                                    matrix=matrix, raster=raster))

# ---- sweep: versions 1-10 x L/M/Q/H, echo only ----
DATA_CW = [[19, 16, 13, 9], [34, 28, 22, 16], [55, 44, 34, 26], [80, 64, 48, 36],
           [108, 86, 62, 46], [136, 108, 76, 60], [156, 124, 88, 66], [194, 154, 110, 86],
           [232, 182, 132, 100], [274, 216, 154, 122]]
rng = random.Random(18004)
alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -_.,:;/?=&%+!#"
sweep_dir = os.path.join(OUT, "sweep")
os.makedirs(sweep_dir, exist_ok=True)
for f in os.listdir(sweep_dir):
    os.remove(os.path.join(sweep_dir, f))
cases = []
for v in range(1, 11):
    for e, ecl in enumerate("LMQH"):
        cap = (DATA_CW[v - 1][e] * 8 - 4 - (8 if v < 10 else 16)) // 8
        # Fill the version exactly; every fourth case carries multi-byte UTF-8.
        head = "é€😀 " if (v + e) % 4 == 0 and cap > 12 else ""
        filler = cap - len(head.encode())
        text = head + "".join(rng.choice(alphabet) for _ in range(filler))
        name = "v%02d-%s" % (v, ecl)
        with open(os.path.join(sweep_dir, name + ".scad"), "w", encoding="utf-8") as fh:
            fh.write('include <../../model.scad>\nmode = "text";\ntext = %s;\nerror_correction = "%s";\n'
                     % (scad_value(text), ecl))
        cases.append((name, v, ecl, text))
t0 = time.time()
r = subprocess.run(["docker", "run", "--rm", "--label", "scadbuddy-verify=" + os.environ.get("SCADBUDDY_VERIFY_LABEL", "local"), "-v", cwd + ":/w", "-w", "/w/" + sweep_dir, IMAGE, "sh", "-c",
                    'for f in *.scad; do openscad -o "${f%.scad}.echo" "$f" 2>/dev/null || echo "FAILED $f"; done'],
                   capture_output=True, text=True)
print("\n[sweep] 40 echo-only runs in %.1f s" % (time.time() - t0))
check("FAILED" not in r.stdout, "sweep: every case evaluates (%s)" % r.stdout.strip())
for name, v, ecl, text in cases:
    try:
        info, pen, matrix = parse_echo(open(os.path.join(sweep_dir, name + ".echo"), encoding="utf-8").read())
    except Exception as ex:
        check(False, "sweep %s: echo output parsed (%s)" % (name, ex))
        continue
    ok = info["version"] == v and info["ecl"] == ecl
    if not ok:
        check(False, "sweep %s: picked version %d-%s" % (name, info["version"], info["ecl"]))
    results["sweep"].append(dict(name=name, text=text, info=info, penalties=pen, matrix=matrix))
check(len(results["sweep"]) == 40, "sweep: 40 cases, each at its intended version and level")

json.dump(results, open(os.path.join(OUT, "qr.json"), "w", encoding="utf-8"), ensure_ascii=False)
if failures:
    print("\nstage 1 FAILED: %d check(s)" % len(failures))
    sys.exit(1)
print("\nstage 1 OK")
PY

echo
echo "==> stage 2: python-qrcode reference + OpenCV decode (uv)"
SSL_CERT_DIR='' "$UV" run --quiet --no-project \
    --with qrcode==8.2 --with opencv-python-headless --with numpy \
    python - "$OUT/qr.json" <<'PY'
import json, sys
import numpy as np
import cv2
import qrcode
import qrcode.util

EC = {"L": qrcode.constants.ERROR_CORRECT_L, "M": qrcode.constants.ERROR_CORRECT_M,
      "Q": qrcode.constants.ERROR_CORRECT_Q, "H": qrcode.constants.ERROR_CORRECT_H}

data = json.load(open(sys.argv[1], encoding="utf-8"))
failures = []


def check(ok, msg):
    if not ok:
        print("  FAIL  " + msg)
        failures.append(msg)
    return ok


def esc(s):
    return "".join("\\" + c if c in '\\;,:"' else c for c in s)


def payload(p):
    if p["mode"] == "wifi":
        pw = "" if p["security"] == "nopass" else "P:%s;" % esc(p["password"])
        return "WIFI:T:%s;S:%s;%sH:%s;;" % (p["security"], esc(p["ssid"]), pw,
                                           "true" if p["hidden_ssid"] else "false")
    return p["url"] if p["mode"] == "url" else p["text"]


def penalty(m):
    """ISO/IEC 18004 7.8.3, outside the symbol counted as light. Written
    independently of the OpenSCAD so the two can check each other."""
    n = len(m)
    cols = [[m[y][x] for y in range(n)] for x in range(n)]
    s = 0
    for line in list(m) + cols:
        run = 1
        for i in range(1, n + 1):
            if i < n and line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    s += run - 2
                run = 1
        ext = "0000" + "".join(map(str, line)) + "0000"
        for pat in ("10111010000", "00001011101"):
            s += 40 * sum(1 for i in range(len(ext) - 10) if ext[i:i + 11] == pat)
    for y in range(n - 1):
        for x in range(n - 1):
            if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                s += 3
    dark = sum(map(sum, m))
    s += 10 * (abs(dark * 20 - n * n * 10) // (n * n))
    return s


def reference(raw, ecl, version=None, mask=None):
    q = qrcode.QRCode(version=version, error_correction=EC[ecl], mask_pattern=mask, border=0)
    q.add_data(qrcode.util.QRData(raw, mode=qrcode.util.MODE_8BIT_BYTE))
    q.make(fit=version is None)
    return q


def ref_matrix(q):
    return ["".join("1" if c else "0" for c in row) for row in q.get_matrix()]


def verify(name, text, info, pens, matrix, raster=None):
    raw = text.encode("utf-8")[:info["nbytes"]]
    ecl, v = info["ecl"], info["version"]
    ok = True
    auto = reference(raw, ecl)
    ok &= check(auto.version == v, "%s: version %d, python-qrcode picks %s" % (name, v, auto.version))
    forced = [ref_matrix(reference(raw, ecl, v, m)) for m in range(8)]
    ok &= check(forced[info["mask"]] == matrix,
                "%s: matrix equals python-qrcode's %d-%s mask %d module for module"
                % (name, v, ecl, info["mask"]))
    py_pens = [penalty([[int(c) for c in row] for row in q]) for q in forced]
    ok &= check(py_pens == pens, "%s: mask penalties %s == Python %s" % (name, pens, py_pens))
    ok &= check(info["mask"] == py_pens.index(min(py_pens)), "%s: lowest-penalty mask chosen" % name)
    ref_mask = auto.best_mask_pattern()
    agree = "same" if ref_mask == info["mask"] else "python-qrcode's own scoring picks %d" % ref_mask
    if raster is not None:
        grid = np.array([[int(c) for c in row] for row in raster], dtype=np.uint8)
        img = np.pad(1 - grid, 4, constant_values=1) * 255
        img = cv2.resize(img, None, fx=10, fy=10, interpolation=cv2.INTER_NEAREST)
        cv2.imwrite(sys.argv[1].replace("qr.json", name + "-code.png"), img)
        dec, _, _ = cv2.QRCodeDetector().detectAndDecode(img)
        ok &= check(dec == raw.decode("utf-8", "replace"),
                    "%s: OpenCV decodes the rendered code to the payload (got %r)" % (name, dec))
    print("  %s  %-26s v%-2d %s mask %d (%s)%s" % ("PASS" if ok else "FAIL", name, v, info["ecl"],
          info["mask"], agree, ", decoded from the 3MF" if raster is not None else ""))


for r in data["variants"]:
    verify(r["name"], payload(r["params"]), r["info"], r["penalties"], r["matrix"], r["raster"])
for r in data["sweep"]:
    verify(r["name"], r["text"], r["info"], r["penalties"], r["matrix"])

if failures:
    print("\nstage 2 FAILED: %d check(s)" % len(failures))
    sys.exit(1)
print("\nstage 2 OK")
PY

echo
echo "OK"
