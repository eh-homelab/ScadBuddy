// Kids' name puzzle — a tray with a pocket shaped like each letter of a name,
// and a chunky letter piece for each pocket. The letters are thicker than the
// pockets are deep, so they stand proud of the tray for small fingers to grab.
//
// Everything prints flat on one plate with no supports: the tray, with the
// letter pieces laid out in a row in front of it. Each pocket is the letter's
// outline grown by `clearance`; the counters of A, B, O and friends stay in
// the tray as islands that poke up through the letter.
//
// Tray shapes: a rounded rectangle, a cloud, or a train — a locomotive
// pulling one wagon per letter.
//
// Written to the MakerWorld Parametric Model Maker customizer conventions so
// the same file works unchanged on MakerWorld and in ScadBuddy.
//
// The colour parameters are the extruder order: tray_color is extruder 1,
// letter_color_1 .. letter_color_4 are extruders 2 to 5. Letter n (counting
// from 1, spaces skipped) takes letter_color_((n-1) mod 4 + 1), so a
// three-letter name prints in four colours. Equal colours merge into one
// filament.

/* [Text] */

// Name to spell (spaces are skipped); capitals make the chunkiest pieces
name = "MIA"; // 10

// Typeface: DejaVu Sans Bold or Lobster Two Bold are measured for tight pockets; any other face is shrunk into generic cells
font = "DejaVu Sans:style=Bold"; // font

// Height of a capital letter in mm
letter_size = 40; // [25:5:70]

/* [Puzzle] */

// Solid floor under the pockets in mm
tray_thickness = 4; // [3:0.5:8]

// Depth of the letter pockets in mm (capped 1 mm below the letter thickness)
pocket_depth = 4; // [2:0.5:8]

// Thickness of the letter pieces in mm; the extra over the pocket depth stands proud
letter_thickness = 7; // [4:0.5:12]

// Gap between a letter and its pocket wall, per side, in mm
clearance = 0.4; // [0.2:0.05:0.8]

// Add a grab knob on top of each letter
knobs = false;

// Tray outline
tray_shape = "rounded_rect"; // [rounded_rect:Rounded rectangle, cloud:Cloud, train:Train (one wagon per letter)]

// Tray border around the pockets in mm
tray_margin = 8; // [4:1:20]

/* [Colors] */

// Tray colour (extruder 1)
tray_color = "#FFE08A"; // color

// Letters 1, 5, 9 (extruder 2)
letter_color_1 = "#E53935"; // color

// Letters 2, 6, 10 (extruder 3)
letter_color_2 = "#1E88E5"; // color

// Letters 3, 7 (extruder 4)
letter_color_3 = "#43A047"; // color

// Letters 4, 8 (extruder 5)
letter_color_4 = "#8E24AA"; // color

/* [Hidden] */

$fn = 48;

// Wall between neighbouring pockets.
wall = 4;
// Extra gap where the name has a space (rounded rectangle and cloud).
space_gap_k = 0.35;
// Radius the letter corners are rounded to, so no piece has a sharp point.
letter_round = 0.8;
// Knob: height, and the radius of the patch of letter it covers.
knob_h = 6;
knob_r_k = 0.14;
// Gap between the tray and the row of loose letters on the plate.
part_gap = 8;
// Train: gap between wagons (bridged by a coupling bar).
coupler = 8;
// Place the letters in their pockets instead of beside the tray (for checking fit).
assembled = false;

