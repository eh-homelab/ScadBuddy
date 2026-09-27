// Articulated critter — a print-in-place flexi animal: a head, a chain of body
// segments and a tail, joined by hinges that print already assembled. Pick a
// snake, dragon, lizard, fish or caterpillar; eyes, nostrils and an optional
// name (one letter per body segment) are flush inlays in the top face.
//
// How the hinges work (one per pair of neighbouring segments):
//   - the rear segment carries a C-shaped ring at its front;
//   - the front segment carries a barrel-shaped pin at its rear, on a neck
//     that runs out through the gap in the C;
//   - the pin is wider in the middle than the ring's hole is at the top and
//     bottom, so it cannot lift out, and wider than the gap in the C, so it
//     cannot slide out. Every overhang is 45 degrees, so nothing needs support;
//   - neighbouring segments are cut apart by V-shaped gaps that open by the
//     joint's bend angle, and every gap is at least `clearance` wide on every
//     layer (verify.sh measures this on the rendered geometry).
// The bottom 0.3 mm of every edge is set in, so first-layer squish cannot
// weld the joints.
//
// Prints flat, as it lies, with no supports.
//
// Written to the MakerWorld Parametric Model Maker customizer conventions so
// the same file works unchanged on MakerWorld and in ScadBuddy.
//
// The colour parameters are the extruder order: body_color is extruder 1,
// stripe_color 2, head_color 3, eye_color 4, detail_color 5, name_color 6.
// Colours that are equal merge into one part and one filament.

/* [Critter] */

// Which animal
animal = "dragon"; // [snake:Snake, dragon:Dragon, lizard:Lizard, fish:Fish, caterpillar:Caterpillar]

// Body segments between the head and the tail (fewer are made if they would be shorter than the hinges allow, see README)
segments = 7; // [3:1:20]

// Length from nose to tail tip in mm, laid out straight
length = 220; // [120:5:300]

// Body width in mm at its widest (legs, wings, fins and the head stick out beyond it)
width = 26; // [22:1:40]

// Thickness in mm (wings and fins are thinner)
thickness = 8; // [6:0.5:12]

// How it lies on the plate: straight, a gentle S-wave, or curled into a C (it prints and bends the same way)
pose = "wave"; // [straight:Straight, wave:Wave, curl:Curl]

/* [Name] */

// Name along the back, one letter per body segment; body segments are added to fit it (leave empty for none)
name = ""; // 12

// Typeface for the name (the app fills this dropdown from the fonts installed in the image)
font = "DejaVu Sans:style=Bold"; // font

/* [Hinges] */

// Gap per side in every hinge and between segments, in mm (raise it if the joints print fused; lower it if they are floppy)
clearance = 0.4; // [0.25:0.05:0.6]

/* [Colours] */

// Body segments, odd ones counting back from the head (extruder 1)
body_color = "#43A047"; // color

// Body segments, even ones: the stripes (extruder 2)
stripe_color = "#FDD835"; // color

// Head, with horns, antennae and so on (extruder 3)
head_color = "#FB8C00"; // color

// Whites of the eyes (extruder 4)
eye_color = "#FFFFFF"; // color

// Pupils, nostrils and mouth (extruder 5)
detail_color = "#212121"; // color

// Name letters (extruder 6)
name_color = "#1E88E5"; // color

/* [Hidden] */

$fn = 48;

// ---- hinge geometry (mm). The pin is a barrel: radius rp at the bed and at
// the top, rp + bulge in the middle, joined by 45-degree cones.
rp = 3.2;
bulge = 1.4;
ring_wall = 1.8;
// Half-angle of the gap in the C ring, and the most each joint bends each way.
slot_angle = 52;
bend = 22;
// Bottom edges are set in by this much (elephant-foot relief), top edges
// chamfered by this much in two steps.
foot = 0.3;
chamfer = 0.8;
chamfer_steps = 2;
// Inlay depth: three 0.2 mm layers.
inlay = 0.6;
// Rounding on the outline's convex corners.
round_r = 3;
// The largest bend the print pose puts into any one joint.
pose_bend = 12;

// Test hook for verify.sh: when > 0, render only the overlap of every pair of
// neighbouring segments' cross-sections, each grown by probe_gap / 2, at a set
// of heights. Empty (bar a sentinel cube) means every gap is >= probe_gap.
probe_gap = 0;
// Test hook for verify.sh: when not [0, 0], render (straight, unposed) the
// overlap of every segment with its front neighbour moved by [dx, 0, dz] —
// forwards out of the C ring, or up out of it. Geometry at every joint means
// the pin is captured in that direction.
probe_capture = [0, 0];

