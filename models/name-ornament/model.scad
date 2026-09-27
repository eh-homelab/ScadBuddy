// Christmas name ornament — a flat ornament in one of five shapes (bauble,
// star, tree, heart, snowflake disc) carrying a name and an optional year,
// with a hanging loop or hole and an optional contrasting border.
//
// Written to the MakerWorld Parametric Model Maker customizer conventions so
// the same file works unchanged on MakerWorld and in ScadBuddy.
//
// The three colour parameters are the extruder order: base_color is extruder
// 1 (the ornament body), text_color is extruder 2 (name and year),
// accent_color is extruder 3 (border, bauble cap, tree star, hanging loop,
// snowflakes). A shape with no accent geometry — e.g. a heart with a hole
// and no border — prints in two colours.

/* [Text] */

// Name on the ornament
name = "Ava"; // 12

// Typeface (the app fills this dropdown from the fonts installed in the image)
font = "Lobster Two:style=Bold"; // font

// Optional second line under the name, e.g. a year; leave empty for none
year = ""; // 12

// Requested name size in mm; shrinks automatically when the name would not fit the shape
text_size = 14; // [6:1:30]

// Raised letters stand on the face; inlay sets them flush into it (the border follows)
text_style = "raised"; // [raised:Raised, inlay:Inlay (flush)]

/* [Shape] */

// Outline of the ornament
shape = "bauble"; // [bauble:Bauble, star:Star, tree:Tree, heart:Heart, snowflake_disc:Snowflake disc]

// Height of the ornament body in mm (a hanging loop adds to it)
size = 70; // [40:5:120]

// Thickness of the body in mm
thickness = 3; // [2:0.5:6]

// A loop above the top, or a hole through the body
hanger = "loop"; // [loop:Loop, hole:Hole]

// Contrasting border around the outline
border = true;

/* [Colors] */

// Ornament body (extruder 1)
base_color = "#B22222"; // color

// Name and year (extruder 2)
text_color = "#FFFFFF"; // color

// Border, bauble cap, tree star, hanging loop, snowflakes (extruder 3)
accent_color = "#D4AF37"; // color

/* [Hidden] */

$fn = 64;

S = size;
t = thickness;

// Raised text / border height above the face.
relief = 1.2;
// Inlay pocket depth (the inlaid parts fill it flush with the face).
inlay_depth = min(1.0, t / 2);

// Hanger: hole diameter and the wall of the loop around it.
hole_d = 5;
ring_wall = 2.4;
ring_ro = hole_d / 2 + ring_wall;

// Border width, and the clearance the text keeps from the border / edge.
border_w = max(1.2, 0.035 * S);
bw = border ? border_w : 0;
text_gap = border ? 0.8 : 1.2;

// ---------------------------------------------------------------------------
// Text width estimate. OpenSCAD's textmetrics() is still experimental and
// disabled on MakerWorld, so the fit uses per-glyph advance widths measured
// at size 10 (ASCII 32..126) with textmetrics() offline. Other faces use the
// DejaVu Sans Bold table, which is on the wide side.