// Glyph metrics at size 1, measured from the fonts in the ScadBuddy image with
// halign="center", valign="center": [char, xmin, xmax, ymin, ymax, knob x,
// knob y, knob radius]. The knob point is the centre of the largest circle
// that fits inside the glyph (biased towards the middle).
DEJAVU = "DejaVu Sans:style=Bold";
LOBSTER = "Lobster Two:style=Bold";
DEJ_T = [
    ["d", -0.435, 0.380, -0.538, 0.538, 0.242, 0.043, 0.138],
    ["e", -0.411, 0.404, -0.399, 0.399, -0.275, 0.010, 0.135],
    ["f", -0.276, 0.314, -0.528, 0.528, -0.033, 0.145, 0.149],
    ["g", -0.435, 0.380, -0.538, 0.538, 0.242, -0.022, 0.138],
    ["h", -0.378, 0.386, -0.528, 0.528, -0.239, 0.029, 0.139],
    ["i", -0.121, 0.121, -0.528, 0.528, -0.000, 0.000, 0.121],
    ["j", -0.284, 0.121, -0.677, 0.677, -0.000, 0.000, 0.121],
    ["k", -0.345, 0.488, -0.528, 0.528, -0.210, -0.129, 0.135],
    ["l", -0.121, 0.121, -0.528, 0.528, -0.000, 0.000, 0.121],
    ["m", -0.608, 0.614, -0.389, 0.389, 0.010, 0.138, 0.137],
    ["n", -0.378, 0.386, -0.389, 0.389, -0.239, 0.167, 0.139],
    ["o", -0.417, 0.417, -0.399, 0.399, 0.286, 0.067, 0.125],
    ["p", -0.380, 0.435, -0.533, 0.533, -0.244, -0.039, 0.136],
    ["q", -0.435, 0.380, -0.532, 0.532, 0.242, -0.046, 0.138],
    ["r", -0.226, 0.338, -0.389, 0.389, -0.086, 0.150, 0.140],
    ["s", -0.341, 0.348, -0.399, 0.399, 0.216, -0.083, 0.116],
    ["t", -0.314, 0.300, -0.488, 0.488, -0.071, 0.185, 0.149],
    ["u", -0.386, 0.378, -0.390, 0.390, 0.239, -0.169, 0.139],
    ["v", -0.432, 0.432, -0.380, 0.380, 0.050, -0.257, 0.122],
    ["w", -0.593, 0.593, -0.380, 0.380, -0.004, 0.261, 0.119],
    ["x", -0.427, 0.427, -0.380, 0.380, -0.000, 0.000, 0.140],
    ["y", -0.436, 0.428, -0.530, 0.530, 0.012, -0.135, 0.149],
    ["z", -0.342, 0.338, -0.380, 0.380, 0.186, 0.246, 0.134],
    ["0", -0.417, 0.417, -0.525, 0.525, 0.283, -0.067, 0.129],
    ["1", -0.327, 0.388, -0.506, 0.506, 0.033, 0.000, 0.123],
    ["2", -0.373, 0.362, -0.516, 0.516, -0.216, -0.374, 0.142],
    ["3", -0.390, 0.373, -0.525, 0.525, 0.236, -0.165, 0.129],
    ["4", -0.421, 0.419, -0.506, 0.506, 0.151, -0.227, 0.155],
    ["5", -0.376, 0.386, -0.516, 0.516, 0.237, -0.062, 0.129],
    ["6", -0.397, 0.409, -0.524, 0.524, -0.243, -0.024, 0.153],
    ["7", -0.390, 0.373, -0.506, 0.506, 0.068, -0.031, 0.116],
    ["8", -0.398, 0.397, -0.525, 0.525, 0.268, -0.166, 0.120],
    ["9", -0.411, 0.394, -0.524, 0.524, 0.241, 0.023, 0.153],
    ["A", -0.531, 0.531, -0.506, 0.506, 0.265, -0.195, 0.141],
    ["B", -0.402, 0.432, -0.506, 0.506, -0.256, 0.025, 0.145],
    ["C", -0.440, 0.421, -0.525, 0.525, -0.300, -0.068, 0.134],
    ["D", -0.449, 0.504, -0.506, 0.506, 0.355, 0.121, 0.134],
    ["E", -0.347, 0.373, -0.506, 0.506, -0.198, 0.020, 0.148],
    ["F", -0.347, 0.358, -0.506, 0.506, -0.198, 0.020, 0.148],
    ["G", -0.501, 0.468, -0.525, 0.525, -0.361, -0.068, 0.134],
    ["H", -0.454, 0.454, -0.506, 0.506, 0.304, 0.022, 0.149],
    ["I", -0.131, 0.130, -0.506, 0.506, -0.000, 0.000, 0.130],
    ["J", -0.336, 0.130, -0.645, 0.645, -0.000, -0.000, 0.130],
    ["K", -0.411, 0.580, -0.506, 0.506, -0.252, 0.015, 0.159],
    ["L", -0.315, 0.404, -0.506, 0.506, -0.184, 0.000, 0.130],
    ["M", -0.564, 0.563, -0.506, 0.506, -0.000, -0.168, 0.132],
    ["N", -0.454, 0.454, -0.506, 0.506, -0.285, 0.338, 0.169],
    ["O", -0.521, 0.520, -0.525, 0.525, -0.381, 0.077, 0.134],
    ["P", -0.381, 0.452, -0.506, 0.506, -0.234, -0.060, 0.148],
    ["Q", -0.521, 0.520, -0.617, 0.617, -0.381, 0.013, 0.134],
    ["R", -0.407, 0.507, -0.506, 0.506, -0.261, -0.032, 0.146],
    ["S", -0.400, 0.398, -0.525, 0.525, 0.035, -0.000, 0.114],
    ["T", -0.467, 0.466, -0.506, 0.506, -0.000, 0.000, 0.131],
    ["U", -0.436, 0.436, -0.516, 0.516, 0.306, 0.000, 0.131],
    ["V", -0.531, 0.531, -0.506, 0.506, -0.058, -0.367, 0.139],
    ["W", -0.725, 0.722, -0.506, 0.506, 0.362, -0.361, 0.145],
    ["X", -0.509, 0.508, -0.506, 0.506, -0.001, -0.001, 0.157],
    ["Y", -0.516, 0.516, -0.506, 0.506, -0.000, -0.005, 0.150],
    ["Z", -0.441, 0.441, -0.506, 0.506, 0.242, 0.358, 0.148],
    ["a", -0.409, 0.359, -0.399, 0.399, 0.226, 0.034, 0.133],
    ["b", -0.380, 0.435, -0.538, 0.538, -0.243, 0.042, 0.137],
    ["c", -0.352, 0.319, -0.399, 0.399, -0.218, -0.076, 0.125]
];
LOB_T = [
    ["d", -0.329, 0.367, -0.490, 0.490, 0.167, 0.000, 0.100],
    ["e", -0.234, 0.237, -0.355, 0.355, -0.129, -0.034, 0.104],
    ["f", -0.319, 0.287, -0.694, 0.694, -0.020, -0.000, 0.100],
    ["g", -0.311, 0.287, -0.524, 0.524, 0.178, -0.004, 0.110],
    ["h", -0.295, 0.360, -0.490, 0.490, -0.184, 0.071, 0.111],
    ["i", -0.143, 0.189, -0.499, 0.499, -0.028, 0.000, 0.100],
    ["j", -0.284, 0.110, -0.669, 0.669, 0.002, -0.000, 0.100],
    ["k", -0.306, 0.371, -0.490, 0.490, -0.190, -0.078, 0.116],
    ["l", -0.124, 0.188, -0.490, 0.490, -0.024, 0.000, 0.100],
    ["m", -0.467, 0.532, -0.355, 0.355, -0.024, 0.000, 0.100],
    ["n", -0.295, 0.360, -0.354, 0.354, -0.195, 0.000, 0.100],
    ["o", -0.292, 0.330, -0.355, 0.355, -0.188, -0.050, 0.102],
    ["p", -0.260, 0.324, -0.547, 0.546, -0.160, -0.000, 0.100],
    ["q", -0.296, 0.299, -0.524, 0.524, 0.188, -0.040, 0.111],
    ["r", -0.196, 0.274, -0.351, 0.351, -0.096, -0.000, 0.100],
    ["s", -0.240, 0.238, -0.373, 0.373, 0.102, 0.044, 0.100],
    ["t", -0.169, 0.187, -0.461, 0.461, -0.025, 0.000, 0.100],
    ["u", -0.292, 0.351, -0.351, 0.351, 0.149, -0.143, 0.104],
    ["v", -0.222, 0.275, -0.365, 0.365, -0.122, 0.000, 0.100],
    ["w", -0.394, 0.447, -0.365, 0.365, 0.049, 0.000, 0.100],
    ["x", -0.310, 0.374, -0.396, 0.396, 0.091, -0.083, 0.096],
    ["y", -0.269, 0.274, -0.521, 0.521, 0.164, -0.021, 0.110],
    ["z", -0.222, 0.319, -0.348, 0.347, 0.045, 0.007, 0.086],
    ["0", -0.352, 0.351, -0.525, 0.525, -0.232, 0.122, 0.105],
    ["1", -0.209, 0.124, -0.520, 0.519, 0.024, -0.000, 0.100],
    ["2", -0.301, 0.311, -0.528, 0.528, 0.123, 0.111, 0.099],
    ["3", -0.327, 0.331, -0.524, 0.524, 0.214, -0.136, 0.108],
    ["4", -0.333, 0.306, -0.521, 0.521, 0.118, -0.091, 0.115],
    ["5", -0.310, 0.315, -0.524, 0.524, 0.164, -0.019, 0.093],
    ["6", -0.322, 0.340, -0.525, 0.525, -0.222, -0.002, 0.091],
    ["7", -0.308, 0.327, -0.526, 0.526, -0.046, -0.145, 0.090],
    ["8", -0.331, 0.340, -0.525, 0.525, 0.233, -0.154, 0.097],
    ["9", -0.341, 0.322, -0.525, 0.525, 0.219, -0.025, 0.091],
    ["A", -0.478, 0.355, -0.600, 0.600, -0.144, -0.023, 0.077],
    ["B", -0.447, 0.458, -0.533, 0.533, -0.072, 0.058, 0.103],
    ["C", -0.285, 0.455, -0.532, 0.532, -0.165, -0.077, 0.113],
    ["D", -0.466, 0.455, -0.527, 0.527, -0.094, 0.000, 0.101],
    ["E", -0.253, 0.413, -0.534, 0.535, -0.133, -0.225, 0.118],
    ["F", -0.358, 0.403, -0.521, 0.521, 0.007, 0.022, 0.102],
    ["G", -0.366, 0.362, -0.664, 0.665, -0.246, 0.112, 0.105],
    ["H", -0.457, 0.418, -0.521, 0.521, -0.080, 0.049, 0.106],
    ["I", -0.255, 0.216, -0.521, 0.521, 0.116, -0.000, 0.100],
    ["J", -0.306, 0.237, -0.664, 0.665, 0.139, 0.000, 0.098],
    ["K", -0.426, 0.458, -0.533, 0.533, -0.052, 0.050, 0.103],
    ["L", -0.291, 0.795, -0.687, 0.688, 0.079, 0.000, 0.101],
    ["M", -0.518, 0.504, -0.521, 0.521, -0.126, 0.227, 0.130],
    ["N", -0.471, 0.435, -0.521, 0.521, -0.072, 0.303, 0.132],
    ["O", -0.460, 0.447, -0.525, 0.525, -0.159, -0.076, 0.103],
    ["P", -0.393, 0.415, -0.521, 0.521, -0.022, -0.000, 0.100],
    ["Q", -0.460, 0.522, -0.599, 0.599, -0.159, -0.003, 0.103],
    ["R", -0.429, 0.454, -0.533, 0.533, -0.056, 0.047, 0.103],
    ["S", -0.322, 0.355, -0.533, 0.534, 0.192, -0.107, 0.112],
    ["T", -0.439, 0.489, -0.520, 0.520, 0.026, -0.000, 0.101],
    ["U", -0.512, 0.473, -0.526, 0.526, -0.092, 0.000, 0.099],
    ["V", -0.472, 0.502, -0.521, 0.521, 0.027, 0.003, 0.099],
    ["W", -0.677, 0.685, -0.521, 0.521, 0.189, 0.302, 0.124],
    ["X", -0.426, 0.453, -0.521, 0.521, 0.132, 0.038, 0.113],
    ["Y", -0.427, 0.467, -0.521, 0.521, 0.177, -0.073, 0.113],
    ["Z", -0.361, 0.390, -0.521, 0.521, 0.005, -0.001, 0.101],
    ["a", -0.330, 0.366, -0.355, 0.355, 0.166, 0.000, 0.100],
    ["b", -0.244, 0.290, -0.490, 0.490, -0.144, -0.001, 0.100],
    ["c", -0.233, 0.238, -0.355, 0.355, -0.133, -0.040, 0.098]
];

