// Gridfinity bin — a self-contained, spec-accurate Gridfinity storage bin with
// optional stacking lip, magnet and screw holes, dividers, a label tab with an
// inlaid label, a finger scoop, and an "ultralight" hollow-foot floor. No
// libraries: every dimension is written out below and cited in the README.
//
// Written to the MakerWorld Parametric Model Maker customizer conventions so
// the same file works unchanged on MakerWorld and in ScadBuddy.
//
// The two colour parameters are the extruder order: bin_color is extruder 1,
// label_color is extruder 2. The label part only exists when label_text is not
// empty and label_tab is not "none"; otherwise the bin is a single colour.

/* [Size] */

// Width in grid units (42 mm each; 7 units is 293.5 mm, the most that fits the H2C bed)
units_x = 2; // [1:1:7]

// Depth in grid units (42 mm each)
units_y = 1; // [1:1:7]

// Height in 7 mm units, measured from the bottom of the feet to the top of the wall (the stacking lip adds to this)
height_units = 3; // [2:1:12]

/* [Features] */

// Stacking lip on top, so another bin can sit on this one
stacking_lip = true;

// 6.5 x 2.4 mm magnet holes in the four corners of every foot
magnet_holes = false;

// 3 mm screw holes in the four corners of every foot
screw_holes = false;

// Number of compartments across the width
divisions_x = 1; // [1:1:8]

// Number of compartments front to back
divisions_y = 1; // [1:1:8]

// Label tab along the back of each compartment
label_tab = "full"; // [none:None, left:Left 42 mm of each compartment, full:Full width]

// Finger scoop along the front inner wall of each compartment
scoop = true;

// Outer wall thickness in mm
wall = 1.2; // [0.8:0.1:2.4]

// Floor style: solid feet, or hollow feet with a thin skin (uses much less filament)
floor_style = "solid"; // [solid:Solid, ultralight:Ultralight (hollow feet)]

/* [Label] */

// Text inlaid into the back-left label tab (leave empty for no label; needs a label tab)
label_text = ""; // 24

// Label letter height in mm (a label too long for the tab is shrunk to fit)
label_size = 6; // [3:0.5:10]

// Label typeface
font = "DejaVu Sans:style=Bold"; // font

/* [Colors] */

// Bin colour (extruder 1)
bin_color = "#8E9089"; // color

// Label colour (extruder 2)
label_color = "#F2F2F2"; // color

/* [Hidden] */

$fn = 48;

// ---- Gridfinity constants (see README for sources) ----
PITCH = 42;               // grid pitch
GAP = 0.5;                // a bin is 0.5 mm smaller than its grid footprint
R = 3.75;                 // outer corner radius (7.5 / 2)
HU = 7;                   // height unit
BASE_H = 7;               // foot profile + bridge
// Foot profile from the bottom: 0.8 at 45 deg, 1.8 vertical, 2.15 at 45 deg.
P1 = 0.8; P2 = 1.8; P3 = 2.15;
BASE_PROFILE_H = P1 + P2 + P3;      // 4.75
BASE_INSET = P1 + P3;               // 2.95: bottom of a foot is 35.6 mm square
// Stacking lip, from the inner tip up: 0.7 at 45 deg, 1.8 vertical, 1.9 at 45 deg.
L1 = 0.7; L2 = 1.8; L3 = 1.9;
LIP_D = L1 + L3;                    // 2.6 deep
LIP_SUPPORT = 1.2;                  // vertical face under the inner tip
LIP_FILLET = 0.6;                   // rounds the knife edge on top
MAGNET_D = 6.5; MAGNET_DEPTH = 2.4;
SCREW_D = 3; SCREW_DEPTH = 6;
HOLE_OFFSET = 13;                   // holes at +/-13 mm from each cell centre
DIVIDER = 1.2;
TAB_DEPTH = 15.85; TAB_ANGLE = 36; TAB_LEDGE = 1.2; TAB_MAX_W = 42;
LABEL_DEPTH = 0.6;                  // inlay depth: three 0.2 mm layers
UL_SKIN = 1.0;                      // ultralight: bottom skin of each foot
EPS = 0.01;

// ---- Derived ----
W = [units_x * PITCH - GAP, units_y * PITCH - GAP];
H = height_units * HU;
ultralight = floor_style == "ultralight";
floor_z = ultralight ? UL_SKIN : BASE_H;
// Everything inside the bin stays below the feet of a bin stacked on top.
top_z = stacking_lip ? H - LIP_SUPPORT : H;
inner = W - 2 * [wall, wall];
nx = max(1, round(divisions_x));
ny = max(1, round(divisions_y));
cw = (inner.x - (nx - 1) * DIVIDER) / nx;        // compartment width
cd = (inner.y - (ny - 1) * DIVIDER) / ny;        // compartment depth
has_tab = label_tab != "none";
tab_d = min(TAB_DEPTH, cd / 2);
tab_on = has_tab && tab_d >= 4;
tab_w = label_tab == "left" ? min(TAB_MAX_W, cw) : cw;
scoop_r = min((top_z - floor_z) / 2, (tab_on ? cd - tab_d : cd) * 0.45);
has_label = tab_on && label_text != "";
function cx0(i) = -inner.x / 2 + i * (cw + DIVIDER);   // left edge of column i
function cy0(j) = -inner.y / 2 + j * (cd + DIVIDER);   // front edge of row j

