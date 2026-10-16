// Tube squeezer — a bar with a narrow slot that the crimped end of a tube
// (toothpaste, lotion, paint, glue) is fed through, then slid along the tube
// to press the contents towards the cap.
//
// Printed flat: the slot runs straight down through the bar, so its walls are
// vertical and nothing needs support. The ends of the slot are rounded so the
// tube's folded edges are not cut by a corner.
//
// Styles:
//   closed    the slot is enclosed; thread the tube's flat end through it
//   open_end  the slot runs out through one end, so the squeezer slips on
//             from the side of a tube that is already half used
//
// The colour parameters are the extruder order: body_color is extruder 1,
// label_color extruder 2.

/* [Tube] */

// Width of the tube when flattened (measure the crimped end), mm
tube_width = 50; // [15:1:120]

// Slot gap, mm: about twice the tube's wall. Narrower squeezes harder
slot_gap = 1.6; // [0.6:0.1:5]

// Extra slot length beyond the tube's width, each end, mm
clearance = 1; // [0:0.5:5]

/* [Body] */

// Slot style
style = "closed"; // [closed:Closed slot, open_end:Open at one end]

// Height of the bar (how much of the tube it presses at once), mm
height = 10; // [5:1:25]

// Thickness of each jaw beside the slot, mm (stiffer when thicker)
jaw = 6; // [3:0.5:15]

// Length of the grip wing at each end, mm (0 for none)
wing = 15; // [0:1:40]

// Finger grooves across the top of each wing
grooves = true;

/* [Label] */

// Text inlaid flush into the top face, along one jaw. Empty for none
label = ""; // 16

// Typeface for the label
font = "DejaVu Sans:style=Bold"; // font

/* [Colors] */

// Bar (extruder 1)
body_color = "#3A7BD5"; // color

// Label (extruder 2)
label_color = "#FFFFFF"; // color

/* [Hidden] */

$fn = 64;

end_wall = 4;            // solid bar beyond each end of a closed slot
corner_r = 2;            // rounded corners of the bar in plan
groove_d = 3;            // finger groove width
groove_depth = 0.8;
groove_pitch = 5;
inlay = 0.6;
eps = 0.01;

slot_len = tube_width + 2 * clearance;
bar_w = slot_gap + 2 * jaw;                       // across the slot (Y)
bar_l = slot_len + 2 * end_wall + 2 * wing;       // along the slot (X)
label_h = min(jaw * 0.7, 8);                      // letter height
label_y = slot_gap / 2 + jaw / 2;                 // centre of the +Y jaw
label_len = slot_len - 2;                         // fits along the slot

// Plan of the bar: a rounded rectangle centred on the origin.
module bar_2d() {
    offset(r = corner_r) square([bar_l - 2 * corner_r, bar_w - 2 * corner_r], center = true);
}

// The slot, with ends rounded to the gap. open_end extends it out through +X.
module slot_2d() {
    hull() {
        translate([-slot_len / 2 + slot_gap / 2, 0]) circle(d = slot_gap);
        translate([slot_len / 2 - slot_gap / 2, 0]) circle(d = slot_gap);
    }
    if (style == "open_end")
        translate([0, -slot_gap / 2]) square([bar_l, slot_gap]);
}

// Finger grooves: half-round channels across each wing's top face.
module grooves_3d() {
    n = floor((wing - 2) / groove_pitch);
    if (grooves && n > 0)
        for (side = [-1, 1], i = [0 : n - 1]) {
            x = side * (slot_len / 2 + end_wall + 1 + groove_pitch / 2 + i * groove_pitch);
            translate([x, 0, height + groove_d / 2 - groove_depth])
                rotate([90, 0, 0]) cylinder(d = groove_d, h = bar_w + 2, center = true);
        }
}

// Shrink-only fit along X, as in models/cable-grommet: resize() the text plus
// a hair-thin bar of the target length, so short text is left alone.
module fit_x(w) {
    projection(cut = true) translate([0, 0, -0.5])
        resize([w, 0, 0], auto = [false, true, false])
            union() {
                linear_extrude(1) children();
                translate([-w / 2, 0, 10]) cube([w, 0.01, 0.01]);
            }
}

module label_2d() {
    translate([0, label_y])
        fit_x(label_len)
            text(label, size = label_h, font = font, halign = "center", valign = "center");
}

module label_3d(extra = 0) {
    translate([0, 0, height - inlay]) linear_extrude(height = inlay + extra) label_2d();
}

has_label = len(label) > 0;

color(body_color) difference() {
    linear_extrude(height = height) bar_2d();
    translate([0, 0, -eps]) linear_extrude(height = height + 2 * eps) slot_2d();
    grooves_3d();
    if (has_label) label_3d(eps);
}
if (has_label) color(label_color) label_3d();
