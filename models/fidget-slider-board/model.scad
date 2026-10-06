// Fidget slider board — a print-in-place sliding fidget toy. A board in the
// shape of a heart, star, dinosaur, rounded rectangle or circle carries rows of
// straight tracks, and every track holds a row of round-knobbed beads that
// slide from end to end. There is always an empty slot in each row, so the
// beads can be pushed back and forth. The beads print already inside their
// tracks and come off the plate free.
//
// How a bead is held (the cross-section across a track):
//   - the bead's stem runs through a slot in the bottom of the board, widens
//     at 45 degrees to a wide middle inside the board, narrows at 45 degrees
//     to a stem through a slot in the top, and ends in a round knob;
//   - the track is the same shape `clearance` bigger all round, closed at both
//     ends. The wide middle cannot pass either slot, so a bead cannot lift
//     out, drop out or leave its track; it can only slide along it;
//   - the knob's underside is a 45-degree cone that starts `clearance` above
//     the board, so every overhang, on the bead and on the board, is 45
//     degrees or less and nothing needs support;
//   - the bottom 0.3 mm of every bead is set in and the bottom slot is flared
//     by 0.3 mm, so first-layer squish cannot weld a bead to its track.
// verify.sh measures every gap on the rendered geometry.
//
// Where the rows go: the tracks are fitted into the shape, at least 3 mm of
// board from its edge. Each row gets as many beads as fit, up to
// beads_per_row, and the stack of rows (and the name, if there is one) is
// slid up and down the shape to where the most beads fit. The render log
// says (NOTE:) when a row is dropped or holds fewer beads than asked.
//
// Prints flat, as it lies, with no supports and no brim.
//
// Written to the MakerWorld Parametric Model Maker customizer conventions so
// the same file works unchanged on MakerWorld and in ScadBuddy.
//
// The colour parameters are the extruder order: board_color is extruder 1,
// bead_color_1 .. bead_color_8 are extruders 2-9, name_color is extruder 10.
// Only the first bead_colors bead colours are used; colours that are equal
// merge into one part and one filament.

/* [Board] */

// Board outline
shape = "heart"; // [heart:Heart, star:Star, dinosaur:Dinosaur, rounded_rectangle:Rounded rectangle, circle:Circle]

// Board size in mm across its longest side
board_size = 180; // [100:5:280]

/* [Beads] */

// Rows of tracks (rows that do not fit the shape are dropped)
rows = 4; // [1:1:10]

// Beads in each row (a row that is too short for them holds fewer)
beads_per_row = 4; // [1:1:10]

// Knob diameter of each bead in mm; tracks are this far apart plus 2 mm
bead_size = 15; // [12:1:20]

// Empty room left in every row, in bead places, so the beads can slide
free_places = 1; // [0.5:0.5:3]

// Gap per side between each bead and its track, in mm (raise it if beads print stuck; lower it if they rattle)
clearance = 0.4; // [0.25:0.05:0.6]

/* [Colours] */

// How the bead colours are spread over the grid
color_pattern = "diagonal"; // [rows:One colour per row, columns:One colour per column, diagonal:Rainbow diagonals]

// How many of the bead colours below to use
bead_colors = 6; // [1:1:8]

// Board colour (extruder 1)
board_color = "#F8BBD0"; // color

// Bead colour 1 (extruder 2)
bead_color_1 = "#E53935"; // color

// Bead colour 2 (extruder 3)
bead_color_2 = "#FB8C00"; // color

// Bead colour 3 (extruder 4)
bead_color_3 = "#FDD835"; // color

// Bead colour 4 (extruder 5)
bead_color_4 = "#43A047"; // color

// Bead colour 5 (extruder 6)
bead_color_5 = "#1E88E5"; // color

// Bead colour 6 (extruder 7)
bead_color_6 = "#8E24AA"; // color

// Bead colour 7 (extruder 8)
bead_color_7 = "#00ACC1"; // color

// Bead colour 8 (extruder 9)
bead_color_8 = "#EC407A"; // color

// Name colour (extruder 10)
name_color = "#6A1B9A"; // color

/* [Name] */

