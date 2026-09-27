// Train track — wooden-railway-compatible track pieces: straights, curves, a
// ramp, a 90-degree crossing, an end stop with a buffer, and a name tile with a
// word inlaid between the rails. Pieces join each other and the common
// 40 mm wooden track with the usual round peg and socket.
//
// Profile (see README for sources): 40 mm wide, 12 mm high, two 6 mm x 3 mm
// grooves 26 mm apart centre to centre; peg head Ø11.5 on a 7 mm neck, socket
// the peg plus clearance. Standard straights are 54 / 108 / 144 / 216 mm, the
// large curve is 45 degrees at 182 mm inner radius, the ascending track rises
// 64 mm over 216 mm.
//
// Every piece prints flat on its bottom face with the grooves up and no
// supports: the profile is vertical walls and an open-topped groove, the pegs
// sit on the bed, the sockets are through-holes. The ramp is solid under its
// deck, and a male peg at its top end stands on a post down to the bed, which
// passes through the mating track's socket hole.
//
// Written to the MakerWorld Parametric Model Maker customizer conventions so
// the same file works unchanged on MakerWorld and in ScadBuddy.
//
// The colour parameters are the extruder order: track_color is extruder 1,
// text_color extruder 2 (only used when there is text).

/* [Piece] */

// Which piece
type = "straight"; // [straight:Straight, curve:Curve, ramp:Ramp (ascending), crossing:Crossing (90°), end_stop:End stop, name_tile:Name tile]

// Length in mm of a straight, ramp, crossing arm, end stop or name tile (54, 108, 144, 216 are the standard sizes; socket-socket pieces and crossings with sockets are at least 54)
length = 144; // [36:18:216]

// Curve inner-edge radius in mm (182 = the standard large curve, 90 = the short curve; a short socket-socket curve gets a larger radius)
curve_radius = 182; // [80:2:300]

// Curve angle in degrees (45 = the standard curve, 8 make a circle)
curve_angle = 45; // [22.5:22.5:90]

// Ramp height gain in mm (64 = one standard level)
ramp_rise = 64; // [16:4:96]

// Connector at each end (first, second)
connectors = "male_female"; // [male_female:Peg - socket, male_male:Peg - peg, female_female:Socket - socket]

/* [Fit] */

// Gap around the peg in each socket, mm. Raise it if pegs are tight
connector_clearance = 0.3; // [0.1:0.05:0.6]

/* [Decor] */

// Word inlaid between the rails of a name tile, or in the side of a straight
text = ""; // 20

// Typeface (the app fills this dropdown from the fonts installed in the image)
font = "DejaVu Sans:style=Bold"; // font

/* [Colors] */

// Track colour (extruder 1)
track_color = "#C8A06A"; // color

// Text colour (extruder 2)
text_color = "#5D4037"; // color

/* [Hidden] */

$fn = 72;

W = 40;                 // track width
T = 12;                 // track height
GROOVE_W = 6;
GROOVE_D = 3;
GROOVE_C = 13;          // groove centre from the track centre line (26 apart)
EDGE_CH = 1;            // chamfer on the top outside edges
GROOVE_CH = 0.5;        // chamfer on the groove lips
PEG_D = 11.5;
PEG_NECK = 7;           // neck length, end face to the head
PEG_NECK_W = 6.5;
HEAD_C = PEG_NECK + PEG_D / 2;      // head centre from the end face, 12.75
// The socket is sized for the largest common peg (12 mm head, 7 mm neck), so
// store-bought track fits too; printed pairs then join with a little play.
SOCKET_D = 12 + 2 * connector_clearance;
SOCKET_THROAT_W = 7 + 2 * connector_clearance;
MOUTH_CH = 0.8;         // flare at the socket mouth
TEXT_DEPTH = 1;

kind_a = connectors == "female_female" ? "female" : "male";
kind_b = connectors == "male_male" ? "male" : "female";

// Two sockets must leave material between them, or they cut the piece in two.
// A socket reaches SOCK_IN from its end face. Two facing sockets need
// 2 x SOCK_IN + 3 mm; a crossing's sockets on neighbouring arms need each
// socket to stop a socket radius + 1 mm short of the centre. Too short a piece
// is lengthened to the next 18 mm step, and too tight a socket-socket curve
// gets a larger radius (the angle is kept, so curves still make a circle).
SOCK_IN = HEAD_C + SOCKET_D / 2;
two_sockets = connectors == "female_female";
min_len = type == "crossing" && connectors != "male_male" ? 2 * (SOCK_IN + SOCKET_D / 2 + 1)
        : (type == "straight" || type == "name_tile" || type == "ramp") && two_sockets
          ? 2 * SOCK_IN + 3 : 0;
