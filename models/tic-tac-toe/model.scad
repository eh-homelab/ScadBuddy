// Travel tic-tac-toe — a board with nine recessed wells and inlaid grid lines,
// plus five pieces for each player. Pieces are classic X and O, or a star and
// a heart.
//
// Two styles:
//   flat_board       - a flat board; everything prints side by side.
//   box_with_storage - a box that holds all ten pieces, closed by a sliding
//                      lid whose top is the board. The lid runs in 45-degree
//                      dovetail grooves in the side walls and slides out
//                      through the front.
//
// Everything prints flat without supports: the board (or box, open side up,
// with the lid face up beside it), and the pieces in two rows in front.
//
// Written to the MakerWorld Parametric Model Maker customizer conventions so
// the same file works unchanged on MakerWorld and in ScadBuddy.
//
// The colour parameters are the extruder order: board_color is extruder 1,
// grid_color 2, x_color 3, o_color 4.

/* [Board] */

// Board side length in mm (the box is this square too)
size = 100; // [70:5:150]

// Board thickness in mm; on the box it is the lid's thickness
thickness = 5; // [3:0.5:8]

// Flat board, or a box that stores the pieces under a sliding board lid
style = "flat_board"; // [flat_board:Flat board, box_with_storage:Box with sliding lid]

/* [Pieces] */

// Piece shapes
piece_style = "classic_xo"; // [classic_xo:X and O, animals:Star and heart]

// Piece thickness in mm
piece_thickness = 5; // [3:0.5:8]

// Fit clearance in mm per side, for the pieces in the wells and the lid in its grooves
clearance = 0.4; // [0.2:0.05:0.8]

/* [Colors] */

// Board (and box) colour (extruder 1)
board_color = "#4FC3F7"; // color

// Inlaid grid lines (extruder 2)
grid_color = "#FFFFFF"; // color

// X pieces, or stars (extruder 3)
x_color = "#E53935"; // color

// O pieces, or hearts (extruder 4)
o_color = "#FDD835"; // color

/* [Hidden] */

$fn = 64;

// Grid inlay depth, and the wall between a well and a grid line.
inlay = 1;
well_gap = 2;
// Board corner radius.
corner = 8;
// Box: wall and floor thickness, corner radius.
wall = 4;
floor_t = 2;
box_corner = 4;
// Lid dovetail: how far the flange reaches into each wall, and the vertical
// land under its 45-degree face (so the flange has no knife edge).
flange = 1.5;
land = 0.6;
// Gap between the pieces printed on the plate.
piece_gap = 4;
part_gap = 8;
// Corner rounding on the pieces.
piece_round = 1.2;
// Put the lid in the box instead of beside it (for checking the fit).
assembled = false;
lid_lift = 0;  // raise the assembled lid (for checking it is captive)

S = size;
T = thickness;
cl = clearance;
is_box = style == "box_with_storage";
well_depth = min(2, T - 1.2);

// Lid: its core fits between the walls with `cl` each side; the flange under
// it reaches `flange` into each wall. It runs from the box front to `cl`
// short of the back wall.
lid_w = S - 2 * wall - 2 * cl;
lid_l = S - wall - cl;

// The square play area and everything derived from it.
play = is_box ? lid_w : S;
bm = max(4, 0.06 * play);                    // margin round the grid
cell = (play - 2 * bm) / 3;
line_w = max(2, 0.03 * play);
well_d = cell - line_w - 2 * well_gap;
piece_d = well_d - 2 * cl;

// Box inside: pieces stack two deep.
inner_h = 2 * piece_thickness + 1.5;
lid_z = floor_t + inner_h;                   // where the lid's underside sits
box_h = lid_z + T;

// ---------------------------------------------------------------- board face

module rrect(w, h, r) {
    rr = min(r, w / 2 - 0.01, h / 2 - 0.01);
    offset(r = rr) square([w - 2 * rr, h - 2 * rr], center = true);
}

module stadium(len, w) {
    hull() for (sx = [-1, 1]) translate([sx * (len - w) / 2, 0]) circle(d = w);
}

// Four grid lines, round-ended, spanning the three cells.
module grid_2d() {
    for (i = [-1, 1]) {
        translate([0, i * cell / 2]) stadium(3 * cell, line_w);
        translate([i * cell / 2, 0]) rotate(90) stadium(3 * cell, line_w);
    }
}

module wells_2d() {
    for (i = [-1:1], j = [-1:1]) translate([i * cell, j * cell]) circle(d = well_d);
}