// Name inlaid flush into the board below the rows (leave empty for none)
name = "MIA"; // 12

// Typeface for the name (the app fills this dropdown from the fonts installed in the image)
font = "DejaVu Sans:style=Bold"; // font

// Largest letter height in mm (smaller if the name is long for the board)
name_size = 13; // [8:1:30]

/* [Hidden] */

$fn = 48;

// Board thickness; the bead profile below is built to fit it.
T = 8;
// Bead profile, half-widths across the track and heights: a stem of half
// width S0 on the bed up to Z1, 45 degrees out to half width S1 at Z2, straight
// up to Z3, 45 degrees in to S0 at Z4, then the stem up through the top.
S0 = 2.2;
S1 = 4.2;
Z1 = 1.4;
Z2 = Z1 + (S1 - S0);
Z3 = Z2 + 1.2;
Z4 = Z3 + (S1 - S0);
// Bottom-edge set-in against elephant's foot.
foot = 0.3;
// Board left between a track (or a knob) and the outline, and around the name.
wall = 3;
// Top-edge rounding of the board.
edge_round = 1.5;
round_steps = 3;
// Name inlay depth.
inlay = 0.6;
// Gap between neighbouring knobs along a row, and between rows of knobs.
knob_gap = 1;
row_gap = 2;
// A bead's body is 2 mm shorter than its knob; the track ends 1 mm past the
// end beads' bodies.
body_short = 2;
end_gap = 1;
// Search step when sliding the stack of rows up and down the shape.
search_step = 2;
// Plate the board has to fit.
bed_w = 300;
bed_d = 320;

// Test hooks for verify.sh. probe_gap > 0 renders, for every bead, the bead
// grown by probe_gap intersected with the board and with the next bead in its
// row. probe_move = [dx, dy, dz] renders every bead moved by that much
// intersected with the board. probe_slide renders each row's beads swept by
// the free room along the track (grown by probe_gap) intersected with the
// board. probe_wall renders any part of the rows' tracks and knobs, grown by
// probe_wall (the name by a true distance), that lies outside the outline.
probe_gap = 0;
probe_move = [0, 0, 0];
probe_slide = false;
probe_wall = 0;

D = bead_size;
c = clearance;
g = c;                      // knob cone starts this far above the board
PITCH = D + knob_gap;       // bead pitch along a row
BODY = D - body_short;      // bead body length along the track
ROW_P = D + row_gap;        // row pitch
// Half-height of the band a row needs: the wider of its knob and its track,
// plus the wall.
ROW_HALF = max(D / 2, S1 + c) + wall;
KNOB_UP = D / 2 - S0;       // height of the knob's 45-degree cone

// ---------------------------------------------------------------- outlines

function chaikin(p) = let(n = len(p))
    [for (i = [0:n - 1]) let(a = p[i], b = p[(i + 1) % n]) each [0.75 * a + 0.25 * b, 0.25 * a + 0.75 * b]];
function chaikin_n(p, k) = k <= 0 ? p : chaikin_n(chaikin(p), k - 1);

// Scale a point list so its longer side is board_size, centred on the origin.
function fit(p) = let(
    xs = [for (q = p) q.x], ys = [for (q = p) q.y],
    w = max(xs) - min(xs), h = max(ys) - min(ys),
    k = board_size / max(w, h),
    cx = (max(xs) + min(xs)) / 2, cy = (max(ys) + min(ys)) / 2)
    [for (q = p) [(q.x - cx) * k, (q.y - cy) * k]];

heart_raw = [for (i = [0:95]) let(t = i * 360 / 96)
    [16 * pow(sin(t), 3), 13 * cos(t) - 5 * cos(2 * t) - 2 * cos(3 * t) - cos(4 * t)]];

// Five rounded points: radius swings between 0.55 and 1 of the tip radius.
star_raw = [for (i = [0:179]) let(t = i * 2, r = 0.55 + 0.45 * pow((1 + cos(5 * t)) / 2, 1.6))
    [r * cos(t + 90), r * sin(t + 90)]];

circle_raw = [for (i = [0:95]) let(t = i * 360 / 96) [cos(t), sin(t)]];

