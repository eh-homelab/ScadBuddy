// Spinning top — a wide disc carrying the pattern, an underside cone (or bowl)
// below it, and a spindle that runs through both: the stem you twist at the
// top and the tip it spins on at the bottom are one piece. The top face carries
// an inlaid pattern (spiral, rays or dots) in a second colour that turns into
// an optical swirl when it spins, and an optional name around the rim.
//
// Three pieces, all supportless:
//   disc    - printed face up, flat underside on the bed; the pattern is the
//             top layers.
//   cone    - printed upside down, flat top on the bed, so it narrows upwards.
//   spindle - printed standing on its flat stem end, tip up. A 45-degree
//             collar above the tip seats in a matching countersink in the
//             cone's apex, which centres the tip and carries the load.
// Push the spindle up through the cone and then the disc. Stem and tip are
// the same piece, so they are coaxial whatever the print tolerances.
//
// Everything is rotationally symmetric, and the pattern and the name are
// repeated around the axis, so each colour's centre of mass lies on the spin
// axis (verify.sh checks this).
//
// Written to the MakerWorld Parametric Model Maker customizer conventions so
// the same file works unchanged on MakerWorld and in ScadBuddy.
//
// The colour parameters are the extruder order: body_color is extruder 1 (disc
// and cone), pattern_color extruder 2 (the pattern and the name), stem_color
// extruder 3 (the spindle).

/* [Top] */

// Disc diameter in mm
diameter = 50; // [30:5:80]

// Body shape
style = "classic_cone"; // [classic_cone:Classic cone, ufo_disc:UFO disc, flower:Flower]

// How far the stem stands above the disc's face, in mm
stem_length = 20; // [10:1:35]

// Stem (spindle) diameter in mm
stem_d = 6; // [4:0.5:10]

// Tip shape: a ball tip is safer and wanders around a table; a point spins in place longer
tip = "ball"; // [point:Point, ball:Ball]

// Clearance per side between the spindle and the holes in the disc and cone, in mm (lower is tighter)
fit_clearance = 0.15; // [0:0.05:0.4]

/* [Decor] */

// Pattern inlaid in the top face, in the pattern colour
pattern = "spiral"; // [spiral:Spiral, rays:Rays, dots:Dots, none:None]

// Name around the rim, printed twice, opposite each other (leave empty for none)
name = ""; // 12

/* [Colors] */

// Disc and cone colour (extruder 1)
body_color = "#1E88E5"; // color

// Pattern and name colour (extruder 2)
pattern_color = "#FFEB3B"; // color

// Spindle colour: stem and tip (extruder 3)
stem_color = "#E53935"; // color

/* [Hidden] */

$fn = 96;

// true renders the top assembled, tip on z=0 — used by verify.sh to check the
// balance. The printable plate layout is the default.
assembled = false;

// Depth of the pattern and name inlay: three 0.2 mm layers.
inlay = 0.6;
// Gap between the pieces on the plate.
part_gap = 6;
// 45-degree chamfer on the disc's top edge, and on its bottom edge where it
// overhangs the cone.
face_chamfer = 0.8;
under_chamfer = 1;
// Collar above the tip: how far it stands out from the spindle, and the wall
// of cone left around the countersink it seats in.
collar = 1.2;
apex_wall = 1.6;
// Chamfer on the spindle's flat stem end, which is its printing base.
stem_chamfer = 0.8;
// Name letter height, before shrinking to fit half the circumference.
name_size = 3.5;
font = "DejaVu Sans:style=Bold";

R = diameter / 2;
r_s = stem_d / 2;                     // spindle radius
r_h = r_s + fit_clearance;            // hole radius in the disc and cone
r_c = r_s + collar;                   // collar radius
tip_r = tip == "ball" ? min(r_c, max(2, diameter * 0.06)) : 0.6;

// Spindle, in use orientation, tip on z=0: the tip rises to the collar at z_c,
// then a 45-degree seat narrows to the spindle.
z_c = max(r_c * 1.5, 2 * tip_r + 1);
z_seat = z_c + collar;

// Cone apex (flat, with the countersink) at z_c; disc underside at under_h.
r_apex = r_c + fit_clearance + apex_wall;
rim_t = style == "ufo_disc" ? max(3.5, diameter * 0.08) : max(4, diameter * 0.1);
under_h = max(style == "ufo_disc" ? diameter * 0.16 : diameter * 0.34, z_seat + 3);
H = under_h + rim_t;
b = style == "ufo_disc" ? 0 : under_chamfer;

