// Desk cable grommet — a sleeve that lines a hole through a desk, with a
// flange that sits on the desk top, and a cap that drops into the sleeve.
//
// Two pieces, laid out side by side:
//   sleeve  printed flange-down, the tube standing up from it.
//   cap     printed upside-down: its top face on the bed, the locating lip
//           pointing up. The top face comes off the bed smooth, and any
//           cap_text is inlaid flush into it (the first layers).
//
// Cap styles:
//   slot            a slot from the edge to the middle for the cables
//   brush_segments  thin radial fingers across the opening that flex apart
//                   around the cables and close up behind them
//   solid           blank, closes the hole
//   open_ring       a trim ring only, leaving the whole bore open
//
// fit is the clearance at both joints: the sleeve is fit smaller than the
// hole, and the cap's lip is fit smaller than the sleeve's bore.
//
// The colour parameters are the extruder order: sleeve_color is extruder 1,
// cap_color extruder 2, cap_text_color extruder 3. The sleeve and cap share a
// colour by default, which makes them one part on one filament.

/* [Hole] */

// Diameter (or side, for square) of the hole in the desk, mm
hole_d = 60; // [20:1:100]

// Thickness of the desk top, mm (sets the sleeve length)
desk_thickness = 25; // [10:1:60]

// How far the flange reaches over the desk beyond the hole, mm
flange_w = 6; // [3:1:15]

// Clearance at each joint, mm: sleeve in the hole, cap lip in the sleeve (0 = press fit)
fit = 0.3; // [0:0.1:1]

// Shape of the hole
shape = "round"; // [round:Round, square:Square (rounded corners)]

/* [Cap] */

// Cap style
cap_style = "slot"; // [slot:Cable slot, brush_segments:Brush fingers, solid:Solid, open_ring:Open ring]

// Width of the cable slot, mm (slot style only)
slot_w = 12; // [5:1:30]

// Text inlaid in the cap's top face (slot and solid styles only; shrinks to fit). Empty for none
cap_text = ""; // 12

// Typeface for the cap text
font = "DejaVu Sans:style=Bold"; // font

/* [Colors] */

// Sleeve and flange (extruder 1)
sleeve_color = "#5B6470"; // color

// Cap (extruder 2)
cap_color = "#5B6470"; // color

// Cap text (extruder 3)
cap_text_color = "#FFFFFF"; // color

/* [Hidden] */

$fn = 96;

sleeve_wall = 2.4;
flange_t = 3;
cap_t = 3;
lip_depth = 6;
lip_wall = 1.6;
finger_t = 1.2;          // brush finger thickness: 6 layers, still flexible
finger_gap = 0.8;        // slit between fingers
finger_pitch = 6;        // finger width at the root
corner_r = 5;            // corner radius of the square shape (outer)
inlay = 0.6;
gap = 6;                 // between the two pieces on the plate
eps = 0.01;

flange_size = hole_d + 2 * flange_w;
sleeve_size = hole_d - fit;
bore_size = sleeve_size - 2 * sleeve_wall;
lip_size = bore_size - fit;
lip_bore_size = lip_size - 2 * lip_wall;
cap_text_size = hole_d * 0.13;
slot_eff = min(slot_w, lip_bore_size * 0.8);
has_text = len(cap_text) > 0 && (cap_style == "slot" || cap_style == "solid");

// The hole's outline at a given size (diameter or side). Every outline is
// derived from this, so the square's corner radius tracks each offset.
module outline_2d(size) {
    if (shape == "square") {
        r = max(0.5, min(size / 2 - 0.5, corner_r - (hole_d - size) / 2));
        offset(r = r) square(size - 2 * r, center = true);
    } else {
        circle(d = size);
    }
}

// ---- sleeve ---------------------------------------------------------------

