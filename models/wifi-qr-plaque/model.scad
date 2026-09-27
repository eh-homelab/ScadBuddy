// WiFi / URL QR code plaque — a flat plaque (or fridge magnet) carrying a QR
// code that joins a WiFi network, opens a web link or shows plain text, with an
// optional caption and the network name and password printed underneath.
//
// The QR code is generated here, in plain OpenSCAD, with no library: byte mode,
// versions 1-10 (the smallest that fits is chosen automatically), error
// correction L/M/Q/H, Reed-Solomon over GF(256) and all eight masks scored with
// the ISO/IEC 18004 penalty rules. verify.sh checks the result module for
// module against the `segno` Python library and decodes the rendered geometry.
//
// Written to the MakerWorld Parametric Model Maker customizer conventions so
// the same file works unchanged on MakerWorld and in ScadBuddy.
//
// The colour parameters are the extruder order: plate_color is extruder 1,
// code_color extruder 2, caption_color extruder 3. With the defaults the code
// and the caption are both black, so they merge into one part and the plaque
// prints in two colours.

/* [Network] */

// What the code does when scanned
mode = "wifi"; // [wifi:Join a WiFi network, url:Open a web link, text:Show plain text]

// WiFi network name (SSID)
ssid = "MyWiFi"; // 32

// WiFi password (ignored for an open network)
password = "secret"; // 63

// WiFi security type
security = "WPA"; // [WPA:WPA / WPA2 / WPA3, WEP:WEP, nopass:Open network - no password]

// The network does not broadcast its name
hidden_ssid = false;

// Web link for the url mode
url = "https://example.com"; // 250

// Text for the text mode
text = ""; // 250

// Error correction: L 7%, M 15%, Q 25%, H 30% of the code can be damaged and still scan
error_correction = "M"; // [L:L - 7%, M:M - 15%, Q:Q - 25%, H:H - 30%]

/* [Plaque] */

// Plaque width (and height) in mm
size = 80; // [50:5:150]

// Outline of the plaque
shape = "square"; // [square:Square, rounded:Rounded square, round:Round]

// Caption under the code (leave empty for none; long lines shrink to fit)
caption = "Scan for WiFi"; // 30

// Print the network name and password under the code (WiFi mode only)
show_credentials = false;

// Pockets in the back for round magnets (fridge magnet)
magnet_pockets = false;

// Magnet pocket diameter in mm (magnet diameter plus clearance)
magnet_diameter = 10.2; // [5:0.1:20]

// Magnet pocket depth in mm (capped to leave 0.6 mm of plate above it)
magnet_depth = 2.2; // [1:0.1:4]

// Add a separate desk stand the plaque slots into
stand = "none"; // [none:None, desk_stand:Desk stand]

// Extra width in mm of the stand's slot over the plaque thickness
stand_clearance = 0.4; // [0.1:0.05:1]

// Plate thickness in mm
thickness = 3; // [2:0.5:6]

// Height in mm of the raised code and text, or depth of the inlay
module_height = 0.6; // [0.4:0.2:1.2]

// Raised: code stands on the plate. Inlay: code is set flush into the plate
code_style = "raised"; // [raised:Raised, inlay:Inlay - flush]

/* [Colors] */

// Plate colour (extruder 1)
plate_color = "#FFFFFF"; // color

// QR code colour (extruder 2)
code_color = "#000000"; // color

// Caption and credentials colour (extruder 3; same as the code by default)
caption_color = "#000000"; // color

/* [Hidden] */

$fn = 64;

// Quiet zone around the code, in modules. The standard asks for 4; it drops
// to 2 when 4 would make the modules smaller than min_module.
quiet_zone_full = 4;
quiet_zone_min = 2;
min_module = 1.0;

// Nominal text sizes, as a fraction of the plaque size. Lines longer than
// the text band shrink to fit it (fit_x below), never grow.
caption_frac = 0.075;
cred_frac = 0.05;
caption_font = "DejaVu Sans:style=Bold";
cred_font = "DejaVu Sans Mono:style=Bold";
line_pitch = 1.5;

