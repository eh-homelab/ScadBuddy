// Desk Organizer — a pen cup and a matching tray, as a set or on their own,
// in horizontal colour stripes or vertical colour blocks, with a name inlaid
// flush in the front of each piece.
//
// Four shapes: round, hexagon, square, and stacked rings (a stack of
// rounded bands, like stacked rings of dough). The rings' undersides are
// 45-degree slopes, so every shape prints upright without supports. The cup
// is kept stable: its height is capped at twice its footprint width.
//
// The name is a flush inlay: text_depth into the front wall, filled in
// text_color, so the wall stays smooth to the touch and never thinner than
// 0.8 mm behind the letters.
//
// Colours: each distinct colour is one part and one filament. The colour
// parameters, in source order, are the extruder order: color_1 .. color_6
// (the pattern cycles through the first pattern_colors of them), then
// text_color. A colour the chosen options do not use produces no part and
// takes no extruder.
//
// Written to the MakerWorld Parametric Model Maker customizer conventions.

/* [Set] */

// Which pieces to print
pieces = "set"; // [set:Pen cup and tray, cup:Pen cup only, tray:Tray only]

// Shape of both pieces
shape = "round"; // [round:Round, hex:Hexagon, square:Square, stacked_rings:Stacked rings]

// Wall thickness in mm
wall = 2.4; // [1.2:0.2:5]

// Floor thickness in mm
floor_thickness = 2.4; // [1.2:0.2:5]

/* [Pen cup] */

// Cup size in mm: diameter (round, stacked rings) or across the flats (hexagon, square)
cup_width = 80; // [50:1:130]

// Cup height in mm. Capped at twice the footprint width so it does not tip over.
cup_height = 100; // [40:1:200]

// Compartments in the cup
cup_compartments = 1; // [1:One, 2:Two - a wall across, 3:Three, 4:Four, 6:Six]

// Height of the cup's dividers, % of the cup height
cup_divider_height = 70; // [20:5:100]

/* [Tray] */

// Tray length in mm (X)
tray_length = 180; // [80:5:250]

// Tray width in mm (Y)
tray_width = 80; // [50:5:150]

// Tray height in mm
tray_height = 25; // [10:1:60]

// Compartments along the tray
tray_compartments = 3; // [1:1:6]

// Height of the tray's dividers, % of the tray height
tray_divider_height = 80; // [20:5:100]

/* [Pattern] */

// Colour pattern across both pieces
pattern = "stripes"; // [solid:Solid - color_1 only, stripes:Horizontal stripes, blocks:Colour blocks - vertical panels]

// How many of the colours the pattern cycles through
pattern_colors = 3; // [2:1:6]

// Stripe height in mm (the stacked-rings shape stripes ring by ring instead)
stripe_height = 12; // [3:1:50]

// Stacked rings: height of one ring in mm
ring_height = 10; // [6:1:20]

/* [Name] */

// Name inlaid in the front of the pieces
name = "Reagan"; // 24

// Which pieces carry the name
name_on = "both"; // [both:Both, cup:Cup only, tray:Tray only, none:None]

// Typeface
font = "DejaVu Sans:style=Bold"; // font

// Letter height in mm; shrunk automatically to fit the front face
text_size = 14; // [5:1:40]

// Inlay depth in mm (at most the wall minus 0.8 mm)
text_depth = 0.8; // [0.4:0.2:2]

/* [Colours] */

// Colour 1 (the whole set when the pattern is Solid)
color_1 = "#26A69A"; // color

// Colour 2
color_2 = "#FFCA28"; // color

// Colour 3
color_3 = "#EF5350"; // color

// Colour 4
color_4 = "#42A5F5"; // color

// Colour 5
color_5 = "#AB47BC"; // color

// Colour 6
color_6 = "#FFFFFF"; // color

// Name
text_color = "#263238"; // color

/* [Hidden] */

