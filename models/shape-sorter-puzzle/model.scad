// Shape sorter puzzle — a toddler's peg puzzle: a tray with a hole for every
// piece, and chunky pieces with a knob on top to lift them by. Pick a set:
// basic shapes, animals built from simple round shapes, the numbers 0-9, or
// the letters of a word. Every piece has its own colour, and the floor of
// each hole carries a flush inlay of its piece's shape in the piece's colour,
// so matching colours helps with matching shapes.
//
// Each piece is its hole's outline shrunk by `clearance` all round, so it
// drops in without forcing; the top of every hole has a lead-in chamfer. The
// pieces stand a little proud of the tray so they can be picked out without
// the knobs too. Letter and number pieces are the glyph thickened and with
// small gaps filled in, so they stay chunky; their knobs sit at the point of
// the piece furthest from any edge (a table measured from the font, below).
//
// The tray and the pieces lay out side by side on one 300 x 320 plate. If
// they do not fit together, the tray is printed alone and the render log says
// (NOTE:) to print the pieces with layout = pieces.
//
// Prints flat, as it lies, with no supports.
//
// Written to the MakerWorld Parametric Model Maker customizer conventions so
// the same file works unchanged on MakerWorld and in ScadBuddy.
//
// The colour parameters are the extruder order: tray_color is extruder 1,
// piece_color_1 .. piece_color_12 are extruders 2-13. Piece n takes piece
// colour n (the thirteenth letter starts again at piece colour 1). Colours
// that are equal merge into one part and one filament.

/* [Puzzle] */

// Which pieces
set = "shapes"; // [shapes:Shapes (6), animals:Animals (6), numbers:Numbers 0-9, letters:Letters of a word]

// The word for the letters set (A-Z and 0-9; lower case is made upper case, anything else is skipped)
letters = "ANNA"; // 12

// Size of each piece in mm (smaller if the tray would not fit the plate)
piece_size = 55; // [35:5:80]

// Which parts to print (both, or the tray and the pieces on separate plates)
layout = "both"; // [both:Tray and pieces, tray:Tray only, pieces:Pieces only]

/* [Pieces] */

// Knob on every piece to lift it by
knobs = true;

// Knob diameter in mm (narrower on a piece that has no room for it)
knob_diameter = 12; // [8:1:16]

// Knob height in mm above the piece
knob_height = 10; // [6:1:16]

// Piece thickness in mm (at least the hole depth)
piece_thickness = 8; // [4:0.5:14]

// Gap per side between each piece and its hole, in mm
clearance = 0.5; // [0.3:0.05:1]

/* [Tray] */

// Depth of each hole in mm
hole_depth = 5; // [3:0.5:10]

// Tray floor thickness under the holes in mm
floor_thickness = 2.4; // [2:0.2:4]

// Tray between neighbouring holes and round the edge, in mm
tray_wall = 7; // [5:1:14]

// Inlay each hole's floor with its piece's shape in the piece's colour
color_hints = true;

/* [Colours] */

// Tray colour (extruder 1)
tray_color = "#FFF3E0"; // color

// Piece 1 colour (extruder 2)
piece_color_1 = "#E53935"; // color

// Piece 2 colour (extruder 3)
piece_color_2 = "#1E88E5"; // color

// Piece 3 colour (extruder 4)
piece_color_3 = "#FDD835"; // color

// Piece 4 colour (extruder 5)
piece_color_4 = "#43A047"; // color

// Piece 5 colour (extruder 6)
piece_color_5 = "#FB8C00"; // color

// Piece 6 colour (extruder 7)
piece_color_6 = "#8E24AA"; // color

// Piece 7 colour (extruder 8)
piece_color_7 = "#EC407A"; // color

// Piece 8 colour (extruder 9)
piece_color_8 = "#00ACC1"; // color

// Piece 9 colour (extruder 10)
piece_color_9 = "#7CB342"; // color

// Piece 10 colour (extruder 11)
piece_color_10 = "#5E35B1"; // color

// Piece 11 colour (extruder 12)
piece_color_11 = "#6D4C41"; // color

// Piece 12 colour (extruder 13)
piece_color_12 = "#F4511E"; // color

/* [Hidden] */

$fn = 64;