// Desk stand
slot_depth = 6;
slot_tilt = 15;
stand_gap = 6;

// ---------------------------------------------------------------------------
// Small helpers. OpenSCAD has no bitwise operators and no sum().
// ---------------------------------------------------------------------------

function vsum(v) = len(v) == 0 ? 0 : v * [for (x = v) 1];
function bit(v, i) = floor(v / pow(2, i)) % 2;
function xorn(a, b, n) = vsum([for (i = [0:n - 1]) ((floor(a / pow(2, i)) + floor(b / pow(2, i))) % 2) * pow(2, i)]);
function xor8(a, b) = xorn(a, b, 8);
function to_bits(v, n) = [for (i = [n - 1:-1:0]) bit(v, i)];

// ---------------------------------------------------------------------------
// Payload
// ---------------------------------------------------------------------------

function join(v, i = 0) = i >= len(v) ? "" : str(v[i], join(v, i + 1));

function wifi_payload() = str(
    "WIFI:T:", security, ";S:", join([for (c = ssid) (c == "\\" || c == ";" || c == "," || c == ":" || c == "\"") ? str("\\", c) : c]), ";",
    security == "nopass" ? "" : str("P:", join([for (c = password) (c == "\\" || c == ";" || c == "," || c == ":" || c == "\"") ? str("\\", c) : c]), ";"),
    "H:", hidden_ssid ? "true" : "false", ";;");

function payload() = mode == "wifi" ? wifi_payload() : mode == "url" ? url : text;

function utf8_cp(cp) =
    cp < 128 ? [cp] :
    cp < 2048 ? [192 + floor(cp / 64), 128 + cp % 64] :
    cp < 65536 ? [224 + floor(cp / 4096), 128 + floor(cp / 64) % 64, 128 + cp % 64] :
    [240 + floor(cp / 262144), 128 + floor(cp / 4096) % 64, 128 + floor(cp / 64) % 64, 128 + cp % 64];

function utf8(s) = [for (c = s) each utf8_cp(ord(c))];

// ---------------------------------------------------------------------------
// QR tables (ISO/IEC 18004 Table 9), versions 1-10.
// Per version, per level L, M, Q, H:
// [EC codewords per block, group-1 blocks, group-1 data codewords,
//  group-2 blocks, group-2 data codewords]
// ---------------------------------------------------------------------------

EC_BLOCKS = [
    [[7, 1, 19, 0, 0],   [10, 1, 16, 0, 0],  [13, 1, 13, 0, 0],  [17, 1, 9, 0, 0]],
    [[10, 1, 34, 0, 0],  [16, 1, 28, 0, 0],  [22, 1, 22, 0, 0],  [28, 1, 16, 0, 0]],
    [[15, 1, 55, 0, 0],  [26, 1, 44, 0, 0],  [18, 2, 17, 0, 0],  [22, 2, 13, 0, 0]],
    [[20, 1, 80, 0, 0],  [18, 2, 32, 0, 0],  [26, 2, 24, 0, 0],  [16, 4, 9, 0, 0]],
    [[26, 1, 108, 0, 0], [24, 2, 43, 0, 0],  [18, 2, 15, 2, 16], [22, 2, 11, 2, 12]],
    [[18, 2, 68, 0, 0],  [16, 4, 27, 0, 0],  [24, 4, 19, 0, 0],  [28, 4, 15, 0, 0]],
    [[20, 2, 78, 0, 0],  [18, 4, 31, 0, 0],  [18, 2, 14, 4, 15], [26, 4, 13, 1, 14]],
    [[24, 2, 97, 0, 0],  [22, 2, 38, 2, 39], [22, 4, 18, 2, 19], [26, 4, 14, 2, 15]],
    [[30, 2, 116, 0, 0], [22, 3, 36, 2, 37], [20, 4, 16, 4, 17], [24, 4, 12, 4, 13]],
    [[18, 2, 68, 2, 69], [26, 4, 43, 1, 44], [24, 6, 19, 2, 20], [28, 6, 15, 2, 16]],
];