$fn = 72;
// The tray is never narrower in X than in Y.
TW = min(tray_width, tray_length);
if (TW < tray_width && pieces != "cup")
    echo(str("NOTE: tray_width reduced from ", tray_width, " to ", TW, " mm, the tray length"));
BED_X = 300;          // H2C plate using both nozzles
BED_Y = 320;
GAP = 10;             // between the pieces on the plate
BEHIND_TEXT = 0.8;    // wall left behind the inlay, at least
EPS = 0.01;
COLORS = [color_1, color_2, color_3, color_4, color_5, color_6];
NC = pattern == "solid" ? 1 : pattern_colors;

RINGS = shape == "stacked_rings";
RR = ring_height / 2;                 // ring profile radius
TROUGH = 0.66 * RR;                   // deepest inset between two rings (0.65 r exactly; polygons sag a bit)
TOP_RIM = 0.4;                        // flat on top of the top ring, at least
// Profile of one ring, as (inset from the outline, height above the ring's
// centre): a circle from 90 degrees down to -45, then a 45-degree slope to a
// point below the centre, so the underside never overhangs more than 45.
// Above the centre the inset stops TOP_RIM short of the cavity (TROUGH +
// wall in): a tall ring's crest curves in by up to RR, and past the cavity
// the top ring would be cut and the piece come out short (#1616). Only the
// top ring shows it; the others' upper halves are inside the ring above.
RING_PROFILE = concat([for (a = [90 : -15 : -45])
                          [a > 0 ? min(RR - RR * cos(a), TROUGH + wall - TOP_RIM) : RR - RR * cos(a), RR * sin(a)]],
                      [[RR, -RR * sqrt(2)]]);
FOOT_INSET = RINGS ? 0.586 * RR : 0;  // inset of the outline at z = 0
// Height of the boundary between ring i-1 and ring i (at the trough).
function ring_boundary(i) = i * ring_height - 0.0636 * RR;
function ring_count(h) = max(2, round(h / ring_height));

// ===========================================================================
// Outlines. grow > 0 pushes the outline out, < 0 pulls it in.
// ===========================================================================

module cup_outline(grow) {
    if (shape == "hex") circle(r = (cup_width / 2 + grow) / cos(30), $fn = 6);
    else if (shape == "square") offset(r = 4) square(cup_width - 8 + 2 * grow, center = true);
    else circle(d = cup_width + 2 * grow);
}

module tray_outline(grow) {
    if (shape == "hex") {
        // Pointed ends; never more than a quarter of the length each.
        c = min(TW / 2 * tan(30), tray_length / 4);
        offset(delta = grow) polygon([[-tray_length / 2, 0], [-tray_length / 2 + c, -TW / 2],
                                      [tray_length / 2 - c, -TW / 2], [tray_length / 2, 0],
                                      [tray_length / 2 - c, TW / 2], [-tray_length / 2 + c, TW / 2]]);
    } else if (shape == "square") offset(r = 4) square([tray_length - 8 + 2 * grow, TW - 8 + 2 * grow], center = true);
    else hull() for (s = [-1, 1]) translate([s * (tray_length - TW) / 2, 0]) circle(d = TW + 2 * grow);
}

module outline(kind, grow) { if (kind == "cup") cup_outline(grow); else tray_outline(grow); }

// ===========================================================================
// Pieces. The outer solid is a straight prism, or a stack of rings: each
// ring is the hull of thin slices of the outline, inset along RING_PROFILE
// (the outlines are all convex, so the hull is exact).
// ===========================================================================

module outer(kind, h, grow) {
    if (RINGS)
        intersection() {
            union() for (i = [0 : ring_count(h) - 1]) hull() for (pt = RING_PROFILE)
                translate([0, 0, RR + i * ring_height + pt[1]]) linear_extrude(EPS) outline(kind, grow - pt[0]);
            translate([-500, -500, 0]) cube([1000, 1000, h]);
        }
    else linear_extrude(h) outline(kind, grow);
}

// Inset of the cavity, measured from the outline.
CAVITY = RINGS ? TROUGH + wall : wall;