ADV_LOBSTER_BOLD = [2.99, 3.69, 4.85, 7.74, 5.35, 8.83, 9.31, 2.67, 3.88, 3.89, 5.56, 6.15, 3.25, 4.43, 3.29, 7.31, 8.29, 4.63, 7.08, 7.43, 7.13, 7.19, 7.67, 6.57, 7.65, 7.65, 3.76, 3.78, 4.97, 6.85, 5.03, 5.83, 7.83, 8.38, 9.43, 7.01, 9.82, 6.21, 7.75, 8.24, 9.64, 5.6, 6.03, 9.01, 6.29, 10.94, 9.97, 9.68, 8.36, 9.68, 9.08, 7.35, 9.33, 10.71, 9.9, 14.01, 9.01, 9.01, 7.72, 4.11, 7.31, 4.11, 5.85, 6.13, 2.79, 7.43, 6.17, 5.32, 7.42, 5.51, 3.71, 6.97, 7.21, 3.86, 3.26, 7.42, 3.79, 10.64, 7.21, 6.68, 6.5, 6.75, 5.22, 5.46, 3.92, 7.14, 5.75, 9.18, 7.49, 6.69, 5.94, 3.96, 3.26, 3.96, 6.08];
ADV_DEJAVU_BOLD = [4.84, 6.33, 7.24, 11.64, 9.66, 13.92, 12.11, 4.25, 6.35, 6.35, 7.26, 11.64, 5.28, 5.76, 5.28, 5.07, 9.66, 9.66, 9.66, 9.66, 9.66, 9.66, 9.66, 9.66, 9.66, 9.66, 5.55, 5.55, 11.64, 11.64, 11.64, 8.06, 13.89, 10.75, 10.59, 10.19, 11.53, 9.49, 9.49, 11.4, 11.62, 5.17, 5.17, 10.76, 8.85, 13.82, 11.62, 11.81, 10.18, 11.81, 10.69, 10, 9.47, 11.28, 10.75, 15.32, 10.71, 10.06, 10.07, 6.35, 5.07, 6.35, 11.64, 6.94, 6.94, 9.37, 9.94, 8.23, 9.94, 9.42, 6.04, 9.94, 9.89, 4.76, 4.76, 9.24, 4.76, 14.47, 9.89, 9.54, 9.94, 9.94, 6.85, 8.27, 6.64, 9.89, 9.05, 12.83, 8.96, 9.05, 8.08, 9.89, 5.07, 9.89, 11.64];

function starts_with(s, p) =
    len(s) >= len(p) && [for (i = [0:len(p) - 1]) s[i]] == [for (i = [0:len(p) - 1]) p[i]];

ADV = starts_with(font, "Lobster Two") ? ADV_LOBSTER_BOLD : ADV_DEJAVU_BOLD;

// Advance of one character at size 10; anything outside ASCII counts as 'N'.
function adv10(c) = let(o = ord(c)) (o >= 32 && o <= 126) ? ADV[o - 32] : ADV[78 - 32];
function sum_list(v, i = 0) = i >= len(v) ? 0 : v[i] + sum_list(v, i + 1);
function est_width(s, sz) = sz / 10 * sum_list([for (i = [0:len(s) - 1]) adv10(s[i])]);

// Largest size <= requested whose estimated width fits w and height fits h.
// Glyph boxes run to about 1.2 x size in the script faces (ascender+descender).
function fit_size(s, want, w, h) =
    len(s) == 0 ? want : max(1, min(want, h / 1.2, want * 0.95 * max(w, 1) / est_width(s, want)));

// ---------------------------------------------------------------------------
// Shape geometry. Every body stands with its bottom on y=0 and its top at
// y=S, centred on x=0.

// Bauble: a ball with a rectangular cap on top.
cap_h = max(0.15 * S, hole_d + 4);
ball_d = (S - cap_h) / 0.96;
cap_w = max(0.3 * ball_d, hole_d + 6);
ball_c = ball_d / 2;

// Star: five arms, each the hull of a rounded tip and the inner pentagon.
star_rt = 0.04 * S;
star_rc = (S - 2 * star_rt) / (1 + sin(54));
star_r = star_rc + star_rt;
star_c = star_rc * sin(54) + star_rt;

// Heart: a square on its corner with a half-circle on each upper edge.
heart_a = S / (0.75 * sqrt(2) + 0.5);
heart_cusp = sqrt(2) * heart_a;

// Snowflake disc: a ring of small snowflakes inside the rim.
flake_r = 0.055 * S;
flake_rc = S / 2 - bw - 1.2 - flake_r;
disc_inner = flake_rc - flake_r - 1;

// Tree tiers: [bottom y, top y, bottom width, top width] as fractions of S.
tree_rc = 0.025 * S;
TIERS = [[0.10, 0.40, 0.90, 0.56], [0.35, 0.63, 0.70, 0.40], [0.58, 0.86, 0.50, 0]];
tree_star_r = 0.16 * S;