ALIGN = [[], [6, 18], [6, 22], [6, 26], [6, 30], [6, 34],
         [6, 22, 38], [6, 24, 42], [6, 26, 46], [6, 28, 50]];

EC_NAMES = "LMQH";
EC_FORMAT = [1, 0, 3, 2];   // format-information bits for L, M, Q, H

function data_cw(v, e) = let(t = EC_BLOCKS[v - 1][e]) t[1] * t[2] + t[3] * t[4];
function count_bits(v) = v < 10 ? 8 : 16;
function byte_capacity(v, e) = floor((data_cw(v, e) * 8 - 4 - count_bits(v)) / 8);

function fit_version(n, e) = let(f = [for (v = [1:10]) if (byte_capacity(v, e) >= n) v]) len(f) ? f[0] : 0;

// Smallest version at the requested level; if the payload is too long even
// for version 10, step the error correction down until it fits.
function choose(n, e) = fit_version(n, e) > 0 ? [fit_version(n, e), e]
    : e > 0 ? choose(n, e - 1) : [10, 0];

// ---------------------------------------------------------------------------
// GF(256), primitive polynomial x^8 + x^4 + x^3 + x^2 + 1 (285)
// ---------------------------------------------------------------------------

function gf_next(v) = v * 2 >= 256 ? xorn(v * 2, 285, 9) : v * 2;
GF_EXP = [for (i = 0, v = 1; i < 255; i = i + 1, v = gf_next(v)) v];
GF_LOG = [for (x = [0:255]) x == 0 ? 0 : [for (i = [0:254]) if (GF_EXP[i] == x) i][0]];
function gf_mul(a, b) = (a == 0 || b == 0) ? 0 : GF_EXP[(GF_LOG[a] + GF_LOG[b]) % 255];

// Generator polynomial of degree d, coefficients highest first, leading 1 dropped.
function rs_gen(d) = rs_gen_step([for (i = [0:d - 1]) i == d - 1 ? 1 : 0], 1, 0, d);
function rs_gen_step(res, root, i, d) = i == d ? res :
    rs_gen_step([for (j = [0:d - 1]) let(m = gf_mul(res[j], root)) j + 1 < d ? xor8(m, res[j + 1]) : m],
                gf_mul(root, 2), i + 1, d);

function rs_rem(data, gen) = rs_rem_step(data, gen, 0, [for (g = gen) 0]);
function rs_rem_step(data, gen, i, res) = i == len(data) ? res :
    let(f = xor8(data[i], res[0]),
        sh = concat([for (j = [1:1:len(gen) - 1]) res[j]], [0]))
    rs_rem_step(data, gen, i + 1, [for (j = [0:len(gen) - 1]) xor8(sh[j], gf_mul(gen[j], f))]);

// ---------------------------------------------------------------------------
// Codewords
// ---------------------------------------------------------------------------

function data_codewords(bytes, v, e) =
    let(dcw = data_cw(v, e), cap = dcw * 8,
        b0 = concat([0, 1, 0, 0], to_bits(len(bytes), count_bits(v)),
                    [for (b = bytes) each to_bits(b, 8)]),
        term = min(4, cap - len(b0)),
        b1 = concat(b0, [for (i = [0:1:term - 1]) 0]),
        b2 = concat(b1, [for (i = [0:1:(8 - len(b1) % 8) % 8 - 1]) 0]),
        nb = len(b2) / 8,
        cws = [for (i = [0:1:nb - 1]) vsum([for (j = [0:7]) b2[i * 8 + j] * pow(2, 7 - j)])])
    concat(cws, [for (i = [0:1:dcw - nb - 1]) i % 2 == 0 ? 236 : 17]);

