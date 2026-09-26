// Parametric hinge — two leaves with alternating knuckles, printed flat and
// opened 180 degrees. The pin axis runs along Y at the top of the knuckles;
// leaf 1 lies to the left of it, leaf 2 to the right.
//
// Pin types:
//   print_in_place  leaf 2's knuckles carry 45-degree cones that sit in
//                   matching sockets in leaf 1's knuckles, `clearance` apart
//                   everywhere. The hinge comes off the plate assembled; 45
//                   degree cones print without support in this orientation.
//   separate_pin    a bore through every knuckle, and a headed pin laid on
//                   the plate beside the hinge (flat on one side so it prints
//                   lying down).
//   filament_pin    a bore for a length of 1.75 mm filament; pin_d is ignored.
//
// Written to the MakerWorld Parametric Model Maker customizer conventions so
// the same file works unchanged on MakerWorld and in ScadBuddy.
//
// The colour parameters are the extruder order: color1 (leaf 1, and the
// separate pin) is extruder 1, color2 (leaf 2) is extruder 2. They are equal
// by default, which makes the hinge a single part.

/* [Leaf] */

// Width of each leaf, from the pin axis to its outer edge
leaf_w = 30; // [15:1:80]

// Length of the hinge along the pin axis
leaf_l = 40; // [20:5:150]

// Leaf thickness
thickness = 3; // [2:0.5:6]

// Screw holes per leaf
screw_holes = 2; // [0:1:4]

// Countersink the screw holes (on the upward face as printed)
countersink = true;

// Outline of each leaf
leaf_shape = "rect"; // [rect:Rectangle, rounded:Rounded corners, tapered:Tapered]

/* [Knuckle] */

// Number of knuckles, alternating between the leaves (odd)
knuckles = 5; // [3:2:11]

// Pin diameter (cone base diameter for print-in-place)
pin_d = 3; // [2:0.5:6]

// Gap between parts that move against each other
clearance = 0.35; // [0.2:0.05:0.6]

// How the leaves are pinned together
pin_type = "print_in_place"; // [print_in_place:Print in place, separate_pin:Separate pin, filament_pin:1.75 mm filament]

/* [Colors] */

// Leaf 1 colour (extruder 1)
color1 = "#808080"; // color

// Leaf 2 colour (extruder 2)
color2 = "#808080"; // color

/* [Hidden] */

$fn = 64;

W = leaf_w;
L = leaf_l;
T = thickness;
c = clearance;

// Material around the bore or socket.
wall = 1.2;

// Knuckles shorter than this are not worth printing: drop to the largest odd
// count (at least 3) that keeps every knuckle at least this long.
min_knuckle = 4;
n = max(3, min(knuckles, 2 * floor((L / min_knuckle - 1) / 2) + 1));
pitch = L / n;
kl = pitch - c;                       // length of an inner knuckle

filament = pin_type == "filament_pin";
bore_d = filament ? 1.75 + c : pin_d + 2 * c;

// Knuckle diameter: enough wall round the bore, and at least twice the leaf
// thickness so the leaves can fold flat onto each other.
D = max(2 * T, bore_d + 2 * wall);
zc = D / 2;                           // pin axis height

// Print-in-place cones: 45 degrees, so a clearance c normal to the surface
// is an axial shift of s. Sized so both sockets fit in one knuckle.
s = c * sqrt(2);
rb = min(pin_d / 2, (kl - 1) / 2 + c - s);

// Screw holes: #6 / M3.5 wood screw.
hole_d = 3.6;
free_w = W - D / 2 - c;               // leaf width clear of the knuckles
head_d = min(7.2, free_w - 1.5);
hole_u = (D / 2 + c + W) / 2;         // distance of the holes from the axis

// Separate pin: head, and the flat that lets it print lying down.
pin_head_d = min(D, pin_d + 2);
pin_head_l = 1.5;
pin_flat = 0.15 * pin_d;
pin_gap = 5;                          // from the hinge to the pin on the plate