// Per-shape text layout: [name y solo, name y with year, name width,
// name height, year y, year width, year height], y from the body bottom.
LAYOUT =
    shape == "bauble" ? [ball_c, ball_c + 0.1 * ball_d, 0.78 * ball_d, 0.35 * ball_d,
                         ball_c - 0.22 * ball_d, 0.55 * ball_d, 0.16 * ball_d] :
    shape == "star"   ? [star_c + 0.1 * star_r, star_c + 0.12 * star_r, 1.0 * star_r, 0.34 * star_r,
                         star_c - 0.32 * star_r, 0.75 * star_r, 0.16 * star_r] :
    shape == "tree"   ? [0.27 * S, 0.43 * S, 0.56 * S, 0.22 * S,
                         0.2 * S, 0.56 * S, 0.12 * S] :
    shape == "heart"  ? [0.56 * S, 0.62 * S, 0.78 * S, 0.22 * S,
                         0.36 * S, 0.5 * S, 0.12 * S] :
                        [S / 2, S / 2 + 0.2 * disc_inner, 1.7 * disc_inner, 0.55 * disc_inner,
                         S / 2 - 0.5 * disc_inner, 1.2 * disc_inner, 0.28 * disc_inner];

has_year = len(year) > 0;
name_y = has_year ? LAYOUT[1] : LAYOUT[0];
name_sz = fit_size(name, text_size, LAYOUT[2] - 2 * bw, LAYOUT[3]);
year_sz = fit_size(year, 0.6 * text_size, LAYOUT[5] - 2 * bw, LAYOUT[6]);

// Hanger position. The loop's hole starts 1 mm below the attach point (the
// top of the body, or just above the heart's cusp) so the ring bites into
// the body; the hole mode punches the hole inside the body.
attach_y = shape == "heart" ? heart_cusp + 2 : S;
loop_y = attach_y + hole_d / 2 - 1;
hole_y =
    shape == "bauble" ? S - cap_h / 2 :
    shape == "star"   ? star_c + 0.6 * star_rc :
    shape == "tree"   ? 0.62 * S :
    shape == "heart"  ? heart_cusp - ring_ro :
                        S - ring_ro;
hanger_y = hanger == "loop" ? loop_y : hole_y;

// ---------------------------------------------------------------------------

module rounded_star(rc, rt, ri) {
    for (k = [0:4]) {
        a = 90 + 72 * k;
        hull() {
            rotate(a) translate([rc, 0]) circle(r = rt);
            polygon([[0, 0], ri * [cos(a - 36), sin(a - 36)], ri * [cos(a + 36), sin(a + 36)]]);
        }
    }
}

module tier(yb, yt, wb, wt) {
    r = tree_rc;
    hull() {
        for (s = [-1, 1]) translate([s * (wb / 2 - r), yb + r]) circle(r = r);
        if (wt > 2 * r) for (s = [-1, 1]) translate([s * (wt / 2 - r), yt - r]) circle(r = r);
        else translate([0, yt - r]) circle(r = r);
    }
}

module snowflake(r) {
    lw = max(0.8, 0.14 * r);
    for (k = [0:5]) rotate(60 * k) {
        translate([0, -lw / 2]) square([r, lw]);
        for (s = [-1, 1])
            translate([0.5 * r, 0]) rotate(s * 50) translate([0, -lw / 2]) square([0.42 * r, lw]);
    }
    circle(r = 1.2 * lw, $fn = 6);
}