// Letters and numbers use this face: the knob table below is measured on it.
font = "DejaVu Sans:style=Bold";
// Plate.
bed_w = 300;
bed_d = 320;
// Gap between pieces on the plate, and between the tray and the pieces.
piece_gap = 5;
// Bottom-edge set-in against elephant's foot.
foot = 0.3;
// Top-edge rounding of the tray and of the pieces.
tray_round = 1.5;
piece_round = 0.8;
round_steps = 3;
// Lead-in chamfer at the top of each hole.
lead_in = 0.8;
// Colour hint: inset from the hole's edge and depth.
hint_inset = 1.5;
hint_depth = 0.6;
// Knob: room left between it and the piece's edge; smallest knob made.
knob_margin = 1.2;
knob_min = 6;
// Glyph pieces: thickening and gap filling, as fractions of the piece size.
glyph_fat = 0.05;
glyph_fill = 0.02;

// Test hooks for verify.sh. probe_fit > 0 renders each piece, grown by
// probe_fit, sitting in its hole, intersected with the tray. probe_cell
// renders any part of a hole outside its cell. probe_knob renders any part of
// a knob's footprint, grown by knob_margin - 0.1, off its piece. export_hole
// >= 0 renders hole k alone, as 2D, centred on its cell (for measuring the
// knob table).
probe_fit = 0;
probe_cell = false;
probe_knob = false;
export_hole = -1;

// ---------------------------------------------------------------- pieces

function up(ch) = let(o = ord(ch)) o >= 97 && o <= 122 ? chr(o - 32) : ch;
function keep(ch) = let(o = ord(ch)) (o >= 65 && o <= 90) || (o >= 48 && o <= 57);
WORD = [for (ch = letters) if (keep(up(ch))) up(ch)];

SHAPES = ["circle", "square", "triangle", "star", "heart", "hexagon"];
ANIMALS = ["cat", "fish", "bunny", "turtle", "bird", "whale"];
DIGITS = ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9"];
PIECES = set == "shapes" ? SHAPES : set == "animals" ? ANIMALS : set == "numbers" ? DIGITS
       : len(WORD) > 0 ? WORD : ["A"];
N = len(PIECES);
is_glyph = set == "numbers" || set == "letters";

// Piece thickness is never less than the hole is deep.
PT = max(piece_thickness, hole_depth);
TRAY_H = floor_thickness + hole_depth;

// ---------------------------------------------------------------- layout

// Tray grid for n pieces at size s: as many columns as the plate is wide,
// rows balanced.
function t_cols_max(s) = max(1, floor((bed_w - tray_wall) / (s + tray_wall)));
function t_rows(s) = ceil(N / t_cols_max(s));
function t_cols(s) = ceil(N / t_rows(s));
function tray_w(s) = t_cols(s) * (s + tray_wall) + tray_wall;
function tray_d(s) = t_rows(s) * (s + tray_wall) + tray_wall;
// Largest size up to piece_size (in 1 mm steps) whose tray fits the plate.
function fit_size(s) = tray_d(s) <= bed_d && tray_w(s) <= bed_w || s <= 20 ? s : fit_size(s - 1);
S = fit_size(piece_size);

TC = t_cols(S);
TR = t_rows(S);
TW = tray_w(S);
TD = tray_d(S);

// Pieces grid, S apart plus the gap, as wide as the plate allows.
P_PITCH = S + piece_gap;
PC = min(N, max(1, floor((bed_w + piece_gap) / P_PITCH)));
PR = ceil(N / PC);
PW = PC * P_PITCH - piece_gap;
PD = PR * P_PITCH - piece_gap;

BOTH_FIT = TD + piece_gap + PD <= bed_d && max(TW, PW) <= bed_w;
SHOW_TRAY = layout != "pieces";
SHOW_PIECES = layout == "pieces" || (layout == "both" && BOTH_FIT);

// Centre of hole k, tray corner at the origin.
function hole_c(k) = [tray_wall + (k % TC) * (S + tray_wall) + S / 2,
                      TD - tray_wall - floor(k / TC) * (S + tray_wall) - S / 2];
// Centre of piece k on the plate: above the tray when both print.
PIECES_Y0 = SHOW_TRAY ? TD + piece_gap : 0;
function piece_c(k) = [(k % PC) * P_PITCH + S / 2,
                       PIECES_Y0 + PD - floor(k / PC) * P_PITCH - S / 2];