// Knuckle i spans [k0(i), k1(i)] along Y; even ones are leaf 1's.
function k0(i) = i == 0 ? 0 : i * pitch + c / 2;
function k1(i) = i == n - 1 ? L : (i + 1) * pitch - c / 2;
function mine(i, leaf) = (i % 2 == 0) == (leaf == 1);

// Y extent of a leaf at distance u from the axis.
function span_lo(u) = leaf_shape == "tapered" ? 0.2 * L * u / W : 0;

module leaf_2d() {
    if (leaf_shape == "tapered") {
        polygon([[0, 0], [W, 0.2 * L], [W, 0.8 * L], [0, L]]);
    } else if (leaf_shape == "rounded") {
        r = min(6, W / 3, L / 4);
        hull() {
            square([0.01, L]);
            for (y = [r, L - r]) translate([W - r, y]) circle(r = r);
        }
    } else {
        square([W, L]);
    }
}

// Cone with its base centred on the axis at y = y0, radius r, pointing along
// dir (+1 / -1) at 45 degrees.
module cone_y(y0, r, dir) {
    translate([0, y0, zc]) rotate([dir > 0 ? -90 : 90, 0, 0]) cylinder(r1 = r, r2 = 0, h = r);
}

module rod_y(d, y0, y1) {
    translate([0, y0, zc]) rotate([-90, 0, 0]) cylinder(d = d, h = y1 - y0);
}

module screw_holes() {
    span = L - 2 * span_lo(hole_u);
    count = free_w < hole_d + 2 ? 0 : min(screw_holes, floor(span / (head_d + 2)));
    for (k = [0 : 1 : count - 1]) {
        y = span_lo(hole_u) + span * (k + 0.5) / count;
        translate([hole_u, y, 0]) {
            translate([0, 0, -1]) cylinder(d = hole_d, h = T + 2);
            if (countersink) {
                ch = (head_d - hole_d) / 2;
                translate([0, 0, T - ch]) cylinder(d1 = hole_d, d2 = head_d, h = ch + 0.01);
                translate([0, 0, T]) cylinder(d = head_d, h = 1);
            }
        }
    }
}

// One leaf, drawn on the +X side of the axis. Leaf 1 is mirrored to -X.
module leaf(which) {
    difference() {
        union() {
            difference() {
                linear_extrude(height = T) leaf_2d();
                // Room for the other leaf's knuckles to turn.
                for (i = [0 : n - 1]) if (!mine(i, which))
                    rod_y(D + 2 * c, k0(i) - c, k1(i) + c);
                screw_holes();
            }
            for (i = [0 : n - 1]) if (mine(i, which)) {
                rod_y(D, k0(i), k1(i));
                if (pin_type == "print_in_place" && which == 2) {
                    cone_y(k0(i) + 0.01, rb + 0.01, -1);
                    cone_y(k1(i) - 0.01, rb + 0.01, +1);
                }
            }
        }
        if (pin_type == "print_in_place" && which == 1) {
            for (i = [0 : n - 1]) if (mine(i, 1)) {
                e = 0.01;
                rs = rb - c + s + e;
                if (i > 0) cone_y(k0(i) - e, rs, +1);
                if (i < n - 1) cone_y(k1(i) + e, rs, -1);
            }
        } else if (pin_type != "print_in_place") {
            rod_y(bore_d, -1, L + 1);
        }
    }
}

// Headed pin lying along Y, with a flat underside.
module loose_pin() {
    zp = pin_d / 2 - pin_flat;
    translate([W + pin_gap + pin_head_d / 2, 0, 0])
        intersection() {
            translate([0, 0, zp]) rotate([-90, 0, 0]) {
                cylinder(d = pin_head_d, h = pin_head_l);
                cylinder(d = pin_d, h = pin_head_l + L + 0.5);
            }
            translate([-pin_head_d, -1, 0]) cube([2 * pin_head_d, L + 4, pin_d + 2]);
        }
}

color(color1) {
    mirror([1, 0, 0]) leaf(1);
    if (pin_type == "separate_pin") loose_pin();
}

color(color2) leaf(2);