// The body outline, including any accent solids (cap, tree star).
module body_2d() {
    if (shape == "bauble") {
        translate([0, ball_c]) circle(d = ball_d, $fn = 96);
    } else if (shape == "star") {
        translate([0, star_c]) rounded_star(star_rc, star_rt, 0.48 * star_r);
    } else if (shape == "tree") {
        for (ti = TIERS) tier(ti[0] * S, ti[1] * S, ti[2] * S, ti[3] * S);
        hull() {
            for (s = [-1, 1]) translate([s * (0.08 * S - tree_rc), tree_rc]) circle(r = tree_rc);
            translate([-0.08 * S, 0.12 * S]) square([0.16 * S, 0.01]);
        }
    } else if (shape == "heart") {
        a = heart_a;
        polygon([[0, 0], [a / sqrt(2), a / sqrt(2)], [0, a * sqrt(2)], [-a / sqrt(2), a / sqrt(2)]]);
        for (s = [-1, 1]) translate([s * a / (2 * sqrt(2)), 3 * a / (2 * sqrt(2))]) circle(d = a, $fn = 96);
    } else {
        translate([0, S / 2]) circle(d = S, $fn = 96);
    }
}

// Accent solids that are part of the silhouette, full thickness.
module top_2d() {
    if (shape == "bauble") {
        r = min(1.5, cap_w / 4);
        hull() {
            translate([-cap_w / 2, 0.96 * ball_d]) square([cap_w, 0.01]);
            for (s = [-1, 1]) translate([s * (cap_w / 2 - r), S - r]) circle(r = r);
        }
    } else if (shape == "tree") {
        rt = 0.025 * S;
        translate([0, S - tree_star_r]) rounded_star(tree_star_r - rt, rt, 0.5 * tree_star_r);
    }
}

module loop_ring_2d() {
    translate([0, loop_y]) difference() {
        circle(r = ring_ro);
        circle(d = hole_d);
    }
}

// What the hanger removes from the body: the whole loop disc, or the hole.
module hanger_cut_2d() {
    translate([0, hanger_y]) circle(r = hanger == "loop" ? ring_ro : hole_d / 2);
}

module base_region_2d() {
    difference() {
        body_2d();
        top_2d();
        hanger_cut_2d();
    }
}

module accent_solid_2d() {
    difference() {
        union() {
            top_2d();
            if (hanger == "loop") loop_ring_2d();
        }
        translate([0, hanger_y]) circle(d = hole_d);
    }
}

module border_2d() {
    if (border) difference() {
        base_region_2d();
        offset(r = -border_w) base_region_2d();
    }
}

// Area inside the border (or edge) that text and decoration may occupy.
module inner_2d() {
    offset(r = -(bw + text_gap)) base_region_2d();
}

module flakes_2d() {
    if (shape == "snowflake_disc") intersection() {
        for (k = [0:11]) translate([0, S / 2]) rotate(15 + 30 * k) translate([flake_rc, 0])
            rotate(-15 - 30 * k) snowflake(flake_r);
        offset(r = -(bw + 0.6)) base_region_2d();
    }
}

module trim_2d() {
    border_2d();
    flakes_2d();
}

module text_2d() {
    if (len(name) > 0) translate([0, name_y])
        text(name, size = name_sz, font = font, halign = "center", valign = "center");
    if (has_year) translate([0, LAYOUT[4]])
        text(year, size = year_sz, font = font, halign = "center", valign = "center");
}

// Text clipped to the free area and kept clear of the snowflakes.
module text_clipped_2d() {
    difference() {
        intersection() {
            text_2d();
            inner_2d();
        }
        offset(delta = 0.6) flakes_2d();
    }
}

inlay = text_style == "inlay";
top_z = inlay ? t - inlay_depth : t;
top_h = inlay ? inlay_depth : relief;

color(base_color)
    difference() {
        linear_extrude(height = t) base_region_2d();
        if (inlay) translate([0, 0, t - inlay_depth])
            linear_extrude(height = inlay_depth + 1) {
                text_clipped_2d();
                trim_2d();
            }
    }

color(text_color)
    translate([0, 0, top_z]) linear_extrude(height = top_h) text_clipped_2d();

color(accent_color) {
    linear_extrude(height = t) accent_solid_2d();
    translate([0, 0, top_z]) linear_extrude(height = top_h) trim_2d();
}
