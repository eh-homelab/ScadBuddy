// Spinning top, print in place — comes off the plate ready to spin: no
// assembly, no supports. Three variants:
//
//   one_piece      A single solid top, printed upside down with its patterned
//                  face on the bed, so the pattern is the first three layers
//                  and comes out bed-smooth. The stem stands in a 45-degree
//                  funnel-shaped well in the face, flush with it (a stem that
//                  stood proud of the face could not share the bed with it):
//                  pinch it in the well and twist. The tip is the last layers
//                  and narrows upwards, so nothing overhangs.
//   with_launcher  A top whose stem is a 12-tooth pinion, printed in place
//                  inside a launcher housing with a toothed rack threaded
//                  through it. Printed pinion down: the pinion stands on the
//                  bed in a through-hole in the housing, the top's face is a
//                  45-degree dome above the housing, and the rack runs in a
//                  roofed channel beside the pinion. In use, stand the top on
//                  its tip with the launcher over it, pull the ring, and lift
//                  the launcher off: the pinion slides straight out of the
//                  through-hole. Thread the rack back in to launch again.
//   gyro_ring      The one-piece top with a free ring printed in place around
//                  its rim, held by a 45-degree V ridge in a matching groove.
//                  The core spins while the ring stays loose around it.
//
// Every downward-facing surface is at 45 degrees or steeper, except the flat
// roof over the rack channel, which is a short bridge. Parts printed in place
// are `clearance` apart everywhere (verify.sh checks all of this).
//
// Everything on the top is a solid of revolution, and each pattern colour and
// the name are repeated evenly around the axis, so every colour's centre of
// mass lies on the spin axis.
//
// Written to the MakerWorld Parametric Model Maker customizer conventions so
// the same file works unchanged on MakerWorld and in ScadBuddy.
//
// The colour parameters are the extruder order: body_color is extruder 1,
// pattern_color 2, pattern2_color 3, name_color 4, rim_color 5, stem_color 6,
// tip_color 7, ring_color 8, launcher_color 9, rack_color 10. Equal colours
// share an extruder: by default the rim, stem and tip are one red part and the
// name shares the second pattern colour.

/* [Top] */

// Which top to print
variant = "one_piece"; // [one_piece:One piece, with_launcher:With rack launcher, gyro_ring:With captive gyro ring]

// Top diameter in mm
diameter = 50; // [30:5:80]

// Body shape
style = "classic_cone"; // [classic_cone:Classic cone, ufo_disc:UFO disc, flower:Flower]

// Stem length in mm. One piece and gyro ring: the depth of the well the stem stands in (it is flush with the face). With launcher: the length of the geared stem
stem_length = 8; // [5:1:20]

// Stem diameter in mm (one piece and gyro ring; the launcher top's stem is its gear)
stem_d = 7; // [5:0.5:12]

// Tip shape: a ball tip is safer and wanders around a table; a point spins in place longer
tip = "ball"; // [point:Point, ball:Ball]

// Air gap between parts printed in place (launcher, rack, ring), in mm; raise it if parts fuse
clearance = 0.4; // [0.2:0.05:0.6]

/* [Launcher] */

// Gear tooth size (module) of the stem and rack, in mm; bigger teeth are sturdier
gear_module = 1.5; // [1.25:0.25:2]

// Toothed length of the rack in mm; a longer pull spins the top up for longer
pull_length = 100; // [60:10:160]

/* [Decor] */

// Pattern inlaid in the face, in two colours
pattern = "spiral"; // [spiral:Spiral, rays:Rays, rings:Rings, dots:Dots, none:None]

// Name around the rim, printed twice, opposite each other (leave empty for none)
name = ""; // 12

/* [Colors] */

// Body colour (extruder 1)
body_color = "#1E88E5"; // color

// Pattern colour: half the spiral arms or rays, the inner rings or dots (extruder 2)
pattern_color = "#FFEB3B"; // color

// Second pattern colour: the other half of the pattern (extruder 3)
pattern2_color = "#FFFFFF"; // color

// Name colour (extruder 4)
name_color = "#FFFFFF"; // color

// Rim colour: a border around the face and the disc's edge (extruder 5)
rim_color = "#E53935"; // color

// Stem colour: the stem, or the launcher top's gear (extruder 6)
stem_color = "#E53935"; // color

// Tip colour (extruder 7)
tip_color = "#E53935"; // color