// ---------------------------------------------------------------- outlines

function chaikin(p) = let(n = len(p))
    [for (i = [0:n - 1]) let(a = p[i], b = p[(i + 1) % n]) each [0.75 * a + 0.25 * b, 0.25 * a + 0.75 * b]];

// Round convex corners by r (opening) and fill concave ones by r2 (closing).
module soften(r, r2 = 0) {
    offset(r = -r2) offset(r = r2 + r) offset(r = -r) children();
}

// Unit outlines, inside [-0.5, 0.5] square; scaled by S.
heart_pts = let(raw = [for (i = [0:95]) let(t = i * 360 / 96)
        [16 * pow(sin(t), 3), 13 * cos(t) - 5 * cos(2 * t) - 2 * cos(3 * t) - cos(4 * t)]],
    sm = chaikin(chaikin(raw)))
    [for (q = sm) [q.x / 34.5, (q.y + 2.25) / 34.5]];
star_pts = [for (i = [0:179]) let(t = i * 2, r = 0.25 + 0.25 * pow((1 + cos(5 * t)) / 2, 1.3))
    [r * cos(t + 90), r * sin(t + 90) - 0.05]];

module ellipse(rx, ry) { scale([rx, ry]) circle(r = 1); }

module shape_unit(name) {
    if (name == "circle") circle(d = 1);
    else if (name == "square") offset(r = 0.08) square(0.84 - 0.16, center = true);
    else if (name == "triangle") soften(0.07) translate([0, -0.1425]) rotate(90) circle(d = 1.14, $fn = 3);
    else if (name == "star") polygon(star_pts);
    else if (name == "heart") polygon(heart_pts);
    else if (name == "hexagon") soften(0.05) circle(d = 1, $fn = 6);
    else if (name == "cat") soften(0.04, 0.05) {
        translate([0, -0.1]) ellipse(0.36, 0.32);
        for (s = [-1, 1]) hull() {
            translate([s * 0.3, 0.02]) circle(r = 0.06);
            translate([s * 0.08, 0.1]) circle(r = 0.06);
            translate([s * 0.34, 0.39]) circle(r = 0.05);
        }
    }
    else if (name == "fish") soften(0.03, 0.05) {
        translate([0.1, 0]) ellipse(0.36, 0.26);
        hull() {
            translate([-0.18, 0]) circle(r = 0.05);
            translate([-0.44, 0.2]) circle(r = 0.05);
            translate([-0.44, -0.2]) circle(r = 0.05);
        }
    }
    else if (name == "bunny") soften(0.03, 0.06) {
        translate([0, -0.22]) circle(r = 0.27);
        for (s = [-1, 1]) translate([s * 0.12, 0.2]) rotate(s * -12) ellipse(0.1, 0.25);
    }
    else if (name == "turtle") soften(0.03, 0.05) {
        ellipse(0.3, 0.23);
        translate([0.37, 0.03]) circle(r = 0.11);
        for (sx = [-1, 1], sy = [-1, 1]) translate([sx * 0.2, sy * 0.21]) circle(r = 0.09);
        hull() { translate([-0.28, 0]) circle(r = 0.05); translate([-0.42, -0.03]) circle(r = 0.035); }
    }
    else if (name == "bird") soften(0.03, 0.05) {
        translate([-0.06, -0.1]) ellipse(0.32, 0.26);
        translate([0.2, 0.16]) circle(r = 0.17);
        hull() { translate([0.3, 0.2]) circle(r = 0.06); translate([0.46, 0.16]) circle(r = 0.035); }
        hull() { translate([-0.3, -0.05]) circle(r = 0.07); translate([-0.46, 0.12]) circle(r = 0.04); }
    }
    else if (name == "whale") soften(0.03, 0.06) {
        translate([0.06, -0.08]) ellipse(0.42, 0.25);
        hull() { translate([-0.26, -0.02]) circle(r = 0.06); translate([-0.3, 0.1]) circle(r = 0.06); }
        for (s = [-1, 1]) translate([-0.3, 0.12]) rotate(s * 40) translate([0, 0.1]) ellipse(0.055, 0.12);
    }
}