// Flower outline: a centre disc and six round petals reaching the full diameter.
petals = 6;
petal_r = R * 0.3;
flower_in = R * 0.78;

// ---------------------------------------------------------------- body

// Half cross-sections in (r, z), use orientation.
module cone_profile_2d() {
    if (style == "ufo_disc")
        // Saucer: a shallow bowl, vertical at the rim, flat at the apex.
        polygon(concat(
            [[0, z_c], [r_apex, z_c]],
            [for (a = [90:-5:0]) [r_apex + (R - r_apex) * cos(a),
                                  under_h - (under_h - z_c) * sin(a)]],
            [[0, under_h]]));
    else
        polygon([[0, z_c], [r_apex, z_c], [R - b, under_h], [0, under_h]]);
}

module disc_profile_2d() {
    c = face_chamfer;
    polygon([[0, under_h], [R - b, under_h], [R, under_h + b],
             [R, H - c], [R - c, H], [0, H]]);
}

module flower_2d() {
    offset(r = -1) offset(r = 1) union() {
        circle(r = flower_in);
        for (k = [0:petals - 1]) rotate(k * 360 / petals)
            translate([R - petal_r, 0]) circle(r = petal_r, $fn = 48);
    }
}

module outline(h0, h1) {
    if (style == "flower")
        intersection() {
            children();
            translate([0, 0, h0 - 1]) linear_extrude(h1 - h0 + 2) flower_2d();
        }
    else children();
}

module spindle_hole(h0, h1) {
    translate([0, 0, h0 - 1]) cylinder(r = r_h, h = h1 - h0 + 2);
}

module cone_piece() {
    difference() {
        outline(z_c, under_h) rotate_extrude() cone_profile_2d();
        spindle_hole(z_c, under_h);
        // Countersink the collar seats in, offset by the clearance.
        translate([0, 0, z_c - 0.001])
            cylinder(r1 = r_c + fit_clearance, r2 = r_h, h = collar + 0.001);
    }
}

// ---------------------------------------------------------------- face decor

// The face area the decor may use: out to just inside the rim chamfer (or the
// flower's centre disc), in to just outside the spindle hole.
face_r = (style == "flower" ? flower_in : R - face_chamfer) - 1.5;
decor_r0 = r_h + 2;

has_name = len(name) > 0;
name_r = face_r - name_size / 2;                 // centre line of the letters
// Letters are spaced by their DejaVu Sans Bold advance widths (measured at
// size 10 with textmetrics(), ASCII 32..126; anything else counts as 'N'),
// and shrunk so each copy of the name fits in 80 % of half the circumference.
ADV10 = [4.84, 6.33, 7.24, 11.64, 9.66, 13.92, 12.11, 4.25, 6.35, 6.35, 7.26, 11.64, 5.28, 5.76, 5.28, 5.07, 9.66, 9.66, 9.66, 9.66, 9.66, 9.66, 9.66, 9.66, 9.66, 9.66, 5.55, 5.55, 11.64, 11.64, 11.64, 8.06, 13.89, 10.75, 10.59, 10.19, 11.53, 9.49, 9.49, 11.4, 11.62, 5.17, 5.17, 10.76, 8.85, 13.82, 11.62, 11.81, 10.18, 11.81, 10.69, 10, 9.47, 11.28, 10.75, 15.32, 10.71, 10.06, 10.07, 6.35, 5.07, 6.35, 11.64, 6.94, 6.94, 9.37, 9.94, 8.23, 9.94, 9.42, 6.04, 9.94, 9.89, 4.76, 4.76, 9.24, 4.76, 14.47, 9.89, 9.54, 9.94, 9.94, 6.85, 8.27, 6.64, 9.89, 9.05, 12.83, 8.96, 9.05, 8.08, 9.89, 5.07, 9.89, 11.64];
function adv10(c) = let(o = ord(c)) (o >= 32 && o <= 126) ? ADV10[o - 32] : ADV10[78 - 32];
function sum_adv(i) = i <= 0 ? 0 : sum_adv(i - 1) + adv10(name[i - 1]);
name_adv10 = sum_adv(len(name));
name_s = has_name ? min(name_size, PI * name_r * 0.8 * 10 / name_adv10) : 0;
name_ok = has_name && name_s >= 1.5 && name_r - name_s > decor_r0 + 1;
decor_r1 = name_ok ? name_r - name_s / 2 - 1.5 : face_r;
pattern_ok = pattern != "none" && decor_r1 - decor_r0 >= 3;
decor_ok = pattern_ok || name_ok;