module cavity_2d(kind) { outline(kind, -CAVITY); }

module dividers(kind, h) {
    dh = floor_thickness + (h - floor_thickness) * (kind == "cup" ? cup_divider_height : tray_divider_height) / 100;
    n = kind == "cup" ? cup_compartments : tray_compartments;
    intersection() {
        translate([0, 0, floor_thickness - EPS]) linear_extrude(dh - floor_thickness + EPS) cavity_2d(kind);
        if (kind == "cup" && n == 2)
            translate([-cup_width, -wall / 2, 0]) cube([2 * cup_width, wall, dh]);
        else if (kind == "cup" && n > 2)
            for (k = [0 : n - 1]) rotate(90 + k * 360 / n) translate([0, -wall / 2, 0]) cube([cup_width, wall, dh]);
        else if (kind == "tray" && n > 1)
            for (k = [1 : n - 1])
                translate([-tray_length / 2 + k * tray_length / n - wall / 2, -TW, 0]) cube([wall, 2 * TW, dh]);
    }
}

// Height of each piece: the cup is capped for stability, the rings shape
// snaps to whole rings.
CUP_FOOT = cup_width - 2 * FOOT_INSET;             // narrowest footprint width
CUP_H_MAX = 2 * CUP_FOOT;
CUP_H_ASK = min(cup_height, CUP_H_MAX);
CUP_H = RINGS ? ring_count(CUP_H_ASK) * ring_height - (ring_count(CUP_H_ASK) * ring_height > CUP_H_MAX ? ring_height : 0)
              : CUP_H_ASK;
if (cup_height > CUP_H_MAX && pieces != "tray")
    echo(str("NOTE: cup_height reduced from ", cup_height, " to ", CUP_H,
             " mm so the cup stays stable (at most twice its ", CUP_FOOT, " mm footprint)"));
TRAY_H = RINGS ? ring_count(tray_height) * ring_height : tray_height;
function piece_h(kind) = kind == "cup" ? CUP_H : TRAY_H;

TD = min(text_depth, wall - BEHIND_TEXT);
// wall - BEHIND_TEXT is inexact (1.2 - 0.8 < 0.4), so allow for rounding.
if (TD < text_depth - 1e-6 && name_on != "none" && name != "")
    echo(str("NOTE: text_depth reduced from ", text_depth, " to ", TD, " mm to leave ", BEHIND_TEXT, " mm of wall behind the name"));

HAS_NAME = len(name) > 0 && name != " ";
function named(kind) = HAS_NAME && (name_on == "both" || name_on == kind);

// ===========================================================================
// The name: fitted to the front face, extruded straight back through the
// front wall, and kept only in the outer text_depth of the wall.
// ===========================================================================

// Shrink-only fit (as in name-sign): a hair-thin bar of the target size
// makes resize() leave smaller text alone.
module fit(w, h) {
    projection(cut = true) translate([0, 0, -0.5])
        resize([w, 0, 0], auto = [false, true, false]) union() {
            linear_extrude(1) resize([0, h, 0], auto = [true, false, false]) union() {
                text(name, size = text_size, font = font, halign = "center", valign = "center");
                translate([0, -text_size / 2]) square([EPS, text_size]);
            }
            translate([-w / 2, 0, 10]) cube([w, EPS, EPS]);
        }
}

function face_w(kind) =
    kind == "cup" ? (shape == "hex" ? cup_width / sqrt(3) * 0.8 : shape == "square" ? (cup_width - 8) * 0.9 : cup_width * 0.6)
                  : (shape == "hex" ? (tray_length - 2 * min(TW / 2 * tan(30), tray_length / 4)) * 0.85
                     : shape == "square" ? (tray_length - 8) * 0.9 : max((tray_length - TW) * 0.9, TW * 0.6));