c = clearance;
T = thickness;
t = tan(bend / 2);
R = rp + c + bulge + ring_wall;                  // ring outer radius
neck_hw = (rp + c) * sin(slot_angle - bend) - c - 0.05;
P_min = 2 * R + c + 2;                           // shortest segment pitch
wmax = width / 2;
wmin = R + c + 2.5;                              // narrowest a jointed segment can be
low_h = min(T, max(2.4, T * 0.45));              // wings and fins

// ---- the animal

// Head length, head half-width and tail length, in multiples of wmax.
function head_len() = wmax * (animal == "snake" ? 2.3 : animal == "fish" ? 1.7
                             : animal == "caterpillar" ? 2.0 : 2.2);
function head_w() = wmax * (animal == "snake" ? 1.15 : animal == "caterpillar" ? 1.2
                           : animal == "dragon" ? 1.05 : animal == "fish" ? 1.0 : 1.0);
function tail_len() = wmax * (animal == "snake" ? 3.0 : animal == "lizard" ? 3.2
                             : animal == "dragon" ? 3.0 : animal == "fish" ? 1.9 : 1.3);

// Features by body segment (1 = just behind the head), for n segments.
function rear_leg(n) = max(3, round(n * 0.5) + 1);
function feat(k, n) =
    animal == "dragon" ? (n >= 3 ? (k == 1 ? "legs" : k == 2 ? "wings" : k == min(n, rear_leg(n)) ? "legs" : "none")
                        : n == 2 ? (k == 1 ? "wings" : "legs") : "wings")
  : animal == "lizard" ? (k == 1 ? "legs" : n >= 2 && k == max(2, min(n, round(n * 0.45) + 1)) ? "legs" : "none")
  : animal == "fish" ? (k == max(1, ceil(n * 0.35)) ? "dorsal" : n >= 2 && k == max(2, ceil(n * 0.65)) ? "anal" : "none")
  : animal == "caterpillar" ? "feet"
  : "none";
// Segment length as a multiple of the base pitch: legs and wings need room.
function factor(f) = f == "legs" ? 1.25 : f == "wings" ? 2.0 : 1;
function sum(v, i = 0) = i >= len(v) ? 0 : v[i] + sum(v, i + 1);
function sumf(n) = sum([for (k = [1:n]) factor(feat(k, n))]);

span_len = length - head_len() - tail_len();
function fits(n) = span_len / sumf(n) >= P_min;
function best_n(n) = n <= 1 ? 1 : fits(n) ? n : best_n(n - 1);

N = best_n(max(segments, len(name)));
p = max(P_min, span_len / sumf(N));
L = head_len() + tail_len() + p * sumf(N);       // == length unless it had to grow
feats = [for (k = [1:N]) feat(k, N)];
function f_at(k) = factor(feats[k - 1]);

// Joint k (k = 1 .. N + 1) sits between segment k - 1 and segment k; the head
// is segment 0, the tail segment N + 1. x runs from the tail tip (0) to the
// nose (L), along the straight spine.
J = [for (k = [0:N + 1]) k == 0 ? L : L - head_len() - p * sum([for (i = [1:k]) i < k ? f_at(i) : 0])];
function xc(k) = (J[k] + J[k + 1]) / 2;          // centre of body segment k

// Half-width of the body at joint j.
function w_at(j) = let(q = (j - 1) / N) max(wmin,
    animal == "snake" ? wmax * (1 - 0.35 * q * q)
  : animal == "fish" ? wmin + (wmax - wmin) * sin(180 * (0.3 + 0.7 * q))
  : animal == "caterpillar" ? wmax * 0.92
  : q < 0.5 ? wmax : wmax + (wmin - wmax) * (q - 0.5) / 0.5);

// ---- pose: segment k turns about joint k by pose_psi(k) relative to k - 1

wave_a = 360 / (N + 1);
wave_amp = min(28, pose_bend / (2 * sin(wave_a / 2)));
function theta(k) = pose == "wave" ? wave_amp * sin(wave_a * k) : 0;
function pose_psi(k) = pose == "curl" ? min(pose_bend, 270 / (N + 1))
                     : pose == "wave" ? theta(k) - theta(k - 1) : 0;

module posed(k) {
    if (k <= 0) children();
    else posed(k - 1) translate([J[k], 0]) rotate(pose_psi(k)) translate([-J[k], 0]) children();
}