s = letter_size;
cl = clearance;
m = tray_margin;
pd = min(pocket_depth, letter_thickness - 1);
tray_h = tray_thickness + pd;
known_font = font == DEJAVU || font == LOBSTER;
TBL = font == LOBSTER ? LOB_T : DEJ_T;

// Letters, spaces dropped; each entry is [char, follows a space].
function nonspace_before(i) = len([for (j = [0:1:i - 1]) if (name[j] != " ") 1]) > 0;
L = len(name) == 0 ? [] :
    [for (i = [0:len(name) - 1]) if (name[i] != " ")
        [name[i], i > 0 && name[i - 1] == " " && nonspace_before(i)]];
n = len(L);

// Metrics of a character; characters (or faces) not in the table get a
// generic cell and are shrunk into it.
GENERIC = ["?", -0.45, 0.45, -0.55, 0.55, 0, 0, 0.1];
function met(c) = let(i = search(c, TBL))
    len(i) > 0 ? TBL[i[0]] : GENERIC;
function in_tbl(c) = known_font && len(search(c, TBL)) > 0;
function gw(k) = let(t = met(L[k][0])) (t[2] - t[1]) * s;     // glyph width
function gh(k) = let(t = met(L[k][0])) (t[4] - t[3]) * s;     // glyph height
function pw(k) = gw(k) + 2 * cl;                             // pocket width
cell_h = (n == 0 ? s : max([for (k = [0:n - 1]) gh(k)])) + 2 * cl;
H = cell_h + 2 * m;                                          // tray depth (y)