// A rounded rectangle 1 x 0.78 with 0.12 corners.
rrect_raw = let(w = 0.5, h = 0.39, r = 0.12)
    [for (k = [0:3]) let(cx = (k == 0 || k == 3) ? w - r : -(w - r), cy = k < 2 ? h - r : -(h - r))
        for (j = [0:8]) let(a = 90 * k + j * 90 / 8) [cx + r * cos(a), cy + r * sin(a)]];

// A long-necked dinosaur facing right, rounded off by Chaikin corner cutting.
dino_raw = chaikin_n([
    [1, 47], [-1, 44], [1, 41], [8, 40], [16, 38], [24, 43], [32, 48], [42, 51], [52, 51], [61, 48], [67, 45],
    [72, 51], [76, 59], [80, 65], [85, 69], [92, 69], [98, 65], [99, 60], [95, 57], [87, 57],
    [83, 52], [80, 44], [78, 36], [77, 27], [77, 14], [77, 0], [66, 0], [65, 12], [58, 14],
    [48, 14], [40, 14], [35, 12], [34, 0], [23, 0], [23, 12], [19, 19], [14, 26], [8, 33], [3, 40]
], 3);

OUTLINE = fit(
    shape == "heart" ? chaikin_n(heart_raw, 2)
  : shape == "star" ? star_raw
  : shape == "dinosaur" ? dino_raw
  : shape == "circle" ? circle_raw
  : rrect_raw);

// The x the rows are centred on when they are fitted (the dinosaur's body is
// in the middle of its outline too).
CX = 0;

oxs = [for (q = OUTLINE) q.x];
oys = [for (q = OUTLINE) q.y];
Y_MIN = min(oys);
Y_MAX = max(oys);

// ---------------------------------------------------------------- fitting

// Where a horizontal line at y crosses the outline.
function crossings(y) = [for (i = [0:len(OUTLINE) - 1])
    let(a = OUTLINE[i], b = OUTLINE[(i + 1) % len(OUTLINE)])
    if ((a.y <= y && b.y > y) || (b.y <= y && a.y > y))
        a.x + (y - a.y) * (b.x - a.x) / (b.y - a.y)];

// The stretch of that line inside the outline around CX, or [] if CX is
// outside the outline at that height.
function interval(y) = let(
    xs = crossings(y),
    l = [for (x = xs) if (x < CX) x], r = [for (x = xs) if (x >= CX) x])
    len(l) % 2 == 1 && len(r) > 0 ? [max(l), min(r)] : [];

// The stretch at every height where the answer can change (every vertex, a
// hair either side) and on a 1 mm grid, as [y, left, right] (or [y] outside).
SAMPLES = [for (y = concat([for (v = OUTLINE) each [v.y - 1e-4, v.y + 1e-4]],
                           [for (yy = [floor(Y_MIN):1:ceil(Y_MAX)]) yy]))
           concat([y], interval(y))];

// The stretch a whole band y0..y1 has inside the outline: the tightest over
// the band. Between two vertex heights the nearest edge on each side is the
// same straight edge, so the tightest is at a vertex height or at the band's
// ends, which are sampled here exactly.
function band(y0, y1) = let(
    s = concat([concat([y0], interval(y0)), concat([y1], interval(y1))],
               [for (q = SAMPLES) if (q[0] > y0 && q[0] < y1) q]),
    bad = [for (q = s) if (len(q) < 3) 1])
    len(bad) > 0 ? [] : [max([for (q = s) q[1]]), min([for (q = s) q[2]])];

// Track length for n beads.
function track_len(n, f = FREE) = 2 * end_gap + BODY + (n - 1) * PITCH + f;
// How many beads fit a band stretch (0 if not even one does).
function beads_in(iv, f = FREE) = len(iv) < 2 ? 0 : let(room = iv[1] - iv[0] - 2 * wall)
    room < track_len(1, f) ? 0 : min(beads_per_row, 1 + floor((room - track_len(1, f)) / PITCH));

