// Building brick — a parametric stud-and-tube brick that clicks onto the
// standard 8 mm-pitch system bricks, or (system = big) onto the 16 mm-pitch
// toddler bricks. Bricks, plates, smooth tiles and 45-degree slopes, from 1x1
// up to 16x16 studs, with an optional word inlaid in a tile's top or a brick's
// front face.
//
// It prints the way the real ones are moulded: studs up, open underside down.
// The roof bridges between the walls and the underside tubes (at most one stud
// pitch apart), the slope's underside is a 45-degree overhang, and the studs
// and tubes are plain vertical cylinders, so nothing needs supports. Printing
// it studs-down would put the studs on the bed (elephant's foot on exactly the
// surface that has to fit) and leave the whole open cavity hanging.
//
// Dimensions (see README for sources): 8.0 mm pitch, 0.1 mm play per side,
// stud 4.8 x 1.7, brick 9.6, plate 3.2, wall 1.2 with ribs to the stud, tube
// OD 6.51 / ID 4.8. The big system is the 16 mm pitch, 19.2 mm brick with the
// measured Ø9.4 x 4.5 hollow stud.
//
// Written to the MakerWorld Parametric Model Maker customizer conventions so
// the same file works unchanged on MakerWorld and in ScadBuddy.
//
// The colour parameters are the extruder order: brick_color is extruder 1,
// text_color extruder 2 (only used when there is text).

/* [Brick] */

// Brick system: standard 8 mm studs, or the big 16 mm toddler bricks
system = "standard"; // [standard:Standard (8 mm pitch), big:Big (16 mm pitch)]

// Piece type. Slope drops 45 degrees across the last row on the +X end
type = "brick"; // [brick:Brick, plate:Plate, tile:Tile (smooth top), slope:Slope 45°]

// Length in studs
studs_x = 4; // [1:1:16]

// Width in studs
studs_y = 2; // [1:1:16]

/* [Fit] */

// Added to the stud diameter, mm. Raise it if printed studs are loose in other bricks
stud_fit = 0; // [-0.2:0.02:0.2]

// Added to the walls, ribs and tubes the studs below press against, mm. Raise it if the brick falls off
wall_fit = 0; // [-0.2:0.02:0.2]

/* [Decor] */

// Word inlaid in a tile's top, or in the front face of a brick, plate or slope (left out where there is no room: a standard plate's face, a 1-stud slope)
top_text = ""; // 20

// Typeface
font = "DejaVu Sans:style=Bold"; // font

/* [Colors] */

// Brick colour (extruder 1)
brick_color = "#E53935"; // color

// Text colour (extruder 2)
text_color = "#FFFFFF"; // color

/* [Hidden] */

$fn = 48;

BIG = system == "big";
S = BIG ? 2 : 1;

PITCH = 8 * S;
PLAY = 0.1;                         // gap each side, so neighbours don't bind
STUD_D = BIG ? 9.4 : 4.8;
STUD_H = BIG ? 4.5 : 1.7;
STUD_HOLE = BIG ? 6.5 : 0;          // big studs are hollow
STUD_CHAMFER = 0.2 * S;
BRICK_H = 9.6 * S;
PLATE_H = BIG ? 9.6 : 3.2;          // the big system's flat pieces are half-height
WALL = BIG ? 1.6 : 1.2;
RIB_W = BIG ? 1.0 : 0.7;
ROOF = 1.0 * S;
TUBE_ID = 4.8 * S;

NX = studs_x;
NY = studs_y;
LX = NX * PITCH - 2 * PLAY;
LY = NY * PITCH - 2 * PLAY;
H = type == "brick" || type == "slope" ? BRICK_H : PLATE_H;

// Under a tile the roof is as thick as it can be while still clearing the
// studs of the piece below, so an inlaid word leaves a solid skin under it.
roof = type == "tile" ? H - STUD_H - 0.2 * S : ROOF;

// Where the underside grips a stud: rib tips, tube outside and 1-wide pins all
// touch a nominal stud of the piece below.
grip = PITCH / 2 - PLAY - STUD_D / 2 + wall_fit;     // from the outer face
tube_od = PITCH * sqrt(2) - STUD_D + 2 * wall_fit;   // 6.51 standard
pin_d = PITCH - STUD_D + 2 * wall_fit;               // 3.2 standard

// Slope: the last row on +X, 45 degrees, from the top down to the lip. A
// one-stud slope has play on both sides, so its run is the whole length.
slope = type == "slope";
run = min(PITCH - PLAY, LX);
xs = LX / 2 - run;

