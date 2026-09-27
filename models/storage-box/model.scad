// Storage box with lid — a parametric box in four footprints (rectangle,
// rounded rectangle, hexagon, round/oval) with optional grid dividers and a
// choice of lid: friction plug, dovetail sliding lid, or snap-fit.
//
// The box and lid print side by side. Friction and snap lids print upside
// down (lid top on the bed, locating lip pointing up); the sliding lid is a
// flat plate and prints the right way up. The knob handle on a friction or
// snap lid is a separate small part with a peg that presses into a hole in
// the lid top.
//
// Written to the MakerWorld Parametric Model Maker customizer conventions so
// the same file works unchanged on MakerWorld and in ScadBuddy.
//
// The colour parameters are the extruder order: box_color is extruder 1,
// lid_color is extruder 2, lid_text_color is extruder 3. Box and lid share a
// colour by default, which merges them onto one extruder.

/* [Box] */

// Inside length (X), wall to wall, in mm
inner_l = 80; // [20:5:250]

// Inside width (Y), wall to wall, in mm
inner_w = 60; // [20:5:250]

// Inside height, floor to rim (sliding lid: floor to the underside of the lid)
inner_h = 40; // [10:5:200]

// Wall thickness
wall = 2; // [1.2:0.2:4]

// Floor thickness (also the thickness of the lid top)
floor = 1.6; // [1:0.2:4]

// Footprint shape
shape = "rounded"; // [rectangle, rounded, hexagon, round]

// Inside corner radius (rounded shape only)
corner_r = 6; // [0:1:30]

// Dividers across the length (walls parallel to Y)
dividers_x = 0; // [0:1:6]

// Dividers across the width (walls parallel to X)
dividers_y = 0; // [0:1:6]

/* [Lid] */

// Lid type (sliding needs rectangle or rounded; other shapes get a friction lid)
lid_type = "friction"; // [none, friction, sliding, snap]

// Lid height, top to the rim it sits on (friction and snap lids)
lid_h = 10; // [4:1:40]

// Clearance between lid and box
tolerance = 0.25; // [0.1:0.05:0.6]

// Handle on the lid
handle = "none"; // [none, knob, finger_notch]

// Text inlaid in the lid top (leave empty for none)
lid_text = ""; // 24

// Lid text height in mm
lid_text_size = 12; // [5:1:40]

// Lid text typeface
font = "DejaVu Sans:style=Bold"; // font

/* [Colors] */

// Box colour (extruder 1)
box_color = "#4A7FB5"; // color

// Lid colour (extruder 2; same as the box by default)
lid_color = "#4A7FB5"; // color

// Lid text colour (extruder 3)
lid_text_color = "#FFFFFF"; // color

/* [Hidden] */

$fn = 64;

// Gap between parts on the plate.
gap = 10;

// Sliding lids need straight long sides to run in.
sliding_ok = shape == "rectangle" || shape == "rounded";
lid = (lid_type == "sliding" && !sliding_ok) ? "friction" : lid_type;
plug_lid = lid == "friction" || lid == "snap";

// Friction/snap: the locating lip that drops inside the box.
lip_d = min(6, inner_h / 2);
lip_w = max(1.2, wall);

// Snap: V bump on the lip, V groove in the box wall, same depth.
snap_b = min(0.6, wall * 0.4);

// Sliding: plate thickness, groove depth into the long walls, and the
// horizontal clearance (the dovetail faces are 45 degrees).
slide_t = 2;
slide_g = min(max(wall / 2, tolerance + 0.4), wall - 0.4);

// Inlay depth for the lid text.
text_depth = min(0.6, floor / 2);

// Knob: separate part, head + peg that presses into the lid top.
knob_d = 18;
knob_h = 8;
peg_d = 6;
peg_hole_d = peg_d + tolerance / 2;
knob = handle == "knob" && lid != "none";
notch = handle == "finger_notch" && lid != "none";

has_text = len(lid_text) > 0;

// Box height: rim of the box (the lid of a friction/snap box sits above it).
box_h = floor + inner_h + (lid == "sliding" ? slide_t : 0);

// ---- footprint -----------------------------------------------------------

function cr(l, w) = min(corner_r, min(l, w) / 2 - 0.5);
function sharp(l, w) = shape == "rectangle" || shape == "hexagon"
                    || (shape == "rounded" && cr(l, w) <= 0);

// Elongated regular-angled hexagon, points along the longer axis.
module hexagon(l, w) {
    if (l >= w) {
        c = w / (2 * sqrt(3));
        polygon([[l/2, 0], [l/2 - c, w/2], [-l/2 + c, w/2],
                 [-l/2, 0], [-l/2 + c, -w/2], [l/2 - c, -w/2]]);
    } else {
        rotate(90) hexagon(w, l);
    }
}