// Name: its ink box, from per-glyph metrics of DejaVu Sans Bold measured at
// size 10 with textmetrics() offline (ASCII 32..126), as [advance, ink x min,
// ink x max, ink y min, ink y max], rounded outward. textmetrics() itself is
// not used because MakerWorld's OpenSCAD does not have it. Anything outside
// ASCII counts as the widest, tallest and deepest glyph the face has up to
// U+017F. Kerning only ever pulls letters together, so the box is never short.
GLYPH10 = [
    [4.835, 0.0, 0.0, 0.0, 0.0], [6.334, 1.94, 4.4, 0.0, 10.13], [7.236, 1.31, 5.92, 6.36, 10.13],
    [11.637, 0.94, 10.7, 0.0, 9.98], [9.664, 1.08, 8.73, -2.05, 10.56],
    [13.916, 0.44, 13.48, -0.2, 10.32], [12.112, 0.83, 11.54, -0.2, 10.32],
    [4.252, 1.31, 2.94, 6.36, 10.13], [6.348, 1.19, 5.24, -1.84, 10.55],
    [6.348, 1.1, 5.16, -1.84, 10.55], [7.263, 0.27, 6.99, 3.85, 10.32],
    [11.637, 1.46, 10.17, 0.0, 8.71], [5.276, 0.73, 3.86, -1.98, 2.63],
    [5.764, 0.74, 5.02, 3.01, 4.99], [5.276, 1.41, 3.86, 0.0, 2.63],
    [5.073, 0.0, 5.08, -1.29, 10.13], [9.664, 0.65, 9.01, -0.2, 10.32],
    [9.664, 1.56, 8.72, 0.0, 10.13], [9.664, 1.09, 8.47, 0.0, 10.32],
    [9.664, 0.92, 8.57, -0.2, 10.32], [9.664, 0.62, 9.04, 0.0, 10.13],
    [9.664, 1.06, 8.7, -0.2, 10.13], [9.664, 0.85, 8.93, -0.2, 10.3],
    [9.664, 0.92, 8.57, 0.0, 10.13], [9.664, 0.84, 8.81, -0.2, 10.32],
    [9.664, 0.71, 8.79, -0.2, 10.3], [5.554, 1.54, 4.01, 0.0, 7.6],
    [5.554, 0.87, 4.01, -1.98, 7.6], [11.637, 1.46, 10.17, 0.41, 8.3],
    [11.637, 1.46, 10.17, 2.0, 6.7], [11.637, 1.46, 10.17, 0.41, 8.3],
    [8.057, 0.95, 7.16, 0.0, 10.32], [13.889, 0.91, 12.91, -2.42, 9.77],
    [10.749, 0.06, 10.69, 0.0, 10.13], [10.586, 1.27, 9.62, 0.0, 10.13],
    [10.193, 0.69, 9.31, -0.2, 10.32], [11.529, 1.27, 10.81, 0.0, 10.13],
    [9.488, 1.27, 8.48, 0.0, 10.13], [9.488, 1.27, 8.33, 0.0, 10.13],
    [11.4, 0.69, 10.39, -0.2, 10.32], [11.624, 1.27, 10.35, 0.0, 10.13],
    [5.168, 1.27, 3.9, 0.0, 10.13], [5.168, -0.79, 3.9, -2.78, 10.13],
    [10.762, 1.27, 11.19, 0.0, 10.13], [8.85, 1.27, 8.48, 0.0, 10.13],
    [13.821, 1.27, 12.55, 0.0, 10.13], [11.624, 1.27, 10.35, 0.0, 10.13],
    [11.807, 0.69, 11.12, -0.2, 10.32], [10.179, 1.27, 9.62, 0.0, 10.13],
    [11.807, 0.69, 11.12, -2.03, 10.32], [10.695, 1.27, 10.42, 0.0, 10.13],
    [10.003, 0.99, 8.99, -0.2, 10.32], [9.474, 0.06, 9.41, 0.0, 10.13],
    [11.278, 1.27, 10.01, -0.2, 10.13], [10.749, 0.06, 10.69, 0.0, 10.13],
    [15.32, 0.4, 14.89, 0.0, 10.13], [10.708, 0.26, 10.44, 0.0, 10.13],
    [10.057, -0.15, 10.2, 0.0, 10.13], [10.071, 0.62, 9.45, 0.0, 10.13],
    [6.348, 1.19, 5.41, -1.84, 10.56], [5.073, 0.0, 5.08, -1.29, 10.13],
    [6.348, 0.94, 5.16, -1.84, 10.56], [11.637, 1.4, 10.24, 6.34, 10.13],
    [6.944, 0.0, 6.95, -3.28, -1.99], [6.944, 0.63, 4.48, 8.56, 11.12],
    [9.372, 0.59, 8.29, -0.2, 7.78], [9.942, 1.16, 9.32, -0.2, 10.56],
    [8.233, 0.59, 7.31, -0.2, 7.78], [9.942, 0.62, 8.79, -0.2, 10.56],
    [9.42, 0.59, 8.75, -0.2, 7.78], [6.043, 0.26, 6.17, 0.0, 10.56],
    [9.942, 0.62, 8.79, -3.0, 7.77], [9.888, 1.16, 8.81, 0.0, 10.56],
    [4.761, 1.16, 3.6, 0.0, 10.56], [4.761, -0.47, 3.6, -3.0, 10.56],
    [9.237, 1.16, 9.51, 0.0, 10.56], [4.761, 1.16, 3.6, 0.0, 10.56],
    [14.472, 1.15, 13.38, 0.0, 7.78], [9.888, 1.16, 8.81, 0.0, 7.78],
    [9.542, 0.59, 8.95, -0.2, 7.78], [9.942, 1.16, 9.32, -2.89, 7.78],
    [9.942, 0.62, 8.79, -2.89, 7.77], [6.849, 1.16, 6.81, 0.0, 7.78],
    [8.267, 0.71, 7.61, -0.2, 7.78], [6.639, 0.17, 6.33, 0.0, 9.76],
    [9.888, 1.08, 8.73, -0.2, 7.6], [9.054, 0.2, 8.86, 0.0, 7.6], [12.831, 0.48, 12.36, 0.0, 7.6],
    [8.959, 0.2, 8.75, 0.0, 7.6], [9.054, 0.16, 8.81, -3.0, 7.6], [8.084, 0.62, 7.43, 0.0, 7.6],
    [9.888, 1.73, 8.16, -2.27, 10.56], [5.073, 1.76, 3.31, -3.28, 10.62],
    [9.888, 1.73, 8.16, -2.27, 10.56], [11.637, 1.46, 10.17, 2.93, 5.77]
];
GLYPH10_OTHER = [16.21, -0.79, 18.37, -3.28, 12.98];
function glyph10(ch) = let(o = ord(ch)) o >= 32 && o <= 126 ? GLYPH10[o - 32] : GLYPH10_OTHER;
function pen10(s, i) = i <= 0 ? 0 : pen10(s, i - 1) + glyph10(s[i - 1])[0];
// [x min, x max, y min, y max] of the name's ink at size 10, the first letter's
// pen at x = 0 and the baseline at y = 0 ([0, 0, 0, 0] when nothing is inked).
function ink10(s) = let(
    gs = [for (i = [0:len(s) - 1]) let(g = glyph10(s[i])) if (g[2] > g[1]) [pen10(s, i) + g[1], pen10(s, i) + g[2], g[3], g[4]]])
    len(gs) == 0 ? [0, 0, 0, 0]
    : [min([for (q = gs) q[0]]), max([for (q = gs) q[1]]), min([for (q = gs) q[2]]), max([for (q = gs) q[3]])];
