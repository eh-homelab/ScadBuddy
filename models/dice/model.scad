// Custom dice — a six-sided die with rounded edges and its faces inlaid in a
// second colour: standard pips, numbers, your own words (an activity die:
// HOP, JUMP, SPIN ...) or six simple picture shapes.
//
// Faces are flush inlays: each face pattern is cut into the die and filled
// with the face colour, so nothing stands proud and nothing needs supports.
// Face 1 is on top and face 6 on the bed (its inlay prints as the first
// layers); 2 faces the front, 5 the back, 3 the right, 4 the left, so
// opposite faces add up to 7.
//
// Written to the MakerWorld Parametric Model Maker customizer conventions so
// the same file works unchanged on MakerWorld and in ScadBuddy.
//
// The two colour parameters are the extruder order: die_color is extruder 1,
// face_color is extruder 2.

/* [Die] */

// Edge length of the die in mm
size = 20; // [12:1:40]

// Radius of the rounded edges in mm (capped at a fifth of the size); the face artwork shrinks to stay on the flat
rounding = 2; // [0:0.5:5]

// What goes on the faces
faces = "pips"; // [pips:Pips, numbers:Numbers, custom_text:Custom words, emoji_shapes:Picture shapes]

// Typeface for numbers and words
font = "DejaVu Sans:style=Bold"; // font

// Depth of the face inlays in mm
inlay_depth = 0.8; // [0.4:0.2:2]

/* [Custom words] */

// Face 1 word (top). Used only when faces is Custom words; empty leaves the face blank
face_1 = "HOP"; // 8

// Face 2 word (front)
face_2 = "JUMP"; // 8

// Face 3 word (right)
face_3 = "SPIN"; // 8

// Face 4 word (left)
face_4 = "CLAP"; // 8

// Face 5 word (back)
face_5 = "SING"; // 8

// Face 6 word (bottom)
face_6 = "DANCE"; // 8

/* [Batch] */

// How many dice to put on the plate
count = 1; // [1:1:6]

/* [Colours] */

// Die body (extruder 1)
die_color = "#FFF3E0"; // color

// Pips, numbers, words and shapes (extruder 2)
face_color = "#D81B60"; // color

/* [Hidden] */

$fn = 48;

part_gap = 6;                       // gap between dice on the plate
r = min(rounding, size / 5);        // effective edge radius
// Artwork is drawn for a 0.72 x size box and shrunk, when the rounding eats
// into the flat of the face, so it never wraps over a rounded edge.
art_fit = min(1, 0.92 * (size - 2 * r) / (0.72 * size));
content = size * 0.72 * art_fit;    // face artwork box
pip_step = size * 0.25 * art_fit;   // pip grid spacing
pip_d = size * 0.17 * art_fit;      // pip diameter
words = [face_1, face_2, face_3, face_4, face_5, face_6];
shapes = ["heart", "star", "moon", "sun", "cloud", "lightning"];

// ------------------------------------------------------------ body

module rounded_square(w) {
    if (r > 0) offset(r = r) square(w - 2 * r, center = true);
    else square(w, center = true);
}

// Intersection of three rounded-square prisms: every edge is rounded, the
// outside is exactly size x size x size, and it is quick to render.
module body() {
    translate([0, 0, size / 2])
        intersection() {
            linear_extrude(size, center = true) rounded_square(size);
            rotate([90, 0, 0]) linear_extrude(size, center = true) rounded_square(size);
            rotate([0, 90, 0]) linear_extrude(size, center = true) rounded_square(size);
        }
}

// ------------------------------------------------------------ face artwork

// Shrink-only fit (as in name-sign): resize() to the box of the children
// plus a hair-thin bar of the target length, so small text is left alone and
// long text is scaled down uniformly.
module fit_x(w) {
    projection(cut = true) translate([0, 0, -0.5])
        resize([w, 0, 0], auto = [false, true, false])
            union() {
                linear_extrude(1) children();
                translate([-w / 2, 0, 10]) cube([w, 0.01, 0.01]);
            }
}

module word_2d(s) {
    if (len(s) > 0)
        fit_x(content)
            text(s, size = size * 0.26 * art_fit, font = font, halign = "center", valign = "center");
}

module number_2d(k) scale(art_fit) {
    text(str(k), size = size * 0.5, font = font, halign = "center", valign = "center");
    // Underline the 6 so it cannot be read as a 9.
    if (k == 6) translate([0, -size * 0.34]) offset(r = size * 0.02)
        square([size * 0.26, size * 0.03], center = true);
}