module inner_2d(l, w) {
    if (shape == "hexagon") hexagon(l, w);
    else if (shape == "round") scale([l / 2, w / 2]) circle(r = 1, $fn = 96);
    else if (sharp(l, w)) square([l, w], center = true);
    else offset(r = cr(l, w)) square([l - 2 * cr(l, w), w - 2 * cr(l, w)], center = true);
}

// The inside footprint grown (o > 0) or shrunk (o < 0) by o.
module shape_off(o, l = inner_l, w = inner_w) {
    if (o == 0) inner_2d(l, w);
    else if (sharp(l, w)) offset(delta = o) inner_2d(l, w);
    else offset(r = o) inner_2d(l, w);
}

// Outside extents of the box, for laying the parts out.
hex_long = wall / sin(60);
box_x = inner_l + 2 * ((shape == "hexagon" && inner_l >= inner_w) ? hex_long : wall);
box_y = inner_w + 2 * ((shape == "hexagon" && inner_w > inner_l) ? hex_long : wall);

// Double-frustum V ring around the footprint: offset o0 at z0 +/- h, o0 + d
// at z0. Every footprint here is convex, so a hull is exact.
module v_band(o0, d, z0, h) {
    hull() {
        translate([0, 0, z0 - h]) linear_extrude(0.01) shape_off(o0);
        translate([0, 0, z0]) linear_extrude(0.01) shape_off(o0 + d);
        translate([0, 0, z0 + h]) linear_extrude(0.01) shape_off(o0);
    }
}

// ---- sliding lid, in a frame where it slides along +X --------------------

slide_rot = inner_l >= inner_w ? 0 : 90;
sL = max(inner_l, inner_w);
sW = min(inner_l, inner_w);
s_a = sW / 2;
s_al = sL / 2;

module slide_channel_local() {
    intersection() {
        translate([0, 0, box_h - slide_t])
            rotate([90, 0, 90]) linear_extrude(4 * sL, center = true)
                polygon([[-(s_a + slide_g), 0], [s_a + slide_g, 0],
                         [s_a + slide_g - slide_t - 1, slide_t + 1],
                         [-(s_a + slide_g - slide_t - 1), slide_t + 1]]);
        translate([0, 0, box_h - slide_t - 1]) linear_extrude(slide_t + 3)
            union() {
                shape_off(slide_g, sL, sW);
                translate([-sL, -sW]) square([sL, 2 * sW]);
            }
    }
}

s_half = s_a + slide_g - tolerance;       // lid half-width at its bottom face
s_x0 = -(s_al + wall);                    // open end, flush with the box
s_x1 = s_al + slide_g - tolerance;        // closed end

module slide_plate_local() {
    intersection() {
        rotate([90, 0, 90]) linear_extrude(4 * sL, center = true)
            polygon([[-s_half, 0], [s_half, 0],
                     [s_half - slide_t, slide_t], [-(s_half - slide_t), slide_t]]);
        linear_extrude(slide_t) intersection() {
            union() {
                shape_off(slide_g - tolerance, sL, sW);
                translate([s_x0, -sW]) square([-s_x0, 2 * sW]);
            }
            translate([s_x0, -sW]) square([2 * sL, 2 * sW]);
        }
    }
}

slide_knob_x = s_x0 + 9;

module slide_text_2d() {
    difference() {
        intersection() {
            text(lid_text, size = lid_text_size, font = font,
                 halign = "center", valign = "center");
            offset(delta = -1.5) projection() slide_plate_local();
        }
        if (knob) translate([slide_knob_x, 0]) circle(d = 12);
    }
}

// Printed right way up: z 0 .. slide_t (+ knob).
module slide_lid_local() {
    difference() {
        slide_plate_local();
        if (notch) translate([s_x0 + 12, 0, slide_t + 10 - slide_t / 2]) sphere(r = 10);
        if (has_text) translate([0, 0, slide_t - text_depth])
            linear_extrude(text_depth + 1) slide_text_2d();
    }
    if (knob) translate([slide_knob_x, 0, slide_t - 0.01])
        cylinder(d1 = 10, d2 = 8, h = 5);
}

// ---- box ------------------------------------------------------------------

divider_top = box_h - (plug_lid ? lip_d + 0.5 : 0) - (lid == "sliding" ? slide_t : 0);