// ---- 2D building blocks

BIG = 400;

// Everything behind joint j's V (for the segment behind it).
module behind_2d(j) {
    x0 = J[j] - c / 2;
    polygon([[x0, 0], [x0 - BIG * t, BIG], [-BIG, BIG], [-BIG, -BIG], [x0 - BIG * t, -BIG]]);
}
// Everything ahead of joint j's V (for the segment in front of it).
module ahead_2d(j) {
    x0 = J[j] + c / 2;
    polygon([[x0, 0], [x0 + BIG * t, BIG], [BIG, BIG], [BIG, -BIG], [x0 + BIG * t, -BIG]]);
}
// The part of the plane segment k may occupy.
module region_2d(k) {
    intersection() {
        if (k >= 1) behind_2d(k); else translate([-BIG, -BIG]) square(2 * BIG);
        if (k <= N) ahead_2d(k + 1); else translate([-BIG, -BIG]) square(2 * BIG);
    }
}

// Hinge circles are polygons. Anything cut round a moving part is
// circumscribed (its flats at the nominal radius, not its corners), so the gap
// never shrinks below `clearance` whatever angle the parts sit at.
fn_j = 96;
sec = 1 / cos(180 / fn_j);
module notch_2d(j) { translate([J[j], 0]) circle(r = (R + c) * sec, $fn = fn_j); }

module rounded_2d(r = round_r) { offset(r = r) offset(r = -r) children(); }
// Softer rounding on the body segments, which are otherwise square-shouldered.
function body_round(k) = k == 0 || k == N + 1 ? 1.5 : min(round_r, w_at(k) * 0.3);

module capsule_2d(x0, r0, x1, r1) {
    hull() { translate([x0, 0]) circle(r = r0); translate([x1, 0]) circle(r = r1); }
}

// Legs: a splayed limb with three round toes, on both sides.
module legs_2d(k) {
    x = xc(k); w = min(w_at(k), w_at(k + 1));
    dir = k == 1 ? 1 : -1;                        // front legs reach forward
    reach = wmax * 0.45;
    for (s = [-1, 1]) {
        foot = [x + dir * 3, s * (w + reach)];
        hull() { translate([x, s * (w - 4)]) circle(r = 4.4); translate(foot) circle(r = 3.4); }
        for (a = [-45, 0, 45]) translate(foot + 3.6 * [sin(a), s * cos(a)]) circle(r = 2.6);
    }
}

// Dragon wings: swept back, with a scalloped trailing edge.
module wings_2d(k) {
    x = xc(k); w = min(w_at(k), w_at(k + 1)); P = J[k] - J[k + 1];
    span = wmax * 1.4;
    F = [P * 0.3, w - 2]; Tp = [-P * 0.02, w + span]; B = [-P * 0.4, w - 2];
    v = B - Tp; n = [v[1], -v[0]] / norm(v);     // trailing-edge normal, pointing out
    rb = norm(v) * 0.2;
    for (m = [0, 1]) translate([x, 0]) mirror([0, m]) difference() {
        hull() {
            translate(F) circle(r = 2);
            translate(Tp) circle(r = 3);
            translate(B) circle(r = 2);
        }
        // Scallops between the wing's fingers.
        for (f = [0.2, 0.5, 0.8]) translate(Tp + v * f + n * rb * 0.25) circle(r = rb);
    }
}

// Fish fins: a rounded triangle on the top (dorsal) or bottom (anal) edge.
module fin_2d(k, s) {
    x = xc(k); w = min(w_at(k), w_at(k + 1)); P = J[k] - J[k + 1];
    hull() {
        translate([x + P * 0.3, s * (w - 2)]) circle(r = 1.5);
        translate([x - P * 0.2, s * (w + wmax * 0.7)]) circle(r = 2.5);
        translate([x - P * 0.4, s * (w - 2)]) circle(r = 1.5);
    }
}

module feet_2d(k) {
    for (s = [-1, 1]) translate([xc(k), s * (w_at(k) * 0.95 + 1.2)]) circle(r = 3.2);
}

// Full-height shape of segment k before it is cut to its region.
module shape_2d(k) {
    if (k == 0) head_2d();
    else if (k == N + 1) tail_2d();
    else {
        if (animal == "caterpillar")
            translate([xc(k), 0]) scale([(J[k] - J[k + 1]) * 0.62, w_at(k)]) circle(r = 1);
        else capsule_2d(J[k + 1], w_at(k + 1), J[k], w_at(k));
    }
}