HAS_NAME = len(name) > 0;
NAME_INK = HAS_NAME ? ink10(name) : [0, 0, 0, 0];
NAME_W1 = (NAME_INK[1] - NAME_INK[0]) / 10;  // ink width at size 1
NAME_H1 = (NAME_INK[3] - NAME_INK[2]) / 10;  // ink height at size 1
// The faces the table measures (DejaVu Sans Book is narrower than the Bold).
MEASURED_FONT = font == "DejaVu Sans:style=Bold" || font == "DejaVu Sans" || font == "DejaVu Sans:style=Book";
// The name's band holds its ink at the asked size (taller than name_size when
// it has capitals with accents, or letters that hang below the line), plus the
// wall above and below.
NAME_BAND = NAME_H1 * name_size + 2 * wall;
MIN_NAME = 6;
function name_fit(iv) = len(iv) < 2 || NAME_W1 <= 0 ? 0 : min(name_size, (iv[1] - iv[0] - 2 * wall) / NAME_W1);

// Row r (0 = top) of a stack whose top is at y_top.
function row_y(y_top, r) = y_top - ROW_HALF - r * ROW_P;
function row_iv(y_top, r) = let(y = row_y(y_top, r)) band(y - ROW_HALF, y + ROW_HALF);
function stack_h() = 2 * ROW_HALF + (rows - 1) * ROW_P;
function name_y(y_top) = y_top - stack_h() - NAME_BAND / 2 + wall / 2;
function name_iv(y_top) = let(y = name_y(y_top)) band(y - NAME_BAND / 2, y + NAME_BAND / 2);