// Glyph ink box at size 1 in DejaVu Sans Bold, as [char, width, height,
// centre x, centre y] (text() with valign = "center" already centres the ink
// vertically, so centre y is 0). Measured from SVG exports of every character at size 100.
GLYPHS = [
    ["A", 1.061, 1.012, 0.000, 0.000], ["B", 0.833, 1.012, 0.015, 0.000],
    ["C", 0.861, 1.051, -0.010, 0.000], ["D", 0.953, 1.012, 0.027, 0.000],
    ["E", 0.719, 1.012, 0.013, 0.000], ["F", 0.705, 1.012, 0.005, 0.000],
    ["G", 0.968, 1.051, -0.017, 0.000], ["H", 0.907, 1.012, 0.000, 0.000],
    ["I", 0.261, 1.012, 0.000, 0.000], ["J", 0.467, 1.290, -0.103, 0.000],
    ["K", 0.991, 1.012, 0.085, 0.000], ["L", 0.719, 1.012, 0.045, 0.000],
    ["M", 1.126, 1.012, 0.000, 0.000], ["N", 0.907, 1.012, 0.000, 0.000],
    ["O", 1.042, 1.051, 0.000, 0.000], ["P", 0.833, 1.012, 0.035, 0.000],
    ["Q", 1.042, 1.234, 0.000, 0.000], ["R", 0.914, 1.012, 0.050, 0.000],
    ["S", 0.799, 1.051, -0.001, 0.000], ["T", 0.933, 1.012, 0.000, 0.000],
    ["U", 0.873, 1.032, 0.000, 0.000], ["V", 1.061, 1.012, 0.000, 0.000],
    ["W", 1.447, 1.012, -0.001, 0.000], ["X", 1.017, 1.012, -0.001, 0.000],
    ["Y", 1.033, 1.012, 0.000, 0.000], ["Z", 0.882, 1.012, 0.000, 0.000],
    ["0", 0.833, 1.051, 0.000, 0.000], ["1", 0.714, 1.012, 0.030, 0.000],
    ["2", 0.736, 1.031, -0.005, 0.000], ["3", 0.763, 1.051, -0.009, 0.000],
    ["4", 0.840, 1.012, -0.001, 0.000], ["5", 0.762, 1.032, 0.005, 0.000],
    ["6", 0.806, 1.049, 0.006, 0.000], ["7", 0.763, 1.012, -0.009, 0.000],
    ["8", 0.795, 1.051, -0.001, 0.000], ["9", 0.806, 1.049, -0.009, 0.000]
];
function glyph(ch) = let(i = search([ch], GLYPHS, 1, 0)[0]) i == [] || i == undef ? ["?", 1, 1.05, 0] : GLYPHS[i];
// The ink fills the piece less the thickening on each side, with 3 % to
// spare for the font's curves between the measured points.
function glyph_size(ch) = let(g = glyph(ch)) 0.97 * S * (1 - 2 * glyph_fat) / max(g[1], g[2]);

module glyph_hole(ch) {
    s = glyph_size(ch);
    offset(r = -glyph_fill * S) offset(r = (glyph_fat + glyph_fill) * S)
        translate([-glyph(ch)[3] * s, 0])
            text(ch, size = s, font = font, halign = "center", valign = "center");
}

module hole_2d(k) {
    if (is_glyph) glyph_hole(PIECES[k]);
    else scale(S) shape_unit(PIECES[k]);
}

module piece_2d(k) { offset(r = -clearance) hole_2d(k); }

// ---------------------------------------------------------------- knobs