function sum(v, i) = i <= 0 ? 0 : sum(v, i - 1) + v[i - 1];

// Pocket centres along x, and the tray's inner span.
space_gap = space_gap_k * s;
PW = [for (k = [0:1:n - 1]) pw(k)];
is_train = tray_shape == "train";
STEP = [for (k = [0:1:n - 1])
    PW[k] + (is_train ? 2 * m + coupler : wall)
    + (!is_train && k + 1 < n && L[k + 1][1] ? space_gap : 0)];
span = n == 0 ? s * 0.6 : sum(STEP, n) - (is_train ? 2 * m + coupler : wall);
X0 = -span / 2;
function cx(k) = X0 + sum(STEP, k) + PW[k] / 2;

// ---------------------------------------------------------------- letters

module fit(w, h) {
    // Shrink-only uniform fit into w x h (see name-sign): resize the glyph
    // together with a hair-thin w x h cross so smaller glyphs are untouched.
    projection(cut = true) translate([0, 0, -0.5])
        resize([w, h, 0], auto = [false, false, false])
            union() {
                linear_extrude(1) children();
                translate([-w / 2, 0, 10]) cube([w, 0.01, 0.01]);
                translate([0, -h / 2, 10]) cube([0.01, h, 0.01]);
            }
}

// Glyph k with its bounding box centred on the origin, corners rounded.
module glyph_2d(k) {
    c = L[k][0];
    t = met(c);
    offset(r = letter_round) offset(delta = -letter_round)
        if (in_tbl(c))
            translate([-(t[1] + t[2]) / 2 * s, 0])
                text(c, size = s, font = font, halign = "center", valign = "center");
        else
            fit(gw(k), gh(k))
                text(c, size = s, font = font, halign = "center", valign = "center");
}