module sleeve() {
    color(sleeve_color) difference() {
        union() {
            linear_extrude(height = flange_t) outline_2d(flange_size);
            linear_extrude(height = flange_t + desk_thickness) outline_2d(sleeve_size);
        }
        translate([0, 0, -eps])
            linear_extrude(height = flange_t + desk_thickness + 2 * eps) outline_2d(bore_size);
    }
}

// ---- cap, modelled in use: top face at z = cap_t, lip hanging below z = 0 --

// Cable slot: from the middle out through the +Y edge, round-ended.
module slot_2d() {
    hull() {
        circle(d = slot_eff);
        translate([-slot_eff / 2, 0]) square([slot_eff, flange_size]);
    }
}

// Radial slits splitting the opening into fingers, plus a small hole where
// they meet so no finger ends in a sliver.
module finger_cuts_2d() {
    n = max(8, floor(PI * lip_bore_size / finger_pitch));
    for (i = [0 : n - 1])
        rotate(i * 360 / n)
            translate([0, -finger_gap / 2]) square([flange_size, finger_gap]);
    circle(d = max(3, finger_gap * 4));
}

// Plan of the cap's top plate. The finger web is handled separately.
module cap_plate_2d() {
    difference() {
        outline_2d(flange_size);
        if (cap_style == "slot") slot_2d();
        if (cap_style == "open_ring" || cap_style == "brush_segments")
            outline_2d(lip_bore_size);
    }
}

module lip_2d() {
    difference() {
        outline_2d(lip_size);
        outline_2d(lip_bore_size);
        if (cap_style == "slot") slot_2d();
    }
}

// Text in the solid part of the cap: centred in the band between the slot's
// end and the rim on the side opposite the slot, or across the middle of a
// solid cap. Letters are cap_text_size tall at most and shrink
// (never grow) to fit the widest chord of the band they sit in, measured on
// a circle 1.5 mm inside the cap's edge, so a long word is scaled down rather
// than cut off by the rim.
text_r = flange_size / 2 - 1.5;
text_y_in = cap_style == "slot" ? slot_eff / 2 + 1.5 : 0;
text_h = cap_style == "slot" ? min(cap_text_size, (text_r - text_y_in) * 0.8) : cap_text_size;
text_yc = cap_style == "slot" ? -(text_y_in + text_r) / 2 : 0;
text_w = 2 * sqrt(max(0, pow(text_r, 2) - pow(abs(text_yc) + text_h / 2, 2)));

// Shrink-only fits, as in models/building-brick: resize() the children plus
// a hair-thin bar of the target length, so smaller text is left alone and
// larger text is scaled down uniformly.
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

module cap_text_2d() {
    intersection() {
        translate([0, text_yc])
            fit_y(text_h) fit_x(text_w)
                text(cap_text, size = text_h, font = font,
                     halign = "center", valign = "center");
        offset(delta = -1.5) cap_plate_2d();
    }
}

module cap_text_3d(extra = 0) {
    translate([0, 0, cap_t - inlay]) linear_extrude(height = inlay + extra) cap_text_2d();
}

module cap_in_use() {
    color(cap_color) difference() {
        union() {
            linear_extrude(height = cap_t) cap_plate_2d();
            translate([0, 0, -lip_depth]) linear_extrude(height = lip_depth + eps) lip_2d();
            if (cap_style == "brush_segments")
                translate([0, 0, cap_t - finger_t])
                    linear_extrude(height = finger_t)
                        difference() {
                            outline_2d(lip_bore_size + eps);
                            finger_cuts_2d();
                        }
        }
        if (has_text) cap_text_3d(eps);
    }
    if (has_text) color(cap_text_color) cap_text_3d();
}

// ---- plate layout -----------------------------------------------------------

sleeve();

// Turned over about X so the top face lands on the bed (a rotation, so the
// inlaid text still reads correctly from above once the cap is fitted).
translate([flange_size + gap, 0, cap_t]) rotate([180, 0, 0]) cap_in_use();