// Score a stack position: every bead counts, the name fitting counts for more
// than any number of beads, and nearer the middle breaks ties.
function score(y_top, f) = let(
    nb = [for (r = [0:rows - 1]) beads_in(row_iv(y_top, r), f)] * [for (r = [0:rows - 1]) 1],
    nm = HAS_NAME && name_fit(name_iv(y_top)) >= MIN_NAME ? 1 : 0,
    mid = y_top - (stack_h() + (HAS_NAME ? NAME_BAND : 0)) / 2)
    nm * 100000 + nb * 100 - abs(mid - (Y_MIN + Y_MAX) / 2) / 1000;

CANDS = [for (y = [Y_MAX:-search_step:Y_MIN]) y];
function best_for(f) = let(sc = [for (y = CANDS) score(y, f)]) CANDS[search(max(sc), sc)[0]];
function beads_at(y_top, f) = [for (r = [0:rows - 1]) beads_in(row_iv(y_top, r), f)] * [for (r = [0:rows - 1]) 1];
// The free room asked for, or if not even one bead fits with it, the most
// (in half places, down to half a place) that lets one.
function pick(fp) = let(b = best_for(fp * PITCH))
    beads_at(b, fp * PITCH) > 0 || fp <= 0.5 ? [fp, b] : pick(fp - 0.5);
PICK = pick(free_places);
FREE_PLACES = PICK[0];
FREE = FREE_PLACES * PITCH; // free room in each row
BEST = PICK[1];

ROW_IV = [for (r = [0:rows - 1]) row_iv(BEST, r)];
ROW_N = [for (r = [0:rows - 1]) beads_in(ROW_IV[r])];
NAME_IV = HAS_NAME ? name_iv(BEST) : [];
NAME_S = HAS_NAME ? name_fit(NAME_IV) : 0;
NAME_OK = HAS_NAME && NAME_S >= MIN_NAME;

// Rows that hold beads, top to bottom, renumbered from 0 for the colours.
LIVE = [for (r = [0:rows - 1]) if (ROW_N[r] > 0) r];
N_BEADS = len(LIVE) == 0 ? 0 : [for (r = LIVE) ROW_N[r]] * [for (r = LIVE) 1];

// Track r: x of its left end, and bead centre x.
function track_x0(r) = (ROW_IV[r][0] + ROW_IV[r][1]) / 2 - track_len(ROW_N[r]) / 2;
function bead_x(r, i) = track_x0(r) + end_gap + BODY / 2 + i * PITCH;

// ---------------------------------------------------------------- colours

bead_palette = [bead_color_1, bead_color_2, bead_color_3, bead_color_4,
                bead_color_5, bead_color_6, bead_color_7, bead_color_8];
function bead_col(k, i) = bead_palette[
    (color_pattern == "rows" ? k : color_pattern == "columns" ? i : k + i) % bead_colors];

// ---------------------------------------------------------------- geometry