// The board's top detail cut into a slab whose top is at z = top.
module face_cuts(top) {
    translate([0, 0, top - well_depth]) linear_extrude(well_depth + 1) wells_2d();
    translate([0, 0, top - inlay]) linear_extrude(inlay + 1) grid_2d();
}

module grid_inlay(top) {
    translate([0, 0, top - inlay]) linear_extrude(inlay) grid_2d();
}

// ---------------------------------------------------------------- pieces

module rounded() { offset(r = piece_round) offset(delta = -piece_round) children(); }

module x_2d() {
    rounded() for (a = [45, -45]) rotate(a) stadium(piece_d, 0.26 * piece_d);
}

module o_2d() {
    difference() { circle(d = piece_d); circle(d = 0.5 * piece_d); }
}

module star_2d() {
    R = piece_d / 2;
    rounded() polygon([for (i = [0:9]) let(r = i % 2 ? 0.5 * R : R, a = 90 + i * 36)
                           [r * cos(a), r * sin(a)]]);
}

module heart_2d() {
    d = piece_d;
    intersection() {
        union() for (sx = [-1, 1]) hull() {
            translate([sx * 0.2 * d, 0.1 * d]) circle(r = 0.27 * d);
            translate([0, -0.36 * d]) circle(r = 0.08 * d);
        }
        circle(d = d);
    }
}

module piece_a_2d() { if (piece_style == "animals") star_2d(); else x_2d(); }
module piece_b_2d() { if (piece_style == "animals") heart_2d(); else o_2d(); }

// ---------------------------------------------------------------- box and lid

// Lid cross-section in the XZ plane (x across the box, y here = height),
// grown by `grow` on every side. Its top is extended by `top_extra`.
module lid_profile(grow = 0, top_extra = 0) {
    offset(delta = grow) polygon([
        [-lid_w / 2 - flange, 0], [lid_w / 2 + flange, 0],
        [lid_w / 2 + flange, land], [lid_w / 2, land + flange],
        [lid_w / 2, T + top_extra], [-lid_w / 2, T + top_extra],
        [-lid_w / 2, land + flange], [-lid_w / 2 - flange, land]]);
}

// Extrude an XZ profile along +y from y = y0 for length l.
module along_y(y0, l) {
    translate([0, y0, 0]) rotate([90, 0, 0]) mirror([0, 0, 1]) linear_extrude(l) children();
}

module box() {
    difference() {
        linear_extrude(box_h) rrect(S, S, box_corner);
        // Storage well.
        translate([0, 0, floor_t]) linear_extrude(inner_h + 0.01)
            rrect(S - 2 * wall, S - 2 * wall, 2);
        // Lid slot, open through the front wall. Its floor is at lid_z, so
        // the lid rests on the groove floors with its face flush with the rim.
        translate([0, 0, lid_z + cl]) along_y(-S / 2 - 1, S - wall + 1)
            lid_profile(grow = cl, top_extra = 1);
    }
}

// The lid face up, centred on the origin.
module lid_body() { along_y(-lid_l / 2, lid_l) lid_profile(); }

// ---------------------------------------------------------------- plate

board_ymin = -S / 2;
// The box and lid side by side must fit the H2C's 300 mm (both nozzles): at
// size 150 that takes the gap between them down to about 6 mm.
bed_w = 300;
lid_gap = max(3, min(part_gap, bed_w - (S + lid_w + 2 * flange)));
lid_x = S + lid_gap - (S - lid_w - 2 * flange) / 2;

if (is_box) {
    color(board_color) box();
    translate(assembled ? [0, -S / 2 + lid_l / 2, lid_z + lid_lift] : [lid_x, 0, 0]) {
        color(board_color) difference() { lid_body(); face_cuts(T); }
        color(grid_color) grid_inlay(T);
    }
} else {
    color(board_color) difference() {
        linear_extrude(T) rrect(S, S, corner);
        face_cuts(T);
    }
    color(grid_color) grid_inlay(T);
}

// Two rows of five pieces in front of the board.
pitch = piece_d + piece_gap;
row0_y = board_ymin - part_gap - piece_d / 2;
for (i = [0:4]) {
    x = -S / 2 + piece_d / 2 + i * pitch;
    color(x_color) translate([x, row0_y, 0]) linear_extrude(piece_thickness) piece_a_2d();
    color(o_color) translate([x, row0_y - pitch, 0]) linear_extrude(piece_thickness) piece_b_2d();
}

plate_w = max(is_box ? lid_x + lid_w / 2 + flange + S / 2 : S, 5 * pitch - piece_gap);
if (plate_w > bed_w)
    echo(str("NOTE: the plate is ", round(plate_w), " mm wide, more than the ", bed_w, " mm bed; print the lid on a second plate"));