function all_codewords(dcws, v, e) =
    let(t = EC_BLOCKS[v - 1][e], ecn = t[0], g1n = t[1], g1k = t[2], g2n = t[3], g2k = t[4],
        nb = g1n + g2n,
        blocks = [for (b = [0:nb - 1])
            let(st = b < g1n ? b * g1k : g1n * g1k + (b - g1n) * g2k, k = b < g1n ? g1k : g2k)
            [for (i = [0:k - 1]) dcws[st + i]]],
        gen = rs_gen(ecn),
        ecs = [for (bl = blocks) rs_rem(bl, gen)],
        maxk = g2n > 0 ? g2k : g1k)
    concat([for (i = [0:maxk - 1]) for (b = [0:nb - 1]) if (i < len(blocks[b])) blocks[b][i]],
           [for (i = [0:ecn - 1]) for (b = [0:nb - 1]) ecs[b][i]]);

// ---------------------------------------------------------------------------
// Matrix. x is the column, y the row, (0, 0) top left.
// region(): -1 data module; 0/1 fixed function module; 100+i format bit i.
// ---------------------------------------------------------------------------

function bch(data, poly, deg) =
    let(r = bch_loop(data, poly, deg, deg)) data * pow(2, deg) + r;
function bch_loop(rem, poly, deg, k) = k == 0 ? rem :
    bch_loop(xorn(rem * 2, floor(rem / pow(2, deg - 1)) * poly, deg + 2), poly, deg, k - 1);

function format_word(e, m) = xorn(bch(EC_FORMAT[e] * 8 + m, 1335, 10), 21522, 15);
function version_word(v) = bch(v, 7973, 12);

function finder_val(x, y, cx, cy) = let(d = max(abs(x - cx), abs(y - cy))) (d <= 1 || d == 3) ? 1 : 0;

function align_val(x, y, al) =
    let(last = len(al) - 1,
        h = [for (i = [0:1:last], j = [0:1:last])
                if (!((i == 0 && j == 0) || (i == 0 && j == last) || (i == last && j == 0))
                    && abs(x - al[i]) <= 2 && abs(y - al[j]) <= 2)
                max(abs(x - al[i]), abs(y - al[j])) == 1 ? 0 : 1])
    len(h) ? h[0] : -1;

function region(x, y, n, v) =
    (x < 8 && y < 8) ? finder_val(x, y, 3, 3) :
    (x >= n - 8 && y < 8) ? finder_val(x, y, n - 4, 3) :
    (x < 8 && y >= n - 8) ? finder_val(x, y, 3, n - 4) :
    (x == 8 && y == n - 8) ? 1 :
    (x == 8 && y <= 8 && y != 6) ? 100 + (y < 6 ? y : y == 7 ? 6 : 7) :
    (y == 8 && x <= 7 && x != 6) ? 100 + (x == 7 ? 8 : 14 - x) :
    (y == 8 && x >= n - 8) ? 100 + (n - 1 - x) :
    (x == 8 && y >= n - 7) ? 100 + (y - n + 15) :
    (x == 6 || y == 6) ? (((x == 6 ? y : x) % 2 == 0) ? 1 : 0) :
    (v >= 7 && x >= n - 11 && x <= n - 9 && y <= 5) ? bit(version_word(v), x - (n - 11) + 3 * y) :
    (v >= 7 && y >= n - 11 && y <= n - 9 && x <= 5) ? bit(version_word(v), y - (n - 11) + 3 * x) :
    align_val(x, y, ALIGN[v - 1]);

// Column pairs are walked right to left, zig-zagging up and down; column 6
// (vertical timing) is skipped. seq_index() is a cell's position in that walk.
function pair_right(p, n) = let(r0 = n - 1 - 2 * p) r0 > 6 ? r0 : r0 - 1;
function upward(right) = floor((right + 1) / 2) % 2 == 0;
function seq_index(x, y, n) =
    let(right = x > 6 ? (x % 2 == 0 ? x : x + 1) : (x % 2 == 1 ? x : x + 1),
        p = right > 6 ? (n - 1 - right) / 2 : (n - 7) / 2 + (5 - right) / 2,
        vert = upward(right) ? n - 1 - y : y)
    p * 2 * n + vert * 2 + (right - x);