// A bead's cross-section, grown by gr (verify.sh's probes), up to z_top.
module bead_profile(gr, z_top) {
    offset(r = gr) polygon([[-S0 + foot, 0], [S0 - foot, 0], [S0 - foot, foot], [S0, foot], [S0, Z1],
                            [S1, Z2], [S1, Z3], [S0, Z4], [S0, z_top], [-S0, z_top], [-S0, Z4],
                            [-S1, Z3], [-S1, Z2], [-S0, Z1], [-S0, foot], [-S0 + foot, foot]]);
}

// A bead centred on x = 0, y = 0, grown by gr, and swept sweep mm along +x
// (verify.sh's probes; the knob is convex, so a hull sweeps it exactly).
module bead(gr = 0, sweep = 0) {
    zt = T + g;
    // Body along x (its profile has the bottom 0.3 mm set in).
    rotate([90, 0, 90]) translate([0, 0, -BODY / 2 - gr]) linear_extrude(BODY + 2 * gr + sweep)
        bead_profile(gr, zt + 0.01);
    // Knob: a 45-degree cone from the stem out to the knob's rim, a short
    // band, and a flattened dome.
    hull() for (dx = [0, sweep]) translate([dx, 0, 0]) {
        translate([0, 0, zt - gr]) linear_extrude(0.01) square([BODY + 2 * gr, 2 * (S0 + gr)], center = true);
        translate([0, 0, zt + KNOB_UP]) cylinder(d = D + 2 * gr, h = 1.5 + gr);
        translate([0, 0, zt + KNOB_UP + 1.5]) scale([1, 1, 0.35]) sphere(d = D + 2 * gr);
    }
}

// The track a row of n beads slides in, left end at x = 0.
module track(n) {
    L = track_len(n);
    rotate([90, 0, 90]) translate([0, 0, 0]) linear_extrude(L) {
        bead_profile(c, T + 1);
        translate([-(S0 + c + foot), -1]) square([2 * (S0 + c + foot), 1 + foot]);
    }
}

module outline_2d() { polygon(OUTLINE); }

module board_blank() {
    linear_extrude(foot) offset(delta = -foot) outline_2d();
    top = T - edge_round;
    translate([0, 0, foot]) linear_extrude(top - foot) outline_2d();
    for (i = [1:round_steps]) {
        z0 = top + edge_round * (i - 1) / round_steps;
        in = edge_round - sqrt(max(0, edge_round * edge_round - pow(edge_round * i / round_steps, 2)));
        translate([0, 0, z0]) linear_extrude(edge_round / round_steps) offset(r = -in) outline_2d();
    }
}

// The name, centred on its band by its measured ink box. The box it is
// clipped to is the band less the wall, which the ink of a measured face never
// reaches (verify.sh checks the inlay is the whole of the text); it only keeps
// a wider face off the board's edge, and the render log says when that can be.
module name_2d() {
    if (NAME_OK)
        translate([(NAME_IV[0] + NAME_IV[1]) / 2, name_y(BEST)])
            intersection() {
                scale(NAME_S / 10) translate([-(NAME_INK[0] + NAME_INK[1]) / 2, -(NAME_INK[2] + NAME_INK[3]) / 2])
                    text(name, size = 10, font = font, halign = "left", valign = "baseline");
                square([NAME_IV[1] - NAME_IV[0] - 2 * wall, NAME_BAND - 2 * wall], center = true);
            }
}

module board() {
    difference() {
        board_blank();
        for (r = LIVE) translate([track_x0(r), row_y(BEST, r), 0]) track(ROW_N[r]);
        translate([0, 0, T - inlay]) linear_extrude(inlay + 1) name_2d();
    }
}

module name_part() {
    translate([0, 0, T - inlay]) linear_extrude(inlay) name_2d();
}

module each_bead() {
    for (k = [0:len(LIVE) - 1]) let(r = LIVE[k]) for (i = [0:ROW_N[r] - 1])
        translate([bead_x(r, i), row_y(BEST, r), 0]) children();
}

// ---------------------------------------------------------------- report

for (r = [0:rows - 1]) if (ROW_N[r] == 0)
    echo(str("NOTE: row ", r + 1, " of ", rows, " does not fit the ", shape, " at this size and is left out; raise board_size or lower bead_size"));