module dividers_2d() {
    intersection() {
        shape_off(0.01);
        union() {
            if (dividers_x > 0) for (i = [1 : dividers_x])
                translate([-inner_l / 2 + i * inner_l / (dividers_x + 1), 0])
                    square([wall, inner_w + 2], center = true);
            if (dividers_y > 0) for (i = [1 : dividers_y])
                translate([0, -inner_w / 2 + i * inner_w / (dividers_y + 1)])
                    square([inner_l + 2, wall], center = true);
        }
    }
}

notch_r = min(10, inner_h / 2, min(inner_l, inner_w) / 4);

module box() {
    difference() {
        linear_extrude(box_h) shape_off(wall);
        translate([0, 0, floor])
            linear_extrude(inner_h + (lid == "sliding" ? 0.01 : 1)) shape_off(0);
        if (lid == "sliding") rotate(slide_rot) slide_channel_local();
        if (lid == "snap") v_band(-0.01, snap_b + 0.01, box_h - lip_d / 2, snap_b + 0.01);
        if (notch && plug_lid) {
            if (inner_l >= inner_w)
                translate([0, -inner_w / 2, box_h]) rotate([90, 0, 0])
                    cylinder(r = notch_r, h = 2 * box_y, center = true);
            else
                translate([-inner_l / 2, 0, box_h]) rotate([0, 90, 0])
                    cylinder(r = notch_r, h = 2 * box_x, center = true);
        }
    }
    if (dividers_x + dividers_y > 0 && divider_top > floor + 1)
        translate([0, 0, floor - 0.01]) linear_extrude(divider_top - floor + 0.01)
            dividers_2d();
}

// ---- friction / snap lid, modelled where it sits on the box ---------------

lid_top = box_h + lid_h;
text_y = knob ? -(knob_d / 2 + 2 + lid_text_size / 2) : 0;

module plug_text_2d() {
    difference() {
        intersection() {
            translate([0, text_y]) text(lid_text, size = lid_text_size, font = font,
                                        halign = "center", valign = "center");
            shape_off(wall - 1.5);
        }
        if (knob) circle(d = knob_d);
    }
}

module plug_lid_body() {
    difference() {
        union() {
            translate([0, 0, box_h]) linear_extrude(lid_h) shape_off(wall);
            translate([0, 0, box_h - lip_d]) linear_extrude(lip_d + 0.5)
                difference() {
                    shape_off(-tolerance);
                    shape_off(-(tolerance + lip_w));
                }
            if (lid == "snap") intersection() {
                difference() {
                    v_band(-tolerance, snap_b, box_h - lip_d / 2, snap_b);
                    translate([0, 0, box_h - lip_d - 1]) linear_extrude(lip_d + 2)
                        shape_off(-(tolerance + lip_w / 2));
                }
                if (inner_l >= inner_w) cube([inner_l / 3, 3 * box_y, 3 * lid_top], center = true);
                else cube([3 * box_x, inner_w / 3, 3 * lid_top], center = true);
            }
        }
        if (lid_h > floor) translate([0, 0, box_h - 1])
            linear_extrude(lid_h - floor + 1) shape_off(-(tolerance + lip_w));
        if (knob) translate([0, 0, lid_top - floor - 1]) cylinder(d = peg_hole_d, h = floor + 2);
        if (has_text) translate([0, 0, lid_top - text_depth])
            linear_extrude(text_depth + 1) plug_text_2d();
    }
}

module plug_lid_text() {
    translate([0, 0, lid_top - text_depth]) linear_extrude(text_depth) plug_text_2d();
}

// Flip a plug-lid part top-down onto the bed, beside the box.
module flip_plug(dx) {
    translate([dx, 0, lid_top]) rotate([180, 0, 0]) children();
}

// Knob, printed head-down: head top on the bed, peg pointing up.
module knob_part() {
    cylinder(d1 = knob_d, d2 = knob_d - 4, h = knob_h);
    translate([0, 0, knob_h - 0.01]) cylinder(d = peg_d, h = floor + 0.01);
}

// ---- plate ------------------------------------------------------------------

plug_x = box_x + gap;
knob_x = plug_x + box_x / 2 + gap + knob_d / 2;
slide_dx = box_x / 2 + gap + (slide_rot == 0 ? -s_x0 : s_half);

color(box_color) box();

if (plug_lid) {
    color(lid_color) {
        flip_plug(plug_x) plug_lid_body();
        if (knob) translate([knob_x, 0, 0]) knob_part();
    }
    if (has_text) color(lid_text_color) flip_plug(plug_x) plug_lid_text();
}

if (lid == "sliding") {
    color(lid_color) translate([slide_dx, 0, 0]) rotate(slide_rot) slide_lid_local();
    if (has_text) color(lid_text_color)
        translate([slide_dx, 0, slide_t - text_depth]) rotate(slide_rot)
            linear_extrude(text_depth) slide_text_2d();
}