// Knob seat of each piece, in units of the piece size: the point of the hole
// furthest from its edge (on a 1 mm grid at piece_size 80), and that distance
// less 0.3 mm. Measured once from export_hole renders; verify.sh's probe_knob
// checks on the rendered geometry that every knob sits on its piece.
KNOB_TABLE = [
    ["circle", 0.000, 0.000, 0.496], ["square", 0.000, 0.000, 0.416],
    ["triangle", 0.000, -0.138, 0.279], ["star", 0.000, -0.050, 0.246],
    ["heart", 0.000, -0.037, 0.237], ["hexagon", 0.000, 0.000, 0.429],
    ["cat", 0.000, -0.100, 0.316], ["fish", 0.087, 0.000, 0.256], ["bunny", 0.000, -0.212, 0.261],
    ["turtle", 0.000, 0.000, 0.226], ["bird", -0.050, -0.100, 0.256],
    ["whale", 0.037, -0.075, 0.240], ["A", 0.000, 0.250, 0.213], ["B", -0.212, 0.025, 0.183],
    ["C", -0.237, -0.087, 0.157], ["D", -0.275, -0.312, 0.170], ["E", -0.163, 0.013, 0.186],
    ["F", -0.163, 0.013, 0.187], ["G", 0.287, -0.075, 0.161], ["H", -0.250, 0.013, 0.188],
    ["I", 0.000, 0.000, 0.159], ["J", 0.062, -0.250, 0.134], ["K", -0.212, 0.013, 0.252],
    ["L", -0.175, -0.312, 0.170], ["M", -0.250, 0.087, 0.231], ["N", -0.200, 0.125, 0.237],
    ["O", -0.312, -0.100, 0.157], ["P", -0.212, -0.050, 0.187], ["Q", 0.113, -0.250, 0.146],
    ["R", -0.250, -0.025, 0.185], ["S", 0.212, -0.100, 0.155], ["T", 0.000, 0.300, 0.183],
    ["U", -0.250, -0.175, 0.155], ["V", 0.000, -0.237, 0.223], ["W", -0.200, -0.050, 0.192],
    ["X", 0.000, -0.037, 0.200], ["Y", 0.000, 0.037, 0.192], ["Z", -0.163, -0.275, 0.198],
    ["0", -0.237, 0.000, 0.154], ["1", 0.000, -0.300, 0.171], ["2", -0.087, -0.275, 0.195],
    ["3", 0.113, 0.013, 0.159], ["4", 0.062, 0.237, 0.217], ["5", -0.100, 0.250, 0.229],
    ["6", -0.175, 0.075, 0.192], ["7", 0.175, 0.300, 0.182], ["8", -0.225, -0.212, 0.149],
    ["9", 0.175, -0.075, 0.196]
];
function knob_row(name) = let(i = search([name], KNOB_TABLE, 1, 0)[0])
    i == [] || i == undef ? [name, 0, 0, 0.1] : KNOB_TABLE[i];
function knob_at(k) = let(r = knob_row(PIECES[k])) [r[1] * S, r[2] * S];
function knob_d(k) = let(r = knob_row(PIECES[k]))
    min(knob_diameter, 2 * (r[3] * S - clearance - knob_margin));
function has_knob(k) = knobs && knob_d(k) >= knob_min;

// ---------------------------------------------------------------- solids

piece_colors = [piece_color_1, piece_color_2, piece_color_3, piece_color_4, piece_color_5, piece_color_6,
                piece_color_7, piece_color_8, piece_color_9, piece_color_10, piece_color_11, piece_color_12];
function pcol(k) = piece_colors[k % 12];

module stepped(h, r) {
    linear_extrude(foot) offset(delta = -foot) children();
    translate([0, 0, foot]) linear_extrude(h - r - foot) children();
    for (i = [1:round_steps]) {
        z0 = h - r + r * (i - 1) / round_steps;
        in = r - sqrt(max(0, r * r - pow(r * i / round_steps, 2)));
        translate([0, 0, z0]) linear_extrude(r / round_steps) offset(r = -in) children();
    }
}

module tray_2d() {
    offset(r = 6) offset(delta = -6) square([TW, TD]);
}

module hint_2d(k) { offset(r = -hint_inset) hole_2d(k); }

module tray() {
    difference() {
        stepped(TRAY_H, tray_round) tray_2d();
        for (k = [0:N - 1]) translate(hole_c(k)) {
            translate([0, 0, floor_thickness]) linear_extrude(hole_depth + 1) hole_2d(k);
            // Lead-in chamfer: 45 degrees, lead_in wide at the top.
            for (i = [1:4]) translate([0, 0, TRAY_H - lead_in * i / 4])
                linear_extrude(lead_in) offset(delta = lead_in * (5 - i) / 4) hole_2d(k);
            if (color_hints) translate([0, 0, floor_thickness - hint_depth])
                linear_extrude(hint_depth + 0.01) hint_2d(k);
        }
    }
}

module hint(k) {
    translate(hole_c(k)) translate([0, 0, floor_thickness - hint_depth]) linear_extrude(hint_depth) hint_2d(k);
}