// Full-height limbs of segment k (legs, feet), rounded less than the body so
// the toes survive.
module extras_2d(k) {
    if (k >= 1 && k <= N) {
        if (feats[k - 1] == "legs") legs_2d(k);
        if (feats[k - 1] == "feet") feet_2d(k);
    }
}

// Thinner shape of segment k (wings and fins), if any.
module low_2d(k) {
    if (k >= 1 && k <= N) {
        if (feats[k - 1] == "wings") wings_2d(k);
        if (feats[k - 1] == "dorsal") fin_2d(k, 1);
        if (feats[k - 1] == "anal") fin_2d(k, -1);
    }
}

// ---- head and tail

hl = head_len();
hw = head_w();
h0 = L - hl;                                      // == J[1]

module head_2d() {
    w1 = w_at(1);
    if (animal == "snake")
        hull() {
            translate([h0, 0]) circle(r = w1);
            translate([h0 + hl * 0.55, 0]) scale([hl * 0.45, hw]) circle(r = 1);
        }
    else if (animal == "fish")
        intersection() {
            translate([h0, 0]) scale([hl, w1]) circle(r = 1, $fn = 96);
            translate([h0 - w1, -BIG]) square([BIG, 2 * BIG]);
        }
    else if (animal == "caterpillar") {
        translate([L - hw, 0]) circle(r = hw);
        // Antennae with round tips.
        for (s = [-1, 1]) hull() {
            translate([L - hw * 0.85, s * hw * 0.6]) circle(r = 1.8);
            translate([L - hw * 0.5, s * (hw + 3.5)]) circle(r = 3);
        }
    } else {
        // Lizard and dragon: a broad skull narrowing to a round snout.
        hull() {
            translate([h0, 0]) circle(r = w1);
            translate([h0 + hl * 0.4, 0]) circle(r = hw);
            translate([L - hw * 0.55, 0]) circle(r = hw * 0.55);
        }
        if (animal == "dragon")
            for (s = [-1, 1]) hull() {
                translate([h0 + hl * 0.32, s * hw * 0.7]) circle(r = 2.8);
                translate([h0 + hl * 0.02, s * (hw + 4.5)]) circle(r = 1.8);
            }
    }
}

module tail_2d() {
    j = N + 1; x = J[j]; w = w_at(j);
    if (animal == "fish") {
        capsule_2d(x, w, x - tail_len() * 0.45, 3);
        difference() {
            hull() {
                translate([x - tail_len() * 0.4, 0]) circle(r = 3);
                translate([2.5, wmax * 1.25 - 2.5]) circle(r = 2.5);
                translate([2.5, -wmax * 1.25 + 2.5]) circle(r = 2.5);
            }
            translate([-wmax * 0.35, 0]) circle(r = wmax * 0.75);
        }
    } else if (animal == "caterpillar") {
        capsule_2d(x, w, w * 0.7, w * 0.7);
    } else if (animal == "dragon") {
        sl = min(tail_len() * 0.4, 16); sw = wmax * 0.75;
        capsule_2d(x, w, sl * 0.6, 2.5);
        rounded_2d(1.2) polygon([[0, 0], [sl * 0.8, sw], [sl * 1.1, 0], [sl * 0.8, -sw]]);
    } else
        capsule_2d(x, w, 2.5, 2.5);
}

// ---- inlays on the top face, in segment-local (straight) coordinates

module eyes_2d() {
    for (e = eye_spots()) translate([e[0], e[1]]) difference() {
        circle(r = e[2]);
        pupil_at(e);
    }
}
module pupil_at(e) { translate([e[2] * 0.25, 0]) circle(r = e[2] * 0.55); }

// [x, y, radius] of each eye.
function eye_spots() =
    animal == "fish" ? [[h0 + hl * 0.5, w_at(1) * 0.3, w_at(1) * 0.28]]
  : animal == "caterpillar" ? [for (s = [-1, 1]) [L - hw * 0.7, s * hw * 0.38, hw * 0.26]]
  : animal == "snake" ? [for (s = [-1, 1]) [h0 + hl * 0.62, s * hw * 0.5, hw * 0.26]]
  : [for (s = [-1, 1]) [h0 + hl * 0.42, s * hw * 0.52, hw * 0.28]];