module knob_2d(k) {
    t = met(L[k][0]);
    intersection() {
        translate([(t[5] - (t[1] + t[2]) / 2) * s, t[6] * s]) circle(r = knob_r_k * s);
        offset(delta = -1.2) glyph_2d(k);
    }
}

module letter_3d(k) {
    linear_extrude(letter_thickness) glyph_2d(k);
    if (knobs)
        translate([0, 0, letter_thickness - 0.01]) linear_extrude(knob_h + 0.01) knob_2d(k);
}

LCOL = [letter_color_1, letter_color_2, letter_color_3, letter_color_4];

// ---------------------------------------------------------------- trays

function corner_r() = max(2, min((sqrt(2) * m - 3) / (sqrt(2) - 1), H / 2 - 0.01));

module rrect(w, h, r) {
    rr = min(r, w / 2 - 0.01, h / 2 - 0.01);
    offset(r = rr) square([w - 2 * rr, h - 2 * rr], center = true);
}

Lr = span + 2 * m;

module rect_tray_2d() { rrect(Lr, H, corner_r()); }

// Cloud: the rounded rectangle with round bumps along the top and bottom and
// at each end, concave joins filleted.
cloud_rb = H * 0.3;
cloud_re = H * 0.42;
module cloud_tray_2d() {
    nb = max(2, round(Lr / (1.7 * cloud_rb)));
    offset(r = -3) offset(r = 3) union() {
        rect_tray_2d();
        for (i = [0:nb - 1], sy = [-1, 1])
            translate([-Lr / 2 + cloud_rb + i * (Lr - 2 * cloud_rb) / (nb - 1),
                       sy * (H / 2 - 0.35 * cloud_rb)])
                circle(r = cloud_rb);
        for (sx = [-1, 1]) translate([sx * (Lr / 2 - H * 0.25), 0]) circle(r = cloud_re);
    }
}