// Gyro ring colour (extruder 8)
ring_color = "#43A047"; // color

// Launcher housing colour (extruder 9)
launcher_color = "#FB8C00"; // color

// Rack colour (extruder 10)
rack_color = "#8E24AA"; // color

/* [Hidden] */

$fn = 96;

// "print" is the plate as printed; "show" flips it over (face up, launcher
// above the top) for the catalogue thumbnail.
display = "print";
// Which bodies to render: all, top, ring, launcher or rack (verify.sh).
part = "all";

// Pattern and name inlay depth: three 0.2 mm layers.
inlay = 0.6;
face_chamfer = 0.8;
under_chamfer = 1;
// Width of the rim-coloured border.
rim_w = 2;
name_size = 3.5;
font = "DejaVu Sans:style=Bold";

R = diameter / 2;
c = clearance;
is_launcher = variant == "with_launcher";
is_ring = variant == "gyro_ring";
ufo = style == "ufo_disc";

tip_r = tip == "ball" ? min(4, max(2, diameter * 0.06)) : 0.6;
tip_h = tip == "ball" ? 2 * tip_r + 0.5 : 4;       // tip colour zone
// Gyro ring: a V ridge g deep, engaging the rim 1 mm past the clearance.
g = c + 1;
rim_t = max(ufo ? max(3.5, diameter * 0.08) : max(4, diameter * 0.1),
            is_ring ? 2 * g + 1.6 : 0);
b = ufo ? 0 : under_chamfer;
fc = is_launcher ? 0 : face_chamfer;

// One-piece stem, standing in a 45-degree well D deep.
r_s = min(stem_d / 2, R * 0.25);
D = max(3, min(stem_length, R - rim_w - 4 - r_s));
r_w = r_s + D;                                      // well radius at the face

// Launcher gear: 12 teeth, 25-degree pressure angle (no undercut at 12).
m = gear_module;
gz = 12;
alpha = 25;
r_p = m * gz / 2;
r_tip = r_p + m;
r_root = r_p - 1.25 * m;
pitch = PI * m;
h_r = 6;                        // rack height
roof = 1.6;                     // housing roof over the rack
t_l = h_r + c + roof;           // housing thickness
L_g = max(stem_length, t_l + 2);
r_ch = r_tip + c / 2;           // through-hole the gear stands in (gear is c/2 undersize)

// Heights, use orientation (tip on z=0).
under_h0 = ufo ? diameter * 0.16 : diameter * 0.34;
under_h = max(under_h0, tip_h + 2, is_launcher ? 0 : tip_h + 2 + D - rim_t);
H_r = under_h + rim_t;                  // top of the rim (one piece: the face)
// The dome narrows to just inside the gear's root, so it never overhangs
// the gear's tooth spaces with a flat ledge.
r_dome = r_root - c;
H_f = H_r + R - r_dome;                 // launcher: top of the 45-degree dome
H_tot = is_launcher ? H_f + L_g : H_r;
z_wb = H_r - D;                         // bottom of the well

// UFO saucer apex.
r_a = max(2 * tip_r, 3);
z_a = tip == "ball" ? tip_r + 1 : 2;

// Flower outline: a centre disc and six round petals reaching the full diameter.
petals = 6;
petal_r = R * 0.3;
flower_in = R * 0.78;

// Gyro ring: centred in the rim.
zc = under_h + rim_t / 2;
ring_w = max(3, diameter * 0.07);

// ---------------------------------------------------------------- body

module flower_2d() {
    offset(r = -1) offset(r = 1) union() {
        circle(r = flower_in);
        for (k = [0:petals - 1]) rotate(k * 360 / petals)
            translate([R - petal_r, 0]) circle(r = petal_r, $fn = 48);
    }
}

module outline_2d() {
    if (style == "flower") flower_2d(); else circle(r = R);
}

module outline(h0, h1) {
    if (style == "flower")
        intersection() {
            children();
            translate([0, 0, h0 - 1]) linear_extrude(h1 - h0 + 2) flower_2d();
        }
    else children();
}