// ---- Rounded-rectangle frustum as a polyhedron (exact and cheap) ----
// Ring of a rounded rectangle `size` inset by `ins`, corner radius R - ins.
SEG = max(2, ceil($fn / 4));
function rr_ring(size, ins, z) =
    let(r = max(R - ins, 0.05),
        hx = size.x / 2 - ins - r, hy = size.y / 2 - ins - r,
        c = [[hx, hy], [-hx, hy], [-hx, -hy], [hx, -hy]])
    [for (k = [0:3]) for (s = [0:SEG])
        let(a = 90 * k + 90 * s / SEG)
        [c[k].x + r * cos(a), c[k].y + r * sin(a), z]];

module frustum(size, ins0, z0, ins1, z1) {
    b = rr_ring(size, ins0, z0);
    t = rr_ring(size, ins1, z1);
    m = len(b);
    polyhedron(
        points = concat(b, t),
        faces = concat(
            [[for (i = [0:m - 1]) i]],
            [[for (i = [m - 1:-1:0]) m + i]],
            [for (i = [0:m - 1]) let(j = (i + 1) % m) [i, m + i, m + j, j]]));
}

module rrect(size, ins = 0) {
    r = max(R - ins, 0.05);
    offset(r = r) square(size - 2 * [ins + r, ins + r], center = true);
}

// ---- Foot (one per grid cell) ----
module each_cell() {
    for (i = [0:units_x - 1], j = [0:units_y - 1])
        translate([(i - (units_x - 1) / 2) * PITCH, (j - (units_y - 1) / 2) * PITCH, 0])
            children();
}

CELL = [PITCH - GAP, PITCH - GAP];
module foot() {
    frustum(CELL, BASE_INSET, 0, P3, P1);
    frustum(CELL, P3, P1, P3, P1 + P2);
    frustum(CELL, P3, P1 + P2, 0, BASE_PROFILE_H);
}

// Ultralight: the foot's profile offset inwards by `wall` (normal to each
// face), from a UL_SKIN bottom skin up into the main cavity.
module foot_cavity() {
    s = sqrt(2) * wall;          // horizontal offset of a 45 degree face
    k = s - wall;                // how far a corner moves up
    frustum(CELL, BASE_INSET - UL_SKIN + s, UL_SKIN, P3 + wall, P1 + k);
    frustum(CELL, P3 + wall, P1 + k, P3 + wall, P1 + P2 + k);
    frustum(CELL, P3 + wall, P1 + P2 + k, wall, BASE_PROFILE_H + k);
    frustum(CELL, wall, BASE_PROFILE_H + k, wall, BASE_H + EPS);
}

// Outside envelope of the bin without the lip.
module envelope() {
    each_cell() foot();
    translate([0, 0, BASE_PROFILE_H]) linear_extrude(H - BASE_PROFILE_H) rrect(W);
}

// ---- Stacking lip: the lip profile swept around the rounded rectangle ----
// Profile in (inset from outer edge, z relative to H): support chamfer, inner
// tip, 0.7 at 45 deg, 1.8 vertical, then the 1.9 at 45 deg filleted into the
// outer wall with r = 0.6.
function lip_profile() =
    let(cz = L1 + L2 + L3 - LIP_FILLET - LIP_FILLET * sqrt(2), // fillet centre height
        cx = LIP_D - LIP_FILLET,                     // ...and distance from the tip
        arc = [for (a = [135:-7.5:0]) [LIP_D - (cx + LIP_FILLET * cos(a)), cz + LIP_FILLET * sin(a)]])
    concat([[0, -(LIP_SUPPORT + LIP_D)], [LIP_D, -LIP_SUPPORT], [LIP_D, 0],
            [L3, L1], [L3, L1 + L2]], arc);

// Profile in (radius from a corner centre, absolute z) for rotate_extrude.
function lip_rz() = [for (p = lip_profile()) [R - p.x, H + p.y]];

module lip() {
    a = W.x / 2 - R; b = W.y / 2 - R;
    for (q = [[a, b, 0], [-a, b, 90], [-a, -b, 180], [a, -b, 270]])
        translate([q.x, q.y, 0]) rotate(q.z) rotate_extrude(angle = 90) polygon(lip_rz());
    for (s = [[a, 2 * b, 0], [b, 2 * a, 90], [a, 2 * b, 180], [b, 2 * a, 270]])
        rotate(s.z) translate([s.x, 0, 0]) rotate([90, 0, 0])
            linear_extrude(s.y + 2 * EPS, center = true) polygon(lip_rz());
}