for (r = [0:rows - 1]) if (ROW_N[r] > 0 && ROW_N[r] < beads_per_row)
    echo(str("NOTE: row ", r + 1, " holds ", ROW_N[r], " of the ", beads_per_row, " beads asked for; the ", shape, " is narrower there"));
if (FREE_PLACES < free_places)
    echo(str("NOTE: free room cut to ", FREE_PLACES, " bead places (", free_places, " asked) so a row of beads fits the ", shape));
if (HAS_NAME && !NAME_OK)
    echo(str("NOTE: the name does not fit the ", shape, " below the rows and is left out; shorten it, take a row away or raise board_size"));
else if (HAS_NAME && NAME_S < name_size)
    echo(str("NOTE: name letters are ", round(NAME_S * 10) / 10, " mm (", name_size, " asked) so the name fits"));
if (NAME_OK && !MEASURED_FONT)
    echo(str("NOTE: the name is fitted with DejaVu Sans Bold's letter widths; in ", font,
             " it may be cut off at the ends if that face is wider"));

assert(N_BEADS > 0, str("no row of beads fits a ", board_size, " mm ", shape, "; raise board_size"));
assert(max(oxs) - min(oxs) <= bed_w && Y_MAX - Y_MIN <= bed_d, "board does not fit the plate");

echo(str("SB_FIDGET beads=", N_BEADS, " rows=[", [for (r = LIVE) ROW_N[r]], "] row_y=[",
         [for (r = LIVE) row_y(BEST, r)], "] x0=[", [for (r = LIVE) track_x0(r)], "] pitch=", PITCH, " free_places=", FREE_PLACES,
         " free=", FREE, " name_size=", NAME_S, " name_box=[", NAME_W1 * NAME_S, ", ", NAME_H1 * NAME_S, "] name_iv=", NAME_IV, " name_y=", HAS_NAME ? name_y(BEST) : 0,
         " size=[", max(oxs) - min(oxs), ", ", Y_MAX - Y_MIN, "] top=", T + g + KNOB_UP + 1.5 + 0.35 * D / 2));

// ---------------------------------------------------------------- output

// Beads print at their places in the tracks, board centred on the origin.
if (probe_gap > 0 && !probe_slide) {
    for (k = [0:len(LIVE) - 1]) let(r = LIVE[k]) for (i = [0:ROW_N[r] - 1])
        translate([bead_x(r, i), row_y(BEST, r), 0]) {
            intersection() { translate([-bead_x(r, i), -row_y(BEST, r), 0]) board(); bead(probe_gap); }
            if (i < ROW_N[r] - 1) intersection() { translate([PITCH, 0, 0]) bead(); bead(probe_gap); }
        }
    translate([-1000, 0, 0]) cube(1);
} else if (probe_move != [0, 0, 0]) {
    intersection() {
        board();
        each_bead() translate(probe_move) bead();
    }
    translate([-1000, 0, 0]) cube(1);
} else if (probe_slide) {
    intersection() {
        board();
        each_bead() bead(probe_gap, FREE);
    }
    translate([-1000, 0, 0]) cube(1);
} else if (probe_wall > 0) {
    linear_extrude(1) difference() {
        union() {
            offset(delta = probe_wall) union() {
                for (r = LIVE) translate([track_x0(r), row_y(BEST, r) - (S1 + c)])
                    square([track_len(ROW_N[r]), 2 * (S1 + c)]);
                for (r = LIVE) for (i = [0:ROW_N[r] - 1]) translate([bead_x(r, i), row_y(BEST, r)]) circle(d = D);
            }
            // The name grown by a true distance: a sharp (delta) offset mitres
            // an acute glyph corner, such as the foot of an A, far past it.
            offset(r = probe_wall) name_2d();
        }
        outline_2d();
    }
    translate([-1000, 0, 0]) cube(1);
} else {
    color(board_color) board();
    for (k = [0:len(LIVE) - 1]) let(r = LIVE[k]) for (i = [0:ROW_N[r] - 1])
        color(bead_col(k, i)) translate([bead_x(r, i), row_y(BEST, r), 0]) bead();
    color(name_color) name_part();
}