// Half cross-section in (r, z), use orientation: the tip and the cone (or
// saucer) up to the disc's underside, then the disc — flat-faced for the one
// piece, a 45-degree dome for the launcher top.
module profile_2d() {
    intersection() {
        square([R + 1, H_f + 1]);
        hull() {
            translate([0, tip_r]) circle(r = tip_r, $fn = 48);
            if (ufo)
                polygon(concat([[0, under_h]],
                    [for (a = [0:5:90]) [r_a + (R - r_a) * cos(a),
                                         under_h - (under_h - z_a) * sin(a)]]));
            else
                polygon([[0, under_h - 0.01], [R - b, under_h], [0, under_h]]);
        }
    }
    if (is_launcher)
        polygon([[0, under_h], [R - b, under_h], [R, under_h + b], [R, H_r],
                 [r_dome, H_f], [0, H_f]]);
    else
        polygon([[0, under_h], [R - b, under_h], [R, under_h + b],
                 [R, H_r - fc], [R - fc, H_r], [0, H_r]]);
}

// One-piece stem: a round stem with twelve small grip ribs.
module stem_2d() {
    circle(r = r_s);
    for (k = [0:11]) rotate(k * 30) translate([r_s, 0]) circle(r = 0.6, $fn = 16);
}

// ---------------------------------------------------------------- gear

// The rack, pitch line at y = -r_p, teeth pointing +y at x = k * pitch.
module rack_tooth_2d(x, tip_y) {
    t = tan(alpha);
    hp = pitch / 4;
    y_root = -r_p - 1.25 * m;
    polygon([[x - hp - (-r_p - y_root) * t, y_root - 0.01],
             [x + hp + (-r_p - y_root) * t, y_root - 0.01],
             [x + hp - (tip_y + r_p) * t, tip_y],
             [x - hp + (tip_y + r_p) * t, tip_y]]);
}

// The pinion, generated by rolling the rack around it: rotating the pinion by
// phi moves the rack by r_p * phi, so the union of the rack at every phi,
// seen from the pinion, is the space its teeth must leave free. One tooth
// space is cut that way (the part of the sweep inside a one-pitch wedge
// around the gap at -90 degrees) and repeated round the gear, so every tooth
// is identical and the gear is balanced. The generating rack reaches 1.25 m
// past the pitch line, cutting the root clearance for the real rack's 1 m
// addendum.
module tooth_space_2d() {
    intersection() {
        rotate(-90) polygon([[0, 0], polar(2 * r_tip, -180 / gz), polar(2 * r_tip, 180 / gz)]);
        for (phi = [-60:1:60])
            rotate(-phi) translate([r_p * phi * PI / 180, 0]) {
                for (k = [-4:4]) rack_tooth_2d(k * pitch, -r_p + 1.25 * m);
                translate([-5 * pitch, -r_p - 1.25 * m - 3]) square([10 * pitch, 3]);
            }
    }
}

// The clearance is split between the gear and the rack: each is c/2 under
// the generated size, so they are c apart and both keep sturdy teeth.
module pinion_2d() {
    offset(delta = -c / 2) difference() {
        circle(r = r_tip, $fn = 96);
        for (k = [0:gz - 1]) rotate(k * 360 / gz) tooth_space_2d();
    }
}

// ---------------------------------------------------------------- decor

// The name runs round inside the rim border (for the flower, inside its
// centre disc); the pattern fills the rest of the face out to the border,
// following the petals.
face_r = (style == "flower" ? flower_in : R) - rim_w - 1;
decor_r0 = is_launcher ? r_tip + 2 : r_w + 1.5;

has_name = len(name) > 0;
name_r = face_r - name_size / 2;
name_pitch = 0.78;
name_s = has_name ? min(name_size, PI * name_r * 0.8 / (len(name) * name_pitch)) : 0;
name_ok = has_name && name_s >= 1.5 && name_r - name_s > decor_r0 + 1;
decor_r1 = name_ok ? name_r - name_s / 2 - 1.5 : R - rim_w - 1;
pattern_ok = pattern != "none" && decor_r1 - decor_r0 >= 3;

function polar(r, a) = [r * cos(a), r * sin(a)];

module arc_band(r0, r1, a0, a1, n = 24) {
    polygon(concat([for (i = [0:n]) polar(r1, a0 + (a1 - a0) * i / n)],
                   [for (i = [n:-1:0]) polar(r0, a0 + (a1 - a0) * i / n)]));
}

// Half of the pattern: sel 0 is the pattern colour, sel 1 the second one.
// Each half is itself spread evenly around the axis.
module pattern_2d(sel) {
    intersection() {
        offset(delta = -(rim_w + 1)) outline_2d();
        pattern_full_2d(sel);
    }
}