function mask_bit(m, x, y) = (
    m == 0 ? (x + y) % 2 == 0 :
    m == 1 ? y % 2 == 0 :
    m == 2 ? x % 3 == 0 :
    m == 3 ? (x + y) % 3 == 0 :
    m == 4 ? (floor(x / 3) + floor(y / 2)) % 2 == 0 :
    m == 5 ? (x * y) % 2 + (x * y) % 3 == 0 :
    m == 6 ? ((x * y) % 2 + (x * y) % 3) % 2 == 0 :
             ((x + y) % 2 + (x * y) % 3) % 2 == 0) ? 1 : 0;

// Unmasked matrix: function modules, format cells as 100+i, data bits placed.
function base_matrix(v, cws) =
    let(n = 17 + 4 * v,
        R = [for (y = [0:n - 1]) [for (x = [0:n - 1]) region(x, y, n, v)]],
        F = [for (p = [0:(n - 1) / 2 - 1]) let(right = pair_right(p, n), up = upward(right))
                for (vert = [0:n - 1]) for (j = [0:1])
                    let(x = right - j, y = up ? n - 1 - vert : vert) R[y][x] < 0 ? 1 : 0],
        P = [for (k = 0, c = 0; k < len(F); c = c + F[k], k = k + 1) c],
        nbits = len(cws) * 8)
    [for (y = [0:n - 1]) [for (x = [0:n - 1])
        R[y][x] >= 0 ? R[y][x] :
        let(r = P[seq_index(x, y, n)])
        // -2 = data 0, -1 = data 1 (sign keeps "data" distinct from function)
        (r < nbits && bit(cws[floor(r / 8)], 7 - r % 8) == 1) ? -1 : -2]];

function apply_mask(B, e, m) =
    let(n = len(B), fw = format_word(e, m))
    [for (y = [0:n - 1]) [for (x = [0:n - 1]) let(c = B[y][x])
        c >= 100 ? bit(fw, c - 100) :
        c >= 0 ? c :
        ((c == -1 ? 1 : 0) + mask_bit(m, x, y)) % 2]];

// ---- Penalty (ISO/IEC 18004 7.8.3); outside the symbol counts as light ----

function n1_line(l) =
    let(n = len(l),
        st = [for (i = [0:n - 1]) if (i == 0 || l[i] != l[i - 1]) i],
        en = concat([for (i = [1:1:len(st) - 1]) st[i]], [n]))
    vsum([for (i = [0:len(st) - 1]) let(L = en[i] - st[i]) L >= 5 ? L - 2 : 0]);

N3_A = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0];
N3_B = [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1];
function n3_line(l) =
    let(e = concat([0, 0, 0, 0], l, [0, 0, 0, 0]))
    vsum([for (i = [0:len(e) - 11])
        if (e[i + 4] == 1 && e[i + 6] == 1 && e[i + 5] == 0)
        let(w = [for (j = [0:10]) e[i + j]]) (w == N3_A ? 40 : 0) + (w == N3_B ? 40 : 0)]);

function penalty(M) =
    let(n = len(M), C = [for (x = [0:n - 1]) [for (y = [0:n - 1]) M[y][x]]],
        dark = vsum([for (r = M) vsum(r)]))
    vsum([for (r = M) n1_line(r)]) + vsum([for (c = C) n1_line(c)])
    + vsum([for (y = [0:n - 2], x = [0:n - 2])
            (M[y][x] == M[y][x + 1] && M[y][x] == M[y + 1][x] && M[y][x] == M[y + 1][x + 1]) ? 3 : 0])
    + vsum([for (r = M) n3_line(r)]) + vsum([for (c = C) n3_line(c)])
    + 10 * floor(abs(dark * 20 - n * n * 10) / (n * n));

// ---------------------------------------------------------------------------
// Encode
// ---------------------------------------------------------------------------