L = length >= min_len ? length : 18 * ceil(min_len / 18);
min_rc = type == "curve" && two_sockets ? (2 * SOCK_IN + 3) / (curve_angle * PI / 180) : 0;
RI = curve_radius + W / 2 >= min_rc ? curve_radius : 2 * ceil((min_rc - W / 2) / 2);
RC = RI + W / 2;                    // centre-line radius
A = curve_angle;
RISE = ramp_rise;

if (L != length)
    echo(str("NOTE: length raised from ", length, " to ", L, " mm so the two sockets do not cut the piece in two"));
if (RI != curve_radius)
    echo(str("NOTE: curve_radius raised from ", curve_radius, " to ", RI, " mm so the two sockets do not cut the curve in two"));

// ---------------------------------------------------------------- profile

// Cross-section in (across, up): 2D x is across the track, 2D y is up.
module deck_2d() {
    polygon([[-W / 2, 0], [W / 2, 0], [W / 2, T - EDGE_CH], [W / 2 - EDGE_CH, T],
             [-W / 2 + EDGE_CH, T], [-W / 2, T - EDGE_CH]]);
}

module grooves_2d() {
    for (s = [-1, 1]) translate([s * GROOVE_C, 0])
        polygon([[-GROOVE_W / 2, T - GROOVE_D], [GROOVE_W / 2, T - GROOVE_D],
                 [GROOVE_W / 2, T - GROOVE_CH], [GROOVE_W / 2 + GROOVE_CH, T],
                 [GROOVE_W / 2 + GROOVE_CH, T + 1], [-GROOVE_W / 2 - GROOVE_CH, T + 1],
                 [-GROOVE_W / 2 - GROOVE_CH, T], [-GROOVE_W / 2, T - GROOVE_CH]]);
}

module profile_2d() { difference() { deck_2d(); grooves_2d(); } }

// A 2D cross-section swept along +x from x0 to x1: 2D (a, b) -> (x, a, b).
module sweep_x(x0, x1) {
    translate([x0, 0, 0]) rotate([90, 0, 90]) linear_extrude(x1 - x0) children();
}

// ---------------------------------------------------------------- connectors

// In an end's frame: end face at x = 0, piece at x < 0, pointing +x.
module peg(h) {
    linear_extrude(h) {
        translate([-1, -PEG_NECK_W / 2]) square([HEAD_C + 1, PEG_NECK_W]);
        translate([HEAD_C, 0]) circle(d = PEG_D);
    }
}

module socket(h) {
    translate([0, 0, -1]) linear_extrude(h + 2) {
        translate([-HEAD_C, 0]) circle(d = SOCKET_D);
        translate([-HEAD_C, -SOCKET_THROAT_W / 2]) square([HEAD_C + 1, SOCKET_THROAT_W]);
        // flared mouth
        polygon([[0.01, -SOCKET_THROAT_W / 2 - MOUTH_CH], [0.01, SOCKET_THROAT_W / 2 + MOUTH_CH],
                 [-MOUTH_CH, SOCKET_THROAT_W / 2], [-MOUTH_CH, -SOCKET_THROAT_W / 2]]);
    }
}

// End frames of the current piece: [position, outward heading in degrees,
// connector kind, z of the deck bottom].
function ends() =
    type == "curve" ?
        [[[RC, 0], -90, kind_a, 0],
         [[RC * cos(A), RC * sin(A)], A + 90, kind_b, 0]]
    : type == "crossing" ?
        [[[-L / 2, 0], 180, kind_a, 0], [[L / 2, 0], 0, kind_b, 0],
         [[0, -L / 2], -90, kind_a, 0], [[0, L / 2], 90, kind_b, 0]]
    : type == "end_stop" ?
        [[[-L / 2, 0], 180, kind_a, 0]]
    : type == "ramp" ?
        [[[-L / 2, 0], 180, kind_a, 0], [[L / 2, 0], 0, kind_b, RISE]]
    :   [[[-L / 2, 0], 180, kind_a, 0], [[L / 2, 0], 0, kind_b, 0]];

// A peg at the top of the ramp stands on a post down to the bed.
module pegs() {
    for (e = ends()) if (e[2] == "male")
        translate([e[0][0], e[0][1], 0]) rotate([0, 0, e[1]]) peg(e[3] + T);
}

module sockets() {
    for (e = ends()) if (e[2] == "female")
        translate([e[0][0], e[0][1], 0]) rotate([0, 0, e[1]]) socket(e[3] + T);
}

// ---------------------------------------------------------------- pieces

// Ramp centre line: level landings at both ends, a cosine S between them.
FLAT = min(20, L / 6);
N_SEG = 32;
function ramp_x(i) = -L / 2 + FLAT + (L - 2 * FLAT) * i / N_SEG;
function ramp_z(i) = RISE * (1 - cos(180 * i / N_SEG)) / 2;