module details_2d() {
    for (e = eye_spots()) translate([e[0], e[1]]) pupil_at(e);
    if (animal == "caterpillar")
        // Smile.
        translate([L - hw, 0]) intersection() {
            difference() { circle(r = hw * 0.78); circle(r = hw * 0.78 - 1.4); }
            polygon([[0, 0], [BIG * cos(35), BIG * sin(35)], [BIG * cos(35), -BIG * sin(35)]]);
        }
    else if (animal == "fish")
        // Mouth.
        translate([L - 4, -w_at(1) * 0.2]) circle(r = 1.3);
    else {
        // Nostrils near the snout.
        nx = animal == "snake" ? L - hl * 0.12 : L - hw * 0.35;
        for (s = [-1, 1]) translate([nx, s * hw * 0.2]) circle(r = 1.1);
    }
}

// Letter i of the name goes on body segment name_seg(i), counting from the
// left (tail end) so it reads left to right with the head on the right.
nm = len(name);
name_first = N - floor((N - nm) / 2);
function name_seg(i) = name_first - i;
function letter_of(k) = let(i = name_first - k) (nm > 0 && i >= 0 && i < nm) ? name[i] : "";

function has(set, ch) = search(ch, set) != [];
function wide_of(ch) = ch == "W" ? 1.45 : has("MmwQ@&%", ch) ? 1.22 : 1.07;
function tall_of(ch) = has("JQjgpqy@", ch) ? 1.36 : 1.06;

// Where a letter may go on segment k: clear of the hinge hole in front and
// the notch for the next ring behind, inside the flat top.
module letter_room_2d(k) {
    x_lo = J[k + 1] + R + c + 0.6;
    x_hi = J[k] - (rp + c + inlay + 0.6);
    w = min(w_at(k), w_at(k + 1)) - chamfer - 1;
    translate([x_lo, -w]) square([max(0.01, x_hi - x_lo), 2 * w]);
}

function room_x(k) = (J[k] - (rp + c + inlay + 0.6)) - (J[k + 1] + R + c + 0.6);
function room_y(k) = 2 * (min(w_at(k), w_at(k + 1)) - chamfer - 1);
function fit_size(k) = let(ch = letter_of(k))
    ch == "" || ch == " " ? 1e9 : min(room_x(k) / wide_of(ch), room_y(k) / tall_of(ch));
// One size for every letter of the name: the largest that fits them all.
name_size = nm == 0 ? 0 : min([for (k = [1:N]) fit_size(k)]);

module letter_2d(k) {
    ch = letter_of(k);
    if (ch != "" && ch != " ") {
        x_lo = J[k + 1] + R + c + 0.6;
        x_hi = J[k] - (rp + c + inlay + 0.6);
        s = name_size;
        if (s >= 3)
            intersection() {
                letter_room_2d(k);
                translate([(x_lo + x_hi) / 2, 0])
                    text(ch, size = s, font = font, halign = "center", valign = "center");
            }
    }
}

// All pockets in segment k's top, and the parts that fill them, clipped to
// the flat top.
module flat_top_2d(k) { offset(delta = -(chamfer + 0.4)) footprint_2d(k); }

module inlay_2d(k, what) {
    intersection() {
        flat_top_2d(k);
        if (k == 0) {
            if (what == "eye") eyes_2d();
            if (what == "detail") details_2d();
        } else if (k <= N && what == "name") letter_2d(k);
    }
}

module pockets_2d(k) {
    if (k == 0) { inlay_2d(0, "eye"); inlay_2d(0, "detail"); }
    else if (k <= N) inlay_2d(k, "name");
}

// ---- segment solids

module footprint_2d(k) {
    difference() {
        union() {
            rounded_2d(body_round(k)) intersection() { region_2d(k); shape_2d(k); }
            rounded_2d(1.2) intersection() { region_2d(k); extras_2d(k); }
            if (k >= 1) translate([J[k], 0]) circle(r = R, $fn = fn_j);
        }
        if (k <= N) notch_2d(k + 1);
    }
    if (k <= N) {
        // Neck and pin that ride in the next segment's ring.
        translate([J[k + 1], -neck_hw]) square([R + c + 2, 2 * neck_hw]);
        translate([J[k + 1], 0]) circle(r = rp, $fn = fn_j);
    }
}

module low_footprint_2d(k) {
    difference() {
        rounded_2d(1.2) intersection() { region_2d(k); low_2d(k); }
        if (k <= N) notch_2d(k + 1);
        if (k >= 1) notch_2d(k);
    }
}