module pattern_full_2d(sel) {
    r0 = decor_r0; r1 = decor_r1;
    if (pattern == "spiral") {
        arms = 4; sweep = 240; w = 360 / arms / 2; n = 64; r2 = r0 + (r1 - r0) * 1.2;
        intersection() {
            circle(r = r1);
            for (k = [sel:2:arms - 1]) rotate(k * 360 / arms)
                polygon(concat(
                    [for (i = [0:n]) polar(r0 + (r2 - r0) * i / n, sweep * i / n)],
                    [for (i = [n:-1:0]) polar(r0 + (r2 - r0) * i / n, sweep * i / n + w)]));
        }
    } else if (pattern == "rays") {
        rays = 8;
        for (k = [sel:2:rays - 1]) rotate(k * 360 / rays)
            arc_band(r0, r1, 0, 180 / rays, 8);
    } else if (pattern == "rings") {
        // Seven equal bands: 0 and 4 in one colour, 2 and 6 in the other.
        s = (r1 - r0) / 7;
        for (k = [2 * sel, 2 * sel + 4])
            difference() { circle(r = r0 + s * (k + 1)); circle(r = r0 + s * k); }
    } else if (pattern == "dots") {
        span = r1 - r0;
        // The flower's dots sit on its petals, six to a ring.
        ring = style == "flower" ? [[0.3, 6, 0], [0.75, 6, 0]][sel]
                                 : [[0.3, 6, 0], [0.75, 12, 15]][sel];
        rr = r0 + span * ring[0];
        d = min(span * 0.36, 2 * PI * rr / ring[1] * 0.55);
        for (k = [0:ring[1] - 1]) rotate(ring[2] + k * 360 / ring[1])
            translate([rr, 0]) circle(d = d, $fn = 32);
    }
}

module name_copy_2d() {
    step = name_s * name_pitch / name_r * 180 / PI;
    n = len(name);
    for (i = [0:n - 1]) {
        a = 90 + ((n - 1) / 2 - i) * step;
        translate(polar(name_r, a)) rotate(a - 90)
            text(name[i], size = name_s, font = font, halign = "center", valign = "center");
    }
}

module name_2d() {
    for (k = [0, 1]) rotate(k * 180) name_copy_2d();
}

// A decor layer, inlay deep, under the face (one piece) or the dome (launcher).
module decor_zone() {
    if (is_launcher) {
        t = inlay * sqrt(2);
        ra = decor_r0 - 1; rb = R;
        intersection() {
            // The band runs 1 mm above the dome and is trimmed by the solid,
            // so the dome surface is not computed twice.
            rotate_extrude() polygon([[ra, H_r + R - ra - t], [rb, H_r + R - rb - t],
                                      [rb, H_r + R - rb + 1], [ra, H_r + R - ra + 1]]);
            translate([0, 0, under_h]) linear_extrude(H_f - under_h) children();
        }
    } else
        translate([0, 0, H_r - inlay]) linear_extrude(inlay + 1) children();
}

// ---------------------------------------------------------------- the top (use orientation)

module ring_profile_2d() {
    polygon([[R + c, under_h], [R + c + ring_w, under_h], [R + c + ring_w, H_r],
             [R + c, H_r], [R + c, zc + g], [R + c - g, zc], [R + c, zc - g]]);
}

module top_solid() {
    if (is_launcher) {
        outline(0, H_f) rotate_extrude() profile_2d();
        translate([0, 0, H_f - 0.01]) linear_extrude(L_g + 0.01) pinion_2d();
    } else difference() {
        union() {
            difference() {
                outline(0, H_r) rotate_extrude() profile_2d();
                // The 45-degree wall runs on past the stem's foot, so the stem
                // meets it on a slope and leaves no flat sliver.
                rotate_extrude() polygon([[0, z_wb - 1], [r_s - 1, z_wb - 1], [r_w, H_r],
                                          [r_w + 1, H_r + 1], [0, H_r + 1]]);
            }
            translate([0, 0, z_wb - 1.5]) linear_extrude(D + 1.5) stem_2d();
        }
        if (is_ring) rotate_extrude() offset(delta = c) ring_profile_2d();
    }
}

module tip_zone() { translate([0, 0, -1]) cylinder(r = R + 2, h = tip_h + 1); }

module rim_zone() {
    translate([0, 0, under_h]) linear_extrude(H_r - under_h)
        difference() { offset(delta = 2) outline_2d(); offset(delta = -rim_w) outline_2d(); }
}