module piece(k) {
    stepped(PT, piece_round) piece_2d(k);
    if (has_knob(k)) translate(knob_at(k)) translate([0, 0, PT - 0.01]) {
        d = knob_d(k);
        cylinder(d = d, h = knob_height - d / 2 * 0.6 + 0.01);
        translate([0, 0, knob_height - d / 2 * 0.6]) scale([1, 1, 0.6]) sphere(d = d);
    }
}

// ---------------------------------------------------------------- report

NARROW = [for (k = [0:N - 1]) if (knobs && knob_d(k) < knob_diameter) k];
NO_KNOB = [for (k = [0:N - 1]) if (knobs && !has_knob(k)) k];
if (len(letters) > 0 && set == "letters" && len(WORD) < len(letters))
    echo(str("NOTE: only A-Z and 0-9 make pieces; ", len(letters) - len(WORD), " other character(s) skipped"));
if (set == "letters" && len(WORD) == 0)
    echo("NOTE: the word has no letters or numbers; one A piece is made");
if (S < piece_size)
    echo(str("NOTE: pieces are ", S, " mm (", piece_size, " asked) so the tray fits the plate"));
if (layout == "both" && !BOTH_FIT)
    echo(str("NOTE: the tray and the pieces do not fit one plate together; this is the tray only - print the pieces with layout = pieces"));
if (len(NO_KNOB) > 0)
    echo(str("NOTE: ", len(NO_KNOB), " piece(s) have no room for a knob and are made without one"));
if (len(NARROW) > len(NO_KNOB))
    echo(str("NOTE: ", len(NARROW) - len(NO_KNOB), " knob(s) are narrower than ", knob_diameter,
             " mm to sit on their piece (narrowest ", round(min([for (k = NARROW) if (has_knob(k)) knob_d(k)]) * 10) / 10, " mm)"));

echo(str("SB_SORTER n=", N, " size=", S, " tray=[", TW, ", ", TD, "] grid=[", TC, ", ", TR,
         "] pieces=[", PW, ", ", PD, "] pgrid=[", PC, ", ", PR, "] both_fit=", BOTH_FIT,
         " tray_shown=", SHOW_TRAY, " pieces_shown=", SHOW_PIECES, " piece_h=", PT,
         " knobs=[", [for (k = [0:N - 1]) has_knob(k) ? knob_d(k) : 0], "]",
         " holes=[", [for (k = [0:N - 1]) hole_c(k)], "]"));

// ---------------------------------------------------------------- output

if (export_hole >= 0) {
    hole_2d(export_hole);
} else if (probe_fit > 0) {
    intersection() {
        tray();
        for (k = [0:N - 1]) translate(hole_c(k)) translate([0, 0, floor_thickness + 0.01])
            linear_extrude(PT) offset(r = probe_fit) piece_2d(k);
    }
    translate([-1000, 0, 0]) cube(1);
} else if (probe_cell) {
    linear_extrude(1) for (k = [0:N - 1]) translate(hole_c(k)) difference() {
        offset(delta = lead_in) hole_2d(k);
        square(S + 2 * lead_in + 0.01, center = true);
    }
    // And the lead-in never reaches another hole or the edge: holes grown
    // by the lead-in stay tray_wall - 2 * lead_in apart (checked as overlap).
    linear_extrude(1) for (k = [0:N - 1]) intersection() {
        translate(hole_c(k)) offset(delta = lead_in + (tray_wall / 2 - lead_in - 0.05)) hole_2d(k);
        union() for (j = [0:N - 1]) if (j != k)
            translate(hole_c(j)) offset(delta = lead_in + (tray_wall / 2 - lead_in - 0.05)) hole_2d(j);
    }
    translate([-1000, 0, 0]) cube(1);
} else if (probe_knob) {
    linear_extrude(1) for (k = [0:N - 1]) if (has_knob(k)) translate(hole_c(k)) difference() {
        translate(knob_at(k)) circle(d = knob_d(k) + 2 * (knob_margin - 0.1));
        piece_2d(k);
    }
    translate([-1000, 0, 0]) cube(1);
} else {
    if (SHOW_TRAY) {
        color(tray_color) tray();
        if (color_hints) for (k = [0:N - 1]) color(pcol(k)) hint(k);
    }
    if (SHOW_PIECES) for (k = [0:N - 1]) color(pcol(k)) translate(piece_c(k)) piece(k);
}