EC_REQ = error_correction == "L" ? 0 : error_correction == "Q" ? 2 : error_correction == "H" ? 3 : 1;
BYTES_ALL = utf8(payload());
CHOSEN = choose(len(BYTES_ALL), EC_REQ);
VER = CHOSEN[0];
ECL = CHOSEN[1];
// Truncation (only past 271 bytes) backs off to a whole UTF-8 character.
function utf8_cut(b, c) = (c >= len(b) || b[c] < 128 || b[c] >= 192) ? c : utf8_cut(b, c - 1);
BYTES = len(BYTES_ALL) > byte_capacity(VER, ECL)
    ? [for (i = [0:1:utf8_cut(BYTES_ALL, byte_capacity(VER, ECL)) - 1]) BYTES_ALL[i]] : BYTES_ALL;
CODEWORDS = all_codewords(data_codewords(BYTES, VER, ECL), VER, ECL);
BASE = base_matrix(VER, CODEWORDS);
PENALTIES = [for (m = [0:7]) penalty(apply_mask(BASE, ECL, m))];
MASK = search(min(PENALTIES), PENALTIES)[0];
QR = apply_mask(BASE, ECL, MASK);
N = len(QR);

// ---------------------------------------------------------------------------
// Layout
// ---------------------------------------------------------------------------

W = size;
R = W / 2;
has_caption = caption != "";
cred_lines = (mode == "wifi" && show_credentials) ? (security == "nopass" ? 1 : 2) : 0;
cap_fs0 = caption_frac * W;
cred_fs0 = cred_frac * W;
T = (has_caption ? cap_fs0 * line_pitch : 0) + cred_lines * cred_fs0 * line_pitch;
with_stand = stand == "desk_stand";

// Plain margin under the text; with the stand the bottom edge sits in its slot.
e_b = max(T > 0 ? max(2, 0.04 * W) : 0, with_stand ? slot_depth + 1.5 : 0);

// S: side of the code block including its quiet zone. The code block and the
// text band under it form an S x (S + T) rectangle; on a round plaque its
// corners sit on a circle 1 mm inside the rim.
Rr = R - 1;
S_round = min((-T + sqrt(8 * Rr * Rr - T * T)) / 2,
              with_stand ? 2 * (R - slot_depth - 1.5) - T : W);
S = shape == "round" ? S_round : W - T - e_b;
block_top = shape == "round" ? (S + T) / 2 : R;

QZ = S / (N + 2 * quiet_zone_full) >= min_module ? quiet_zone_full : quiet_zone_min;
MS = S / (N + 2 * QZ);
X0 = -S / 2 + QZ * MS;
Y0 = block_top - QZ * MS;          // top edge of row 0

text_w = shape == "round" ? S : shape == "rounded" ? 0.8 * W : 0.9 * W;
inlay = code_style == "inlay";
top_z = inlay ? thickness : thickness + module_height;
feat_z0 = inlay ? thickness - module_height : thickness;

corner_r = 0.1 * W;

// ---------------------------------------------------------------------------
// Geometry
// ---------------------------------------------------------------------------

module plate_2d() {
    if (shape == "round") circle(r = R, $fn = 128);
    else if (shape == "rounded") offset(r = corner_r) square(W - 2 * corner_r, center = true);
    else square(W, center = true);
}

// Dark modules, one rectangle per horizontal run.
module code_2d() {
    for (y = [0:N - 1]) {
        row = QR[y];
        for (x = [0:N - 1]) if (row[x] == 1 && (x == 0 || row[x - 1] == 0)) {
            ends = [for (k = [x:N - 1]) if (row[k] == 0) k];
            len_ = (len(ends) ? ends[0] : N) - x;
            translate([X0 + x * MS, Y0 - (y + 1) * MS]) square([len_ * MS, MS]);
        }
    }
}

// Shrink-only fit to width w, as in models/name-sign: resize() the text
// together with a hair-thin bar exactly w long, so text already narrower is
// left alone and wider text is scaled down (both axes) to w. The bar sits
// above the cut and is dropped by the projection.
module fit_x(w) {
    projection(cut = true) translate([0, 0, -0.5])
        resize([w, 0, 0], auto = [false, true, false])
            union() {
                linear_extrude(1) children();
                translate([-w / 2, 0, 10]) cube([w, 0.01, 0.01]);
            }
}

module text_line(s, fs0, font, yc) {
    translate([0, yc]) fit_x(text_w)
        text(s, size = fs0, font = font, halign = "center", valign = "center");
}