// Train: a wagon per letter, coupled together, pulled by a locomotive.
wr = max(5, min(12, 0.16 * H));                     // wheel radius
Le = max(0.9 * H, 30);                               // locomotive length
function wagon_w(k) = PW[k] + 2 * m;
module wagon_2d(k) {
    ww = wagon_w(k);
    translate([cx(k), 0]) {
        rrect(ww, H, 4);
        for (sx = [-1, 1]) translate([sx * max(0, ww / 2 - wr - 2), -H / 2]) circle(r = wr);
    }
}
bar_h = max(6, 0.16 * H);
bar_y = -H / 2 + 0.22 * H;
eng_x1 = X0 - m - coupler;                           // back of the locomotive
module engine_2d() {
    b = -H / 2;
    translate([eng_x1 - Le, 0]) offset(r = 2) offset(delta = -2) union() {
        translate([0.06 * Le, b]) square([Le * 0.94, 0.28 * H]);          // chassis
        hull() {                                                         // boiler
            translate([0.1 * Le, b]) square([0.55 * Le, 0.62 * H]);
            translate([0.06 * Le + 0.12 * H, b + 0.5 * H]) circle(r = 0.12 * H);
        }
        translate([0.58 * Le, b]) square([0.42 * Le, H]);                // cab
        translate([0.52 * Le, H / 2 - 0.12 * H]) square([0.48 * Le, 0.12 * H]);  // roof
        hull() {                                                         // chimney
            translate([0.16 * Le, b + 0.5 * H]) square([0.12 * Le, 0.01]);
            translate([0.12 * Le, H * 0.4]) square([0.2 * Le, 0.1 * H]);
        }
        polygon([[0.1 * Le, b], [-0.02 * Le, b], [0.1 * Le, b + 0.3 * H]]);  // cowcatcher
        for (x = [0.28, 0.76]) translate([x * Le, b + 0.2 * wr]) circle(r = wr * 1.2);
    }
}
// Cab window: a shallow recess.
module engine_window_2d() {
    translate([eng_x1 - 0.21 * Le, H / 2 - 0.3 * H])
        rrect(0.22 * Le, 0.22 * H, 2);
}
module train_tray_2d() {
    engine_2d();
    for (k = [0:1:n - 1]) wagon_2d(k);
    // Coupling bars from the locomotive through every wagon.
    if (n > 0) translate([eng_x1 - 1, bar_y - bar_h / 2])
        square([(n == 0 ? 0 : cx(n - 1)) - eng_x1 + 1, bar_h]);
}

module tray_2d() {
    if (tray_shape == "cloud") cloud_tray_2d();
    else if (is_train) train_tray_2d();
    else rect_tray_2d();
}

// Lowest point of the tray outline, for placing the loose letters.
tray_ymin = tray_shape == "cloud" ? -(H / 2 + 0.65 * cloud_rb)
          : is_train ? -(H / 2 + wr) : -H / 2;

// ---------------------------------------------------------------- model

color(tray_color) difference() {
    linear_extrude(tray_h) tray_2d();
    for (k = [0:1:n - 1])
        translate([cx(k), 0, tray_thickness])
            linear_extrude(pd + 1) offset(r = cl) glyph_2d(k);
    if (is_train)
        translate([0, 0, tray_h - 1.5]) linear_extrude(2) engine_window_2d();
}

letters_y = tray_ymin - part_gap - cell_h / 2;
for (k = [0:1:n - 1])
    color(LCOL[k % 4])
        translate(assembled ? [cx(k), 0, tray_thickness] : [cx(k), letters_y, 0])
            letter_3d(k);

if (span + 2 * m + (is_train ? Le : 0) > 250)
    echo(str("NOTE: the puzzle is ", round(span + 2 * m + (is_train ? Le : 0)),
             " mm long; shorten the name or reduce letter_size to fit a 256 mm bed"));