module stem_zone() {
    if (is_launcher) translate([0, 0, H_f]) cylinder(r = r_tip + 1, h = L_g + 1);
    else translate([0, 0, z_wb - 1]) cylinder(r = r_s + 1, h = D + 2);
}

module decor_all() {
    if (pattern_ok) { decor_zone() pattern_2d(0); decor_zone() pattern_2d(1); }
    if (name_ok) decor_zone() name_2d();
}

module top_parts() {
    color(body_color) difference() {
        top_solid(); tip_zone(); rim_zone(); stem_zone(); decor_all();
    }
    if (pattern_ok) {
        color(pattern_color) intersection() { top_solid(); decor_zone() pattern_2d(0); }
        color(pattern2_color) intersection() { top_solid(); decor_zone() pattern_2d(1); }
    }
    if (name_ok) color(name_color) intersection() { top_solid(); decor_zone() name_2d(); }
    color(rim_color) intersection() { top_solid(); rim_zone(); }
    color(stem_color) intersection() { top_solid(); stem_zone(); }
    color(tip_color) intersection() { top_solid(); tip_zone(); }
}

// Print orientation: flipped, face (or gear) on the bed.
module print_top() { translate([0, 0, H_tot]) rotate([180, 0, 0]) children(); }

// ---------------------------------------------------------------- launcher (print orientation)

y_tipl = -r_p + m;                   // rack tooth tips
y_rootl = -r_p - 1.25 * m;           // rack tooth roots
y_backl = y_rootl - 5;               // back of the rack
notch = 1.8;                         // 45-degree notch under the rack's back edge
x_h = r_ch + 14;                     // housing half-length along the rack
y_h0 = y_backl - 3;                  // housing back
k0 = -ceil(pull_length / pitch);     // first tooth at the rack's tail
x_tail = k0 * pitch - pitch;
ring_ro = 12;
ring_ri = 7.5;
x_rc = x_h + 4 + ring_ro;            // pull ring centre
y_rc = (y_backl + y_rootl) / 2;

// Extrude a (y, z) profile along x.
module extrude_x(x0, x1) {
    translate([x0, 0, 0]) rotate([90, 0, 90]) linear_extrude(x1 - x0) children();
}

// The rack's cross-section: printed on the bed, with a 45-degree notch under
// its back edge that rides over the housing's lip, so it cannot drop out of
// the open (bed-side) face of the channel in use.
module rack_section_2d() {
    yb = y_backl + c / 2;
    yt = y_tipl - c / 2;
    polygon([[yb + notch, 0], [yt, 0], [yt, h_r], [yb, h_r], [yb, notch]]);
}

module rack() {
    intersection() {
        linear_extrude(h_r) offset(delta = -c / 2) union() {
            translate([x_tail, y_backl]) square([x_rc - x_tail, y_rootl - y_backl + 0.01]);
            for (k = [k0:1]) rack_tooth_2d(k * pitch, y_tipl);
        }
        extrude_x(x_tail - 1, x_rc) rack_section_2d();
    }
    // Pull ring.
    linear_extrude(h_r) difference() {
        translate([x_rc, y_rc]) circle(r = ring_ro);
        translate([x_rc, y_rc]) circle(r = ring_ri);
    }
}

module housing_2d() {
    offset(r = 3) offset(delta = -3) union() {
        circle(r = r_ch + 3);
        translate([-x_h, y_h0]) square([2 * x_h, -y_h0]);
        translate([-10, 0]) square([20, R + 25]);
    }
}

module launcher() {
    difference() {
        linear_extrude(t_l) housing_2d();
        translate([0, 0, -1]) cylinder(r = r_ch, h = t_l + 2);
        extrude_x(-x_h - 5, x_h + 5) offset(delta = c) rack_section_2d();
    }
}

// ---------------------------------------------------------------- output

module plate() {
    if (part == "all" || part == "top") print_top() top_parts();
    if (is_ring && (part == "all" || part == "ring"))
        print_top() color(ring_color) rotate_extrude() ring_profile_2d();
    if (is_launcher && (part == "all" || part == "launcher")) color(launcher_color) launcher();
    if (is_launcher && (part == "all" || part == "rack")) color(rack_color) rack();
}

if (display == "show") translate([0, 0, H_tot]) rotate([180, 0, 0]) plate();
else plate();