module text_2d() {
    y_start = block_top - S;
    if (has_caption)
        text_line(caption, cap_fs0, caption_font, y_start - cap_fs0 * line_pitch / 2);
    y_cred = y_start - (has_caption ? cap_fs0 * line_pitch : 0);
    if (cred_lines >= 1)
        text_line(str("SSID: ", ssid), cred_fs0, cred_font, y_cred - cred_fs0 * line_pitch / 2);
    if (cred_lines >= 2)
        text_line(str("Pass: ", password), cred_fs0, cred_font, y_cred - cred_fs0 * line_pitch * 1.5);
}

mag_depth = min(magnet_depth, thickness - (inlay ? module_height : 0) - 0.6);
mag_inset = magnet_diameter / 2 + 3 + (shape == "rounded" ? 0.35 * corner_r : 0);
mag_pos = shape == "round"
    ? [for (a = [45:90:315]) (R - magnet_diameter / 2 - 3) * [cos(a), sin(a)]]
    : [for (sx = [-1, 1], sy = [-1, 1]) [sx, sy] * (R - mag_inset)];
use_magnets = magnet_pockets && mag_depth >= 0.8;

module plate_3d() {
    difference() {
        linear_extrude(height = thickness) plate_2d();
        if (inlay)
            translate([0, 0, feat_z0]) linear_extrude(height = module_height + 1) {
                code_2d();
                text_2d();
            }
        if (use_magnets)
            for (p = mag_pos) translate([p[0], p[1], -1]) cylinder(d = magnet_diameter, h = mag_depth + 1);
    }
}

// Desk stand: a block with a slot leaning back by slot_tilt, printed upright
// (slot opening up) in front of the plaque.
stand_len = max(40, 0.6 * W);
slot_w = thickness + stand_clearance;
slot_y = 9;
stand_depth = max(30, slot_y + 0.15 * W + 8);
stand_h = slot_depth + 4;

module stand_3d() {
    difference() {
        translate([-stand_len / 2, 0, 0]) cube([stand_len, stand_depth, stand_h]);
        translate([0, slot_y, stand_h - slot_depth]) rotate([-slot_tilt, 0, 0])
            translate([-stand_len, -slot_w / 2, 0]) cube([2 * stand_len, slot_w, 3 * stand_h]);
    }
}

echo(str("QRINFO;", VER, ";", EC_NAMES[ECL], ";", MASK, ";", len(BYTES), ";", len(BYTES_ALL), ";",
         QZ, ";", MS, ";", X0, ";", Y0, ";", N, ";", top_z));
echo(str("QRPEN;", PENALTIES));
for (y = [0:N - 1]) echo(str("QRROW;", y, ";", chr([for (v = QR[y]) v == 1 ? 49 : 48])));
if (ECL != EC_REQ)
    echo(str("WARNING: payload too long for error correction ", error_correction,
             " at version 10; using ", EC_NAMES[ECL]));
if (len(BYTES) < len(BYTES_ALL))
    echo(str("WARNING: payload truncated to ", len(BYTES), " bytes (QR version 10 limit)"));
if (magnet_pockets && !use_magnets)
    echo("WARNING: plaque too thin for magnet pockets; pockets omitted");
if (MS < 0.8)
    echo(str("WARNING: modules are ", MS, " mm; below about 0.8 mm a 0.4 mm nozzle cannot print them cleanly"));
if (QZ < quiet_zone_full)
    echo(str("WARNING: quiet zone reduced to ", QZ, " modules to keep modules >= ", min_module, " mm"));

color(plate_color) {
    plate_3d();
    if (with_stand)
        translate([0, -(shape == "round" ? R : R) - stand_gap - stand_depth, 0]) stand_3d();
}

color(code_color)
    translate([0, 0, feat_z0]) linear_extrude(height = module_height) code_2d();

if (has_caption || cred_lines > 0)
    color(caption_color)
        translate([0, 0, feat_z0]) linear_extrude(height = module_height) text_2d();