// Stacked rings: the name sits on one ring, centred on its crest.
function face_h(kind) = min(text_size, RINGS ? 0.8 * ring_height : piece_h(kind) * (kind == "cup" ? 0.4 : 0.6));
function text_z(kind) = RINGS ? RR + floor(ring_count(piece_h(kind)) / 2) * ring_height : piece_h(kind) / 2;

module name_2d(kind) { fit(face_w(kind), face_h(kind)) children(); }

module name_inlay(kind) {
    h = piece_h(kind);
    intersection() {
        translate([0, 0, text_z(kind)]) rotate([90, 0, 0]) linear_extrude(500) name_2d(kind);
        difference() { outer(kind, h, 0); translate([0, 0, -1]) outer(kind, h + 2, -TD); }
    }
}

// ===========================================================================
// The coloured parts.
// ===========================================================================

module body(kind) {
    h = piece_h(kind);
    difference() {
        union() {
            difference() {
                outer(kind, h, 0);
                translate([0, 0, floor_thickness]) linear_extrude(h) cavity_2d(kind);
            }
            dividers(kind, h);
        }
        if (named(kind)) name_inlay(kind);
    }
}

// Where colour c (0-based) goes, as a solid the body is cut with.
module colour_zone(kind, c) {
    h = piece_h(kind);
    if (pattern == "solid") { if (c == 0) translate([-500, -500, -1]) cube([1000, 1000, h + 2]); }
    else if (pattern == "stripes") {
        n = RINGS ? ring_count(h) : ceil(h / stripe_height);
        for (k = [0 : n - 1]) if (k % NC == c) {
            z0 = k == 0 ? -1 : RINGS ? ring_boundary(k) : k * stripe_height;
            z1 = k == n - 1 ? h + 1 : RINGS ? ring_boundary(k + 1) : (k + 1) * stripe_height;
            translate([-500, -500, z0]) cube([1000, 1000, z1 - z0]);
        }
    } else if (kind == "cup") {
        // Sectors, the first centred on the front.
        s = 360 / NC;
        rotate(-90 - s / 2 + c * s) translate([0, 0, -1]) linear_extrude(h + 2)
            polygon(concat([[0, 0]], [for (a = [0 : s / 8 : s + EPS]) 300 * [cos(a), sin(a)]]));
    } else {
        // Panels along the tray.
        w = tray_length / NC;
        translate([-tray_length / 2 + c * w - (c == 0 ? 50 : 0), -500, -1])
            cube([w + (c == 0 ? 50 : 0) + (c == NC - 1 ? 50 : 0), 1000, h + 2]);
    }
}

module piece(kind) {
    for (c = [0 : NC - 1]) color(COLORS[c]) intersection() { body(kind); colour_zone(kind, c); }
    if (named(kind)) color(text_color) name_inlay(kind);
}

// ===========================================================================
// Layout: side by side, or the cup above the tray when that does not fit.
// ===========================================================================

CUP_X = shape == "hex" ? cup_width / cos(30) : cup_width;   // extent in X
CUP_Y = cup_width;
SHOW_CUP = pieces != "tray";
SHOW_TRAY = pieces != "cup";
ROW = !(SHOW_CUP && SHOW_TRAY) || CUP_X + GAP + tray_length <= BED_X;
CUP_AT = !(SHOW_CUP && SHOW_TRAY) ? [0, 0]
       : ROW ? [-(CUP_X + GAP + tray_length) / 2 + CUP_X / 2, 0]
       : [0, (CUP_Y + GAP + TW) / 2 - CUP_Y / 2];
TRAY_AT = !(SHOW_CUP && SHOW_TRAY) ? [0, 0]
        : ROW ? [(CUP_X + GAP + tray_length) / 2 - tray_length / 2, 0]
        : [0, -(CUP_Y + GAP + TW) / 2 + TW / 2];

echo(ORGANIZER = [CUP_H, CUP_FOOT, TRAY_H, TD, NC, ROW ? 1 : 0, CAVITY]);

if (SHOW_CUP) translate(CUP_AT) piece("cup");
if (SHOW_TRAY) translate(TRAY_AT) piece("tray");
