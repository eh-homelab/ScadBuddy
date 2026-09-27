// Name sign / desk nameplate — one or two lines of text on a flat plate, with
// an optional border and a choice of mounting: a slotted desk-stand foot
// printed alongside, screw holes, magnet pockets in the back, or a hanging
// loop.
//
// The sign prints flat, face up. Every text style keeps the plate a single
// flat slab, so nothing needs supports:
//   raised - letters and border stand proud of the plate
//   inlay  - letters and border are filled pockets, flush with the face
//   cutout - the face layer has the letters cut through it, showing a backing
//            layer in the text colour underneath (one colour change per plate)
//
// Written to the MakerWorld Parametric Model Maker customizer conventions so
// the same file works unchanged on MakerWorld and in ScadBuddy.
//
// The colour parameters are the extruder order: plate_color is extruder 1,
// text_color extruder 2, border_color extruder 3. Colours that are equal merge
// into one part and one filament — with the defaults the text and border are
// the same colour, so the sign is two colours.

/* [Text] */

// First line of text
line1 = "Elan"; // 30

// Second line, smaller, under the first (leave empty for one line)
line2 = ""; // 30

// Typeface (the app fills this dropdown from the fonts installed in the image)
font = "DejaVu Sans:style=Bold"; // font

// Letter height of the first line in mm; auto-fit only ever shrinks it
text_size = 18; // [6:1:40]

// Letter height of the second line in mm
line2_size = 10; // [4:1:30]

// Shrink the text so it always fits inside the plate (never enlarges it)
auto_fit = true;

// How the letters are made
text_style = "raised"; // [raised:Raised, inlay:Flush inlay, cutout:Cut through to backing]

/* [Plate] */

// Plate width in mm
width = 140; // [40:5:300]

// Plate height in mm
height = 45; // [20:5:150]

// Plate thickness in mm (raised letters add to this)
thickness = 4; // [2:0.5:10]

// Corner shape
corner = "rounded"; // [square:Square, rounded:Rounded, chamfered:Chamfered]

// Corner radius or chamfer size in mm
corner_r = 5; // [0:1:20]

// Add a border line around the plate
border = true;

// Border line width in mm
border_w = 2; // [1:0.5:6]

/* [Mounting] */

// How the sign is mounted
mount = "desk_stand"; // [none:None, screw_holes:Screw holes, magnet_pockets:Magnet pockets, hanging_loop:Hanging loop, desk_stand:Desk stand]

// Magnet diameter in mm (the pocket adds clearance)
magnet_d = 8; // [5:0.5:15]

// Desk stand lean, degrees from horizontal (90 would be upright)
stand_angle = 70; // [45:5:85]

/* [Colors] */

// Plate colour (extruder 1); also the desk-stand foot
plate_color = "#FFFFFF"; // color

// Text colour (extruder 2); the backing layer in cutout style
text_color = "#1E1E1E"; // color

// Border colour (extruder 3); set it equal to the text colour to share a filament
border_color = "#1E1E1E"; // color

/* [Hidden] */

$fn = 64;

// Height of raised letters, depth of inlay pockets and of the cutout face.
relief = 1.2;
// Distance from the plate edge to the outside of the border.
border_inset = 2;
// Clear space between the border (or plate edge) and the text.
text_pad = 2;
// Gap between the two lines, as a fraction of the second line's size.
line_gap = 0.4;
// Screw holes: shank clearance and 90-degree countersink head diameter.
screw_d = 4.2;
screw_head_d = 8.4;
// Magnet pockets: magnet thickness and clearance on diameter and depth.
magnet_h = 2;
magnet_clear = 0.2;
// Hanging loop hole and the material around it.
loop_hole_d = 5;
loop_wall = 3;
// Desk stand: slot clearance and gap between the sign and the foot on the plate.
stand_clear = 0.4;
part_gap = 6;

W = width;
H = height;
T = thickness;
has1 = len(line1) > 0;
has2 = len(line2) > 0;