module ramp() {
    // Level landings.
    sweep_x(-L / 2, -L / 2 + FLAT) profile_2d();
    translate([0, 0, RISE]) sweep_x(L / 2 - FLAT, L / 2) profile_2d();
    // Sheared segments: each keeps the profile vertical, so the grooves stay
    // 3 mm deep measured straight down.
    for (i = [0 : N_SEG - 1]) {
        x0 = ramp_x(i); x1 = ramp_x(i + 1);
        z0 = ramp_z(i); z1 = ramp_z(i + 1);
        k = (z1 - z0) / (x1 - x0);
        multmatrix([[1, 0, 0, 0], [0, 1, 0, 0], [k, 0, 1, z0 - k * x0], [0, 0, 0, 1]])
            sweep_x(x0, x1) profile_2d();
    }
    // Solid underneath, up into the deck.
    rotate([90, 0, 0]) linear_extrude(W, center = true)
        polygon(concat([[-L / 2, 0], [L / 2, 0], [L / 2, RISE + 1]],
                       [for (i = [N_SEG : -1 : 0]) [ramp_x(i), ramp_z(i) + 1]],
                       [[-L / 2, 1]]));
}

module curve() {
    rotate_extrude(angle = A) translate([RC, 0]) profile_2d();
}

module crossing() {
    difference() {
        union() {
            sweep_x(-L / 2, L / 2) deck_2d();
            rotate([0, 0, 90]) sweep_x(-L / 2, L / 2) deck_2d();
        }
        sweep_x(-L / 2 - 1, L / 2 + 1) grooves_2d();
        rotate([0, 0, 90]) sweep_x(-L / 2 - 1, L / 2 + 1) grooves_2d();
    }
}

// Buffer across the far end: a block with a rounded top.
BUF_L = 14;
BUF_H = 26;
module buffer() {
    hull() {
        translate([L / 2 - BUF_L, -W / 2, 0]) cube([BUF_L, W, BUF_H - BUF_L / 2]);
        translate([L / 2 - BUF_L / 2, 0, BUF_H - BUF_L / 2])
            rotate([90, 0, 0]) cylinder(d = BUF_L, h = W, center = true);
    }
}

module straight() {
    sweep_x(-L / 2, L / 2) profile_2d();
}

module base() {
    if (type == "curve") curve();
    else if (type == "ramp") ramp();
    else if (type == "crossing") crossing();
    else if (type == "end_stop") { straight(); buffer(); }
    else straight();
}

// ---------------------------------------------------------------- text

on_side = type == "straight";
on_top = type == "name_tile";
// The top strip between the grooves, clear of the socket at each end; or the
// side face below the edge chamfer.
SOCKET_REACH = HEAD_C + SOCKET_D / 2 + 2;
box_w = on_top ? L - 2 * SOCKET_REACH : L - 8;
box_h = on_top ? 2 * (GROOVE_C - GROOVE_W / 2 - GROOVE_CH) - 3 : T - EDGE_CH - 3;
side_zc = 1.5 + box_h / 2;
text_fits = len(text) > 0 && (on_side || on_top) && box_w >= 6 && box_h >= 3;

// Shrink-only fit, as in models/name-sign.
module fit_x(w) {
    projection(cut = true) translate([0, 0, -0.5])
        resize([w, 0, 0], auto = [false, true, false])
            union() {
                linear_extrude(1) children();
                translate([-w / 2, 0, 10]) cube([w, 0.01, 0.01]);
            }
}

module fit_y(h) {
    projection(cut = true) translate([0, 0, -0.5])
        resize([0, h, 0], auto = [true, false, false])
            union() {
                linear_extrude(1) children();
                translate([0, -h / 2, 10]) cube([0.01, h, 0.01]);
            }
}

module word_2d() {
    intersection() {
        fit_y(box_h) fit_x(box_w)
            text(text, size = box_h * 0.7, font = font, halign = "center", valign = "center");
        square([box_w, box_h], center = true);
    }
}

module pocket() {
    if (text_fits) {
        if (on_top)
            translate([0, 0, T - TEXT_DEPTH]) linear_extrude(TEXT_DEPTH + 0.001) word_2d();
        else
            translate([0, -W / 2 + TEXT_DEPTH, side_zc])
                rotate([90, 0, 0]) linear_extrude(TEXT_DEPTH + 0.001) word_2d();
    }
}

// ---------------------------------------------------------------- parts

module piece() {
    difference() {
        union() { base(); pegs(); }
        sockets();
    }
}

color(track_color) difference() { piece(); pocket(); }
if (text_fits) color(text_color) intersection() { piece(); pocket(); }