// Extrude with a set-in bottom edge and a stepped top chamfer.
module slab(h) {
    ch = min(chamfer, h / 4);
    linear_extrude(foot) offset(delta = -foot) children();
    translate([0, 0, foot]) linear_extrude(h - foot - ch) children();
    for (i = [1:chamfer_steps])
        translate([0, 0, h - ch + (i - 1) * ch / chamfer_steps])
            linear_extrude(ch / chamfer_steps) offset(delta = -i * ch / chamfer_steps) children();
}

module pin_3d() {
    rotate_extrude($fn = fn_j) polygon([[0, 0], [rp, 0], [rp + bulge, bulge],
                                      [rp + bulge, T - bulge], [rp, T], [0, T]]);
}

module hole_3d() {
    rotate_extrude($fn = fn_j) scale([sec, 1]) polygon([[0, -1], [rp + c + foot, -1], [rp + c + foot, foot],
                                      [rp + c + bulge, bulge], [rp + c + bulge, T - bulge],
                                      [rp + c, T], [rp + c, T + 1], [0, T + 1]]);
}

module slot_3d() {
    r = R + 2;
    translate([0, 0, -1]) linear_extrude(T + 2)
        polygon(concat([[0, 0]], [for (a = [-slot_angle:5:slot_angle]) [r * cos(a), r * sin(a)] * 1.1]));
}

module segment_3d(k) {
    difference() {
        union() {
            slab(T) footprint_2d(k);
            if (k >= 1 && k <= N && feats[k - 1] != "none" && feats[k - 1] != "legs" && feats[k - 1] != "feet")
                slab(low_h) low_footprint_2d(k);
            if (k <= N) translate([J[k + 1], 0, 0]) pin_3d();
        }
        if (k >= 1) translate([J[k], 0, 0]) { hole_3d(); slot_3d(); }
        translate([0, 0, T - inlay]) linear_extrude(inlay + 1) pockets_2d(k);
    }
}

module inlay_3d(k, what) {
    translate([0, 0, T - inlay]) linear_extrude(inlay) inlay_2d(k, what);
}

function seg_color(k) = k == 0 ? head_color : k % 2 == 1 ? body_color : stripe_color;

// ---- checks, reported for verify.sh

cap_mid = (rp + bulge) - (rp + c + bulge) * sin(slot_angle);   // pin vs gap in the C, middle
cap_top = rp - (rp + c) * sin(slot_angle);                     // pin vs gap in the C, top lip
cap_lip = bulge - c;                                           // pin vs ring lip, vertically
echo(str("SB_CRITTER N=", N, " pitch=", p, " length=", L, " R=", R, " neck=", 2 * neck_hw,
         " cap_mid=", cap_mid, " cap_top=", cap_top, " cap_lip=", cap_lip,
         " low_h=", low_h, " name_size=", name_size, " joints=", [for (k = [1:N + 1]) J[k]]));
assert(neck_hw >= 1, "hinge neck too thin");
assert(cap_mid > 0.3 && cap_top > 0.15 && cap_lip > 0.5, "hinge pin not captured");

// ---- output

if (probe_capture != [0, 0]) {
    for (k = [1:N + 1]) intersection() {
        segment_3d(k);
        translate([probe_capture[0], 0, probe_capture[1]]) segment_3d(k - 1);
    }
    translate([-1000, 0, 0]) cube(1);
} else if (probe_gap > 0) {
    zs = [0.15, foot + 0.1, (foot + bulge) / 2, bulge + 0.05, T / 2, low_h - 0.05,
          T - bulge - 0.05, T - bulge / 2, T - chamfer - 0.05, T - 0.1];
    for (k = [0:N], dk = [1, 2], z = zs) if (k + dk <= N + 1)
        translate([0, 0, z]) linear_extrude(0.01) intersection() {
            offset(r = probe_gap / 2, $fn = 24) projection(cut = true)
                translate([0, 0, -z]) posed(k) segment_3d(k);
            offset(r = probe_gap / 2, $fn = 24) projection(cut = true)
                translate([0, 0, -z]) posed(k + dk) segment_3d(k + dk);
        }
    translate([-1000, 0, 0]) cube(1);
} else {
    for (k = [0:N + 1]) posed(k) {
        color(seg_color(k)) segment_3d(k);
        if (k == 0) {
            color(eye_color) inlay_3d(0, "eye");
            color(detail_color) inlay_3d(0, "detail");
        } else if (k <= N && letter_of(k) != "" && letter_of(k) != " ")
            color(name_color) inlay_3d(k, "name");
    }
}