function polar(r, a) = [r * cos(a), r * sin(a)];

module arc_band(r0, r1, a0, a1, n = 24) {
    polygon(concat([for (i = [0:n]) polar(r1, a0 + (a1 - a0) * i / n)],
                   [for (i = [n:-1:0]) polar(r0, a0 + (a1 - a0) * i / n)]));
}

module pattern_2d() {
    r0 = decor_r0; r1 = decor_r1;
    if (pattern == "spiral") {
        // Four arms at half coverage, each sweeping 200 degrees outwards.
        // The arms run past r1 and are trimmed to it, so the pattern ends in
        // a clean circle.
        arms = 4; sweep = 240; w = 360 / arms / 2; n = 64; r2 = r0 + (r1 - r0) * 1.2;
        intersection() {
            circle(r = r1);
            for (k = [0:arms - 1]) rotate(k * 360 / arms)
                polygon(concat(
                    [for (i = [0:n]) polar(r0 + (r2 - r0) * i / n, sweep * i / n)],
                    [for (i = [n:-1:0]) polar(r0 + (r2 - r0) * i / n, sweep * i / n + w)]));
        }
    } else if (pattern == "rays") {
        rays = 8;
        for (k = [0:rays - 1]) rotate(k * 360 / rays)
            arc_band(r0, r1, 0, 180 / rays, 8);
    } else if (pattern == "dots") {
        span = r1 - r0;
        for (ring = [[0.3, 6, 0], [0.75, 12, 15]]) {
            rr = r0 + span * ring[0];
            d = min(span * 0.36, 2 * PI * rr / ring[1] * 0.55);
            for (k = [0:ring[1] - 1]) rotate(ring[2] + k * 360 / ring[1])
                translate([rr, 0]) circle(d = d, $fn = 32);
        }
    }
}

// One copy of the name, centred at the top (90 degrees), reading clockwise
// with the letters' tops pointing outwards.
module name_copy_2d() {
    k = name_s / 10;                              // advance scale
    total = k * name_adv10;
    for (i = [0:len(name) - 1]) {
        // Arc length from the start of the name to the middle of letter i.
        x = k * (sum_adv(i) + adv10(name[i]) / 2) - total / 2;
        a = 90 - x / name_r * 180 / PI;
        translate(polar(name_r, a)) rotate(a - 90)
            text(name[i], size = name_s, font = font, halign = "center", valign = "center");
    }
}

module decor_2d() {
    if (pattern_ok) pattern_2d();
    if (name_ok) for (k = [0, 1]) rotate(k * 180) name_copy_2d();
}

module disc_piece() {
    difference() {
        outline(under_h, H) rotate_extrude() disc_profile_2d();
        spindle_hole(under_h, H);
        if (decor_ok) translate([0, 0, H - inlay]) linear_extrude(inlay + 1) decor_2d();
    }
}

module decor_piece() {
    if (decor_ok) translate([0, 0, H - inlay]) linear_extrude(inlay) decor_2d();
}

// ---------------------------------------------------------------- spindle

spindle_len = H + stem_length;

module spindle() {
    ch = min(stem_chamfer, r_s / 3);
    rotate_extrude($fn = 48) intersection() {
        square([r_c, spindle_len]);
        union() {
            // Tip up to the collar.
            hull() {
                translate([0, tip_r]) circle(r = tip_r, $fn = 48);
                translate([0, z_c - 0.01]) square([r_c, 0.01]);
            }
            // Seat, spindle, and the chamfered flat stem end.
            polygon([[0, z_c], [r_c, z_c], [r_s, z_seat],
                     [r_s, spindle_len - ch], [r_s - ch, spindle_len], [0, spindle_len]]);
        }
    }
}

// ---------------------------------------------------------------- output

if (assembled) {
    color(body_color) { cone_piece(); disc_piece(); }
    color(pattern_color) decor_piece();
    color(stem_color) spindle();
} else {
    // Disc face up, flat underside on the bed.
    translate([0, 0, -under_h]) {
        color(body_color) disc_piece();
        color(pattern_color) decor_piece();
    }
    // Cone upside down, flat top on the bed.
    color(body_color) translate([2 * R + part_gap, 0, under_h]) rotate([180, 0, 0]) cone_piece();
    // Spindle standing on its stem end, tip up.
    color(stem_color) translate([3 * R + 2 * part_gap + r_c, 0, spindle_len])
        rotate([180, 0, 0]) spindle();
}