function pips(k) =
      k == 1 ? [[0, 0]]
    : k == 2 ? [[-1, 1], [1, -1]]
    : k == 3 ? [[-1, 1], [0, 0], [1, -1]]
    : k == 4 ? [[-1, -1], [-1, 1], [1, -1], [1, 1]]
    : k == 5 ? [[-1, -1], [-1, 1], [1, -1], [1, 1], [0, 0]]
    :          [[-1, -1], [-1, 0], [-1, 1], [1, -1], [1, 0], [1, 1]];

module pips_2d(k) {
    for (p = pips(k)) translate(p * pip_step) circle(d = pip_d);
}

// Picture shapes, drawn in a unit box and scaled to the artwork box. All
// convex corners are rounded.
module heart_2d() {
    for (m = [0, 1]) mirror([m, 0])
        hull() {
            translate([-0.23, 0.17]) circle(r = 0.27);
            translate([0, -0.42]) circle(r = 0.05);
        }
}

module star_2d() {
    offset(r = 0.06) offset(delta = -0.06)
        polygon([for (i = [0 : 9]) let(a = 90 + i * 36, rr = i % 2 == 0 ? 0.53 : 0.25)
                 [rr * cos(a), rr * sin(a) - 0.03]]);
}

module moon_2d() {
    offset(r = 0.05) offset(delta = -0.05)
        difference() {
            circle(r = 0.48);
            translate([0.24, 0.14]) circle(r = 0.4);
        }
}

module sun_2d() {
    circle(r = 0.25);
    for (a = [0 : 45 : 315]) rotate(a)
        hull() {
            translate([0.34, 0]) circle(d = 0.1);
            translate([0.46, 0]) circle(d = 0.1);
        }
}

module cloud_2d() {
    translate([0, -0.08]) {
        translate([-0.24, 0]) circle(r = 0.2);
        translate([0.02, 0.12]) circle(r = 0.26);
        translate([0.26, 0.01]) circle(r = 0.19);
        hull() {
            translate([-0.34, -0.06]) circle(r = 0.12);
            translate([0.36, -0.06]) circle(r = 0.12);
        }
    }
}

module lightning_2d() {
    scale([1.35, 1]) offset(r = 0.04) offset(delta = -0.04)
        polygon([[0.14, 0.5], [-0.3, -0.02], [-0.02, -0.02], [-0.14, -0.5],
                 [0.32, 0.1], [0.04, 0.1]]);
}

module shape_2d(k) {
    n = shapes[k - 1];
    scale(content)
        if (n == "heart") heart_2d();
        else if (n == "star") star_2d();
        else if (n == "moon") moon_2d();
        else if (n == "sun") sun_2d();
        else if (n == "cloud") cloud_2d();
        else lightning_2d();
}

module art_2d(k) {
    if (faces == "pips") pips_2d(k);
    else if (faces == "numbers") number_2d(k);
    else if (faces == "custom_text") word_2d(words[k - 1]);
    else shape_2d(k);
}

function has_art(k) = faces != "custom_text" || len(words[k - 1]) > 0;

// ------------------------------------------------------------ faces

// Place 2D artwork on face k, reading upright from outside the die, as a
// prism starting inlay_depth under the surface and running well outside it.
module on_face(k) {
    e = inlay_depth + size;
    translate([0, 0, size / 2])
        if (k == 1) translate([0, 0, size / 2 - inlay_depth]) linear_extrude(e) children();
        else if (k == 6) translate([0, 0, -size / 2 + inlay_depth]) mirror([0, 0, 1])
                             mirror([1, 0, 0]) linear_extrude(e) children();
        else rotate([90, 0, k == 2 ? 0 : k == 5 ? 180 : k == 3 ? 90 : -90])
                 translate([0, 0, size / 2 - inlay_depth]) linear_extrude(e) children();
}

module inlays() {
    intersection() {
        body();
        union() for (k = [1 : 6]) if (has_art(k)) on_face(k) art_2d(k);
    }
}

module die() {
    color(die_color)
        difference() {
            body();
            for (k = [1 : 6]) if (has_art(k)) on_face(k) art_2d(k);
        }
    if (len([for (k = [1 : 6]) if (has_art(k)) k]) > 0)
        color(face_color) inlays();
}

// Up to three in a row, then a second row.
cols = min(count, 3);
for (i = [0 : count - 1])
    translate([(i % cols) * (size + part_gap), floor(i / cols) * (size + part_gap), 0])
        translate([size / 2, size / 2, 0]) die();
