// Bag clip — one piece, printed flat and open. A thin flexure loop at one end
// joins the two arms; at the other end the lower arm turns up into a post with
// a hook lip that snaps over the tip of the upper arm. Squeeze the arms
// together until the tip clicks under the lip; push the tab on the post
// outwards to release.
//
// The arms are drawn in their closed position and the upper arm is then
// swung open by open_angle about the centre of the loop, which is how the
// part prints: the loop is relaxed when open and bends when latched.
//
// Written to the MakerWorld Parametric Model Maker customizer conventions so
// the same file works unchanged on MakerWorld and in ScadBuddy.
//
// The colour parameters are the extruder order: clip_color is extruder 1,
// text_color is extruder 2 (only used when text is set).

/* [Clip] */

// Overall length of the closed clip in mm, loop to latch tab
length = 100; // [50:5:200]

// Width of the arms across the bag in mm (the clip prints on its side, so this is the print height)
width = 10; // [8:1:20]

// Thickness of each arm in mm (also sets the text height)
thickness = 6; // [4:0.5:10]

// Thickness in mm of the flexure loop that acts as the hinge (thinner bends easier, fatigues sooner)
hinge_t = 1.2; // [0.8:0.1:2]

// Play in mm between the hook lip and the tip of the upper arm when latched (raise if it will not latch)
latch_tol = 0.3; // [0.1:0.05:0.6]

// Jaw faces: flat, or interlocking waves that grip better
style = "flat"; // [flat:Flat, wave_grip:Wave grip]

/* [Text] */

// Text raised 0.6 mm on the lower arm's upward face as printed (empty for none)
text = ""; // 24

// Typeface
font = "DejaVu Sans:style=Bold"; // font

/* [Colors] */

// Clip colour (extruder 1)
clip_color = "#F2B705"; // color

// Text colour (extruder 2)
text_color = "#1E1E1E"; // color

/* [Hidden] */

$fn = 64;

// How far the upper arm is swung open for printing, in degrees.
open_angle = 20;

// Gap between the jaws at the loop when closed. Also keeps the arms from
// fusing next to the loop while printing open.
pivot_gap = 1.0;

// Hook lip: how far it reaches back over the upper arm, and its thickness.
lip_len = 1.6;
lip_t = 2;

// Latch post thickness, and the release tab on its outside.
post_w = 2.4;
tab_w = 2.5;
tab_h = 5;

// Wave grip: amplitude and pitch of the jaw waves.
wave_amp = 0.8;
wave_pitch = 4;

// Raised text height.
text_h = 0.6;

t = thickness;
g = pivot_gap;
R = t + g / 2;                          // loop outer radius
C = [0, t + g / 2];                     // loop centre = pivot
L = length - R - post_w - tab_w;        // lower arm, loop face to post
Lu = L - latch_tol;                     // upper arm length
y_lip = 2 * t + latch_tol;              // underside of the hook lip
y_top = y_lip + lip_t + lip_len;        // top of the post
wave = style == "wave_grip";

// Jaw wave region: starts far enough from the loop that the open arms stay
// clear of each other, stops short of the latch; whole periods only.
wx0 = (2 * wave_amp + 1) / tan(open_angle) + 2;
wn = max(0, floor((Lu - 4 - wx0) / wave_pitch));
wx1 = wx0 + wn * wave_pitch;

function wave_y(x) = (wave && wn > 0 && x > wx0 && x < wx1)
    ? wave_amp * sin(360 * (x - wx0) / wave_pitch) : 0;

// Sample points along a jaw face, left to right.
function jaw_xs(x_end) = wave && wn > 0
    ? concat([0], [for (i = [0 : wn * 16]) wx0 + i * wave_pitch / 16], [x_end])
    : [0, x_end];

module lower_arm_2d() {
    xs = jaw_xs(L);
    polygon(concat([[0, 0], [L, 0]],
                   [for (i = [len(xs) - 1 : -1 : 0]) [xs[i], t + wave_y(xs[i])]]));
}

// Upper arm in its closed position, with a lead-in chamfer on the corner of
// the tip that meets the hook lip.
module upper_arm_2d() {
    xs = jaw_xs(Lu - 1);
    polygon(concat([for (x = xs) [x, t + g + wave_y(x)]],
                   [[Lu, t + g + 1], [Lu, 2 * t + g], [0, 2 * t + g]]));
}

// Thin loop from the lower arm round the back to the (opened) upper arm.
// Runs a few degrees into each arm so the joints are solid.
module loop_2d() {
    a0 = 90 + open_angle - 4;
    a1 = 274;
    n = 48;
    translate(C) polygon(concat(
        [for (i = [0 : n]) R * [cos(a0 + (a1 - a0) * i / n), sin(a0 + (a1 - a0) * i / n)]],
        [for (i = [n : -1 : 0]) (R - hinge_t) * [cos(a0 + (a1 - a0) * i / n), sin(a0 + (a1 - a0) * i / n)]]));
}

// Post, hook lip with a sloped top as a lead-in, and the release tab.
module latch_2d() {
    translate([L, 0]) square([post_w, y_top]);
    polygon([[L - lip_len, y_lip], [L + 0.01, y_lip],
             [L + 0.01, y_top], [L - lip_len, y_lip + 0.6]]);
    r = 1;
    hull() for (x = [L + post_w - 0.01, L + post_w + tab_w - r])
        for (y = [y_top - tab_h + r, y_top - r])
            translate([x, y]) circle(r = r);
}

module clip_2d() {
    lower_arm_2d();
    latch_2d();
    loop_2d();
    translate(C) rotate(open_angle) translate(-C) upper_arm_2d();
}

// Text band on the lower arm, clear of the waves and of the loop and latch.
band_y0 = 0.8;
band_y1 = t - (wave ? wave_amp : 0) - 0.8;
band_x0 = 3;
band_x1 = L - lip_len - 2;
n_chars = len(text);
text_size = n_chars == 0 ? 0
    : min((band_y1 - band_y0) * 0.95, (band_x1 - band_x0) / (n_chars * 0.72));

color(clip_color)
    linear_extrude(height = width)
        clip_2d();

if (n_chars > 0)
    color(text_color)
        translate([0, 0, width])
            linear_extrude(height = text_h)
                intersection() {
                    translate([(band_x0 + band_x1) / 2, (band_y0 + band_y1) / 2])
                        text(text, size = text_size, font = font,
                             halign = "center", valign = "center");
                    translate([band_x0, band_y0])
                        square([band_x1 - band_x0, band_y1 - band_y0]);
                }