// Inlay pockets and the cutout face must leave material underneath.
rel = text_style == "raised" ? relief : min(relief, T * 0.4);

// Distance from the plate edge to the inside of the border.
border_in = border ? border_inset + border_w : border_inset;

// Screw holes sit inside the border at each end; the text keeps clear of them.
screw_x = W / 2 - (border_in + 1 + screw_head_d / 2);
screw_reserve = mount == "screw_holes" ? 2 * (screw_head_d + 2) : 0;

// Box the text is fitted into.
text_w = max(1, W - 2 * (border_in + text_pad) - screw_reserve);
text_h = max(1, H - 2 * (border_in + text_pad));

// ---------------------------------------------------------------- outline

module plate_2d() {
    r = corner == "square" ? 0 : min(corner_r, min(W, H) / 2 - 0.01);
    if (r <= 0) square([W, H], center = true);
    else if (corner == "rounded")
        offset(r = r) square([W - 2 * r, H - 2 * r], center = true);
    else
        offset(delta = r, chamfer = true) square([W - 2 * r, H - 2 * r], center = true);
}

loop_r = loop_hole_d / 2 + loop_wall;
loop_cy = H / 2 + loop_hole_d / 2 + 1.5;

module outline_2d() {
    difference() {
        union() {
            plate_2d();
            if (mount == "hanging_loop")
                hull() {
                    translate([0, loop_cy]) circle(r = loop_r);
                    translate([-loop_r, H / 2 - 1]) square([2 * loop_r, 1]);
                }
        }
        if (mount == "hanging_loop")
            translate([0, loop_cy]) circle(d = loop_hole_d);
    }
}

module border_2d() {
    if (border)
        difference() {
            offset(delta = -border_inset) plate_2d();
            offset(delta = -border_in) plate_2d();
        }
}

// ---------------------------------------------------------------- text

// Shrink-only fit. resize() scales to the bounding box of its children; a
// hair-thin bar of the target length, parked at z=10, makes that box at least
// the target size, so text already smaller is left alone and larger text is
// scaled down uniformly. z is left unscaled, so a cut at z=0.5 returns just
// the text.
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

module line_2d(s, size) {
    if (auto_fit) fit_x(text_w)
        text(s, size = size, font = font, halign = "center", valign = "center");
    else
        text(s, size = size, font = font, halign = "center", valign = "center");
}

// Lines stacked about the origin by their nominal sizes.
h1 = has1 ? text_size * 1.1 : 0;
h2 = has2 ? line2_size * 1.1 : 0;
block_h = h1 + h2 + (has1 && has2 ? line_gap * line2_size : 0);

module lines_2d() {
    if (has1) translate([0, block_h / 2 - h1 / 2]) line_2d(line1, text_size);
    if (has2) translate([0, -block_h / 2 + h2 / 2]) line_2d(line2, line2_size);
}

// The text, fitted, and always clipped to the space inside the border (and
// clear of the screw heads) so no combination can overhang the plate.
module text_2d() {
    if (has1 || has2)
        difference() {
            intersection() {
                if (auto_fit) fit_y(text_h) lines_2d();
                else lines_2d();
                offset(delta = -(border_in + text_pad / 2)) plate_2d();
            }
            if (mount == "screw_holes")
                for (sx = [-1, 1]) translate([sx * screw_x, 0])
                    circle(d = screw_head_d + 2);
        }
}

// ---------------------------------------------------------------- holes

// Deepest a back pocket may go and still leave a skin under the face detail.
pocket_depth = min(magnet_h + magnet_clear,
                   T - (text_style == "raised" ? 0.8 : rel + 0.6));
pocket_d = magnet_d + magnet_clear;

// Pockets go in the four corners, clear of the rounding, inside the border.
magnet_m = max(border_in + 1 + pocket_d / 2,
               corner == "square" ? 0 : min(corner_r, min(W, H) / 2));
magnet_pos = [for (sx = [-1, 1], sy = [-1, 1])
                  [sx * max(0, W / 2 - magnet_m), sy * max(0, H / 2 - magnet_m)]];