// ---- Interior features ----
module dividers() {
    for (i = [1:1:nx - 1])
        translate([cx0(i) - DIVIDER, -inner.y / 2, 0]) cube([DIVIDER, inner.y, top_z]);
    for (j = [1:1:ny - 1])
        translate([-inner.x / 2, cy0(j) - DIVIDER, 0]) cube([inner.x, DIVIDER, top_z]);
}

// Tab against the back of compartment (i, j): flat top, 36 degree underside.
module tab(i, j) {
    yb = cy0(j) + cd;
    h = tan(TAB_ANGLE) * tab_d + TAB_LEDGE;
    // 0.1 mm overlap into the neighbouring divider or wall, so the parts fuse
    // instead of meeting on a coplanar face.
    translate([cx0(i) - 0.1, yb + 0.1, 0]) rotate([90, 0, 90]) linear_extrude(tab_w + 0.2)
        polygon([[0, top_z - h], [0, top_z], [-tab_d - 0.1, top_z], [-tab_d - 0.1, top_z - TAB_LEDGE]]);
}

// Solid under a concave quarter-circle along the front of row j.
module scoop_fill(j) {
    yf = cy0(j);
    translate([-inner.x / 2, yf, floor_z]) rotate([90, 0, 90]) linear_extrude(inner.x)
        difference() {
            translate([0, -floor_z]) square([scoop_r, scoop_r + floor_z]);
            translate([scoop_r, scoop_r]) circle(r = scoop_r);
        }
}

module holes() {
    each_cell() for (sx = [-1, 1], sy = [-1, 1]) translate([sx * HOLE_OFFSET, sy * HOLE_OFFSET, 0]) {
        if (magnet_holes) translate([0, 0, -EPS]) cylinder(d = MAGNET_D, h = MAGNET_DEPTH + EPS);
        if (screw_holes) translate([0, 0, -EPS]) cylinder(d = SCREW_D, h = SCREW_DEPTH + EPS);
    }
}

// Ultralight feet are hollow, so each hole gets a solid pillar to sit in.
module hole_pillars() {
    depth = screw_holes ? SCREW_DEPTH : MAGNET_DEPTH;
    d = (magnet_holes ? MAGNET_D : SCREW_D) + 2 * wall;
    each_cell() for (sx = [-1, 1], sy = [-1, 1])
        translate([sx * HOLE_OFFSET, sy * HOLE_OFFSET, 0]) cylinder(d = d, h = depth + 0.8);
}

// Shrink-only fit: resize() the children together with their mirror image
// and a hair-thin bar of width w, all on separate layers, then cut out the
// children's layer. Anything whose ink stays within +/- w/2 is left alone;
// anything wider is scaled down uniformly until it does.
module fit_x(w) {
    projection(cut = true) translate([0, 0, -0.5])
        resize([w, 0, 0], auto = [false, true, false])
            union() {
                linear_extrude(1) children();
                translate([0, 0, 5]) linear_extrude(1) mirror([1, 0]) children();
                translate([-w / 2, 0, 10]) cube([w, 0.01, 0.01]);
            }
}

// Label inlay: the text, centred on the flat top of the back-left tab and
// shrunk to fit its width.
module label_2d() {
    yb = inner.y / 2;
    back_clear = stacking_lip ? LIP_D - wall + 0.5 : 1;
    x0 = cx0(0) + 1; x1 = cx0(0) + tab_w - 1;
    y0 = yb - tab_d + 1; y1 = yb - back_clear;
    translate([(x0 + x1) / 2, (y0 + y1) / 2])
        fit_x(x1 - x0)
            text(label_text, size = min(label_size, (y1 - y0) * 0.7),
                 font = font, halign = "center", valign = "center");
}

module label_prism() {
    translate([0, 0, top_z - LABEL_DEPTH]) linear_extrude(LABEL_DEPTH) label_2d();
}

module pocket() {
    translate([0, 0, top_z - LABEL_DEPTH]) linear_extrude(LABEL_DEPTH + 1) label_2d();
}

// ---- Assembly ----
color(bin_color) difference() {
    union() {
        difference() {
            envelope();
            translate([0, 0, BASE_H])
                linear_extrude(H) rrect(W, wall);
            if (ultralight) each_cell() foot_cavity();
        }
        if (stacking_lip) lip();
        intersection() {
            envelope();
            union() {
                if (nx > 1 || ny > 1) dividers();
                if (tab_on) for (i = [0:nx - 1], j = [0:ny - 1]) tab(i, j);
                if (scoop && scoop_r >= 1) for (j = [0:ny - 1]) scoop_fill(j);
                if (ultralight && (magnet_holes || screw_holes)) hole_pillars();
            }
        }
    }
    if (magnet_holes || screw_holes) holes();
    if (has_label) pocket();
}

if (has_label)
    color(label_color) label_prism();