function row_x(i) = -NX * PITCH / 2 + (i + 0.5) * PITCH;
function row_y(j) = -NY * PITCH / 2 + (j + 0.5) * PITCH;

// ---------------------------------------------------------------- body

// Everything above the 45-degree plane through (xs, H), pushed down by `off`.
module slope_cut(off) {
    rotate([90, 0, 0]) linear_extrude(height = LY + 2, center = true)
        polygon([[xs - 0.001, H - off], [LX / 2 + 1, H - off - (LX / 2 + 1 - xs)],
                 [LX / 2 + 1, H + 20], [xs - 0.001, H + 20]]);
}

module outer() {
    difference() {
        translate([-LX / 2, -LY / 2, 0]) cube([LX, LY, H]);
        if (slope) slope_cut(0);
    }
}

module cavity() {
    difference() {
        translate([-LX / 2 + WALL, -LY / 2 + WALL, -1])
            cube([LX - 2 * WALL, LY - 2 * WALL, H - roof + 1]);
        if (slope) slope_cut(roof * sqrt(2));
    }
}

// Tubes, 1-wide pins and wall ribs, full cavity height.
module underside() {
    h = H - roof + 0.01;
    if (NX > 1 && NY > 1)
        for (i = [1 : NX - 1], j = [1 : NY - 1])
            translate([row_x(i) - PITCH / 2, row_y(j) - PITCH / 2, 0])
                difference() {
                    cylinder(d = tube_od, h = h);
                    translate([0, 0, -1]) cylinder(d = TUBE_ID, h = h + 2);
                }
    else if (NX > 1 || NY > 1)
        for (k = [1 : max(NX, NY) - 1])
            translate(NX > 1 ? [row_x(k) - PITCH / 2, 0, 0] : [0, row_y(k) - PITCH / 2, 0])
                cylinder(d = pin_d, h = h);
    if (grip > WALL + 0.01) {
        for (i = [0 : NX - 1], s = [-1, 1])
            translate([row_x(i) - RIB_W / 2, s > 0 ? LY / 2 - grip : -LY / 2, 0])
                cube([RIB_W, grip, h]);
        for (j = [0 : NY - 1], s = [-1, 1])
            translate([s > 0 ? LX / 2 - grip : -LX / 2, row_y(j) - RIB_W / 2, 0])
                cube([grip, RIB_W, h]);
    }
}

module stud() {
    d = STUD_D + stud_fit;
    difference() {
        union() {
            translate([0, 0, -0.01]) cylinder(d = d, h = STUD_H - STUD_CHAMFER + 0.01);
            translate([0, 0, STUD_H - STUD_CHAMFER])
                cylinder(d1 = d, d2 = d - 2 * STUD_CHAMFER, h = STUD_CHAMFER);
        }
        if (STUD_HOLE > 0) cylinder(d = STUD_HOLE, h = STUD_H + 1);
    }
}

module studs() {
    if (type != "tile")
        for (i = [0 : NX - 1], j = [0 : NY - 1])
            if (!(slope && i == NX - 1))
                translate([row_x(i), row_y(j), H]) stud();
}

module body() {
    difference() { outer(); cavity(); }
    intersection() { outer(); underside(); }
    studs();
}

// ---------------------------------------------------------------- text

has_text = len(top_text) > 0;
on_top = type == "tile";
margin = 1.0 * S;
depth = on_top ? 0.6 * S : WALL / 2;

// Box the word is fitted into: the tile's top, or the flat part of the front
// face (left of the slope on a slope).
box_x0 = -LX / 2 + margin;
box_x1 = (slope ? xs : LX / 2) - margin;
box_w = box_x1 - box_x0;
box_h = on_top ? LY - 2 * margin : H - 2 * margin;
text_fits = has_text && box_w >= 4 && box_h >= 2.5;

// Shrink-only fit, as in models/name-sign: resize() to the bounding box of the
// children plus a hair-thin bar of the target length, so text already smaller
// is left alone and larger text is scaled down uniformly.
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
            text(top_text, size = box_h * 0.7, font = font,
                 halign = "center", valign = "center");
        square([box_w, box_h], center = true);
    }
}

module pocket() {
    if (text_fits) {
        if (on_top)
            translate([(box_x0 + box_x1) / 2, 0, H - depth])
                linear_extrude(depth + 0.001) word_2d();
        else
            translate([(box_x0 + box_x1) / 2, -LY / 2 + depth, H / 2])
                rotate([90, 0, 0]) linear_extrude(depth + 0.001) word_2d();
    }
}

// ---------------------------------------------------------------- parts

color(brick_color) difference() { body(); pocket(); }
if (text_fits) color(text_color) intersection() { body(); pocket(); }