module holes_3d() {
    if (mount == "screw_holes")
        for (sx = [-1, 1]) translate([sx * screw_x, 0, 0]) {
            translate([0, 0, -1]) cylinder(d = screw_d, h = T + rel + 2);
            // 90-degree countersink opening upwards: prints without support.
            cs = (screw_head_d - screw_d) / 2;
            translate([0, 0, T - cs]) cylinder(d1 = screw_d, d2 = screw_head_d, h = cs + 0.001);
            translate([0, 0, T]) cylinder(d = screw_head_d, h = rel + 1);
        }
    if (mount == "magnet_pockets" && pocket_depth > 0.2)
        for (p = magnet_pos) translate([p[0], p[1], -1])
            cylinder(d = pocket_d, h = pocket_depth + 1);
}

// ---------------------------------------------------------------- sign

if (text_style == "raised") {
    color(plate_color) difference() {
        linear_extrude(T) outline_2d();
        holes_3d();
    }
    color(text_color) translate([0, 0, T]) linear_extrude(rel) text_2d();
    color(border_color) difference() {
        translate([0, 0, T]) linear_extrude(rel) border_2d();
        holes_3d();
    }
} else if (text_style == "inlay") {
    color(plate_color) difference() {
        linear_extrude(T) outline_2d();
        translate([0, 0, T - rel]) linear_extrude(rel + 1) { text_2d(); border_2d(); }
        holes_3d();
    }
    color(text_color) translate([0, 0, T - rel]) linear_extrude(rel) text_2d();
    color(border_color) difference() {
        translate([0, 0, T - rel]) linear_extrude(rel) border_2d();
        holes_3d();
    }
} else {
    // cutout: text-coloured backing, plate-coloured face with the letters cut
    // through it, border inlaid in the face.
    color(text_color) difference() {
        linear_extrude(T - rel) outline_2d();
        holes_3d();
    }
    color(plate_color) difference() {
        translate([0, 0, T - rel]) linear_extrude(rel)
            difference() { outline_2d(); text_2d(); border_2d(); }
        holes_3d();
    }
    color(border_color) difference() {
        translate([0, 0, T - rel]) linear_extrude(rel) border_2d();
        holes_3d();
    }
}

// ---------------------------------------------------------------- desk stand

// A separate foot printed next to the sign: a block with a slot leaning back
// at stand_angle. The slot's front wall overhangs by (90 - stand_angle)
// degrees, at most 45, so it prints without support. The sign's bottom edge
// drops into the slot.
stand_slot = T + (text_style == "raised" ? rel : 0) + stand_clear;
stand_front = 4;     // material in front of the slot
stand_floor = 2;     // material under the slot
stand_h = stand_floor + max(8, min(16, H * 0.12));
// Deep enough that the sign's centre of mass sits over the foot.
stand_d = max(25, stand_front + stand_slot + H / 2 * cos(stand_angle) + 8);
stand_len = min(W, max(30, W * 0.6));

module stand_profile() {
    a = stand_angle;
    u = [cos(a), sin(a)];          // up the slot
    n = [sin(a), -cos(a)];         // across the slot, towards the back
    p0 = [stand_front, stand_floor + stand_slot * cos(a)];
    p1 = p0 + stand_slot * n;
    L = 3 * stand_h / sin(a) + 10;
    c = 1.5;
    difference() {
        polygon([[0, 0], [stand_d, 0], [stand_d, stand_h - c],
                 [stand_d - c, stand_h], [c, stand_h], [0, stand_h - c]]);
        polygon([p0, p1, p1 + L * u, p0 + L * u]);
    }
}

if (mount == "desk_stand")
    color(plate_color)
        translate([-stand_len / 2, H / 2 + part_gap, 0])
            // extrude along x: profile (p, q) -> world (y = p, z = q)
            multmatrix([[0, 0, 1, 0], [1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 0, 1]])
                linear_extrude(stand_len) stand_profile();
