// Chunky name sign — a word in a heavy display face standing on a thick
// backing cut to the outline of the letters, with the letters raised on top.
// Options: an outer ring in a third colour (a double border), an outline
// stroke around the letters, WordArt-style text shapes (arches, a full circle
// badge, a wave, slants, bulge, pinch, perspective, stairs), vertical layouts,
// a flat bottom or a slotted foot so it stands on a shelf, and magnet pockets
// or keyhole slots in the back.
//
// Prints flat, back down: the backing and the ring are one flat slab and the
// letters and outline stand on top of it, so nothing needs supports. Magnet
// pockets and keyholes open on the bed and are bridged over.
//
// Written to the MakerWorld Parametric Model Maker customizer conventions so
// the same file works unchanged on MakerWorld and in ScadBuddy.
//
// The colour parameters are the extruder order: backing_color is extruder 1,
// text_color extruder 2, ring_color extruder 3, outline_color extruder 4. The
// ring and outline only exist when switched on, so the defaults print in two
// colours. Colours that are equal merge into one part and one filament.

/* [Text] */

// The word on the sign
text = "Emma"; // 20

// Typeface
font = "DejaVu Sans:style=Bold"; // font

// Letter size in mm (about the height of a capital); auto-fit only ever shrinks it
text_size = 40; // [10:1:100]

// Spacing between letters (1 = the font's own; lower pulls them together)
letter_spacing = 0.9; // [0.7:0.01:1.3]

// Thicken every letter by this much all round, in mm (makes any face chunkier)
boldness = 1; // [0:0.1:3]

// Shrink the text so the word is never longer than max_length (off or on, a word too long for the 300 x 320 mm plate is always shrunk to fit it)
auto_fit = true;

// Longest the word may run in mm (its width when horizontal, its height when vertical); capped at what fits the plate
max_length = 180; // [40:5:400]

/* [Layout] */

// Which way the word runs
layout = "horizontal"; // [horizontal:Horizontal, vertical_stacked:Vertical - letters stacked, vertical_rotated:Vertical - rotated to read upwards]

// Stacked layout: extra gap between rows in mm (negative overlaps them)
row_gap = 2; // [-10:0.5:20]

/* [Shape] */

// WordArt-style shape of the word (horizontal and rotated layouts; stacked letters ignore it)
text_shape = "straight"; // [straight:Straight, arch_up:Arch up, arch_down:Arch down - valley, circle:Circle - round badge, wave:Wave, slant_up:Slant up, slant_down:Slant down, bulge:Bulge, pinch:Pinch, perspective:Perspective - shrinking, stairs:Stairs]

// Arch radius in mm; smaller bends the word more
arc_radius = 120; // [40:5:500]

// Circle: radius of the ring the letters stand on, in mm (0 = automatic, the word wraps most of the way round); a badge too big for the plate is shrunk
circle_radius = 0; // [0:1:150]

// Wave height in mm (either side of the centre line)
wave_amplitude = 8; // [1:0.5:30]

// Wave length in mm (one full up-and-down)
wave_length = 120; // [30:5:400]

// Slant angle in degrees
skew_angle = 15; // [5:1:40]

// Bulge, pinch and perspective: how much the letter size changes, in percent
shape_amount = 40; // [10:5:80]

// Stairs: how far each letter steps up from the one before, in mm (reduced if the staircase would not fit the plate)
stair_step = 6; // [1:0.5:30]

/* [Backing] */

// Width of the backing border around the letters in mm
border = 5; // [1.5:0.5:15]

// Thickness of the backing in mm
backing_thickness = 7; // [2:0.5:25]

// How far the letters stand proud of the backing in mm
letter_height = 4; // [1:0.5:15]

// Chamfer on the top edges of the letters and the backing in mm (0 = square)
bevel = 0.6; // [0:0.1:2]

/* [Extras] */

// Add a second ring around the backing in its own colour (a double border)
outer_ring = false;

// Width of the outer ring in mm
ring_width = 3; // [1:0.5:10]

// Add an outline stroke around the letters in its own colour
text_outline = false;

// Width of the letter outline in mm (kept inside the border)
outline_width = 2; // [0.5:0.5:6]

// Something to stand it up on a shelf
stand = "none"; // [none:None, flat_bottom:Flat bottom - stands on its own, foot:Flat bottom plus a slotted foot]

// Fixings in the back
mount = "none"; // [none:None, magnets:Magnet pockets, keyholes:Keyhole slots]

// Distance between the two magnets or keyholes in mm (0 = automatic, half the word)
mount_spacing = 0; // [0:5:300]

// Magnet diameter in mm (the pocket adds 0.3 mm)
magnet_d = 10; // [4:0.5:20]

// Magnet thickness in mm (the pocket adds 0.2 mm, but is never deeper than the backing less 1 mm)
magnet_h = 2; // [1:0.5:5]

/* [Colors] */

// Backing colour (extruder 1); also the foot
backing_color = "#FFD23F"; // color

// Letter colour (extruder 2)
text_color = "#1565C0"; // color

// Outer ring colour (extruder 3)
ring_color = "#E53935"; // color

// Letter outline colour (extruder 4)
outline_color = "#FFFFFF"; // color

/* [Hidden] */

$fn = 48;

// Morphological close (dilate then erode) that welds neighbouring glyphs into
// one piece without changing the word's overall size.
weld_k = 0.05;
// Shaped text: per-pair weld and the radius of the rod bridging a pair that
// does not touch, as fractions of the size.
pair_weld_k = 0.035;
bridge_k = 0.09;
// Stepped chamfer: number of steps.
bevel_steps = 3;
// Centre line of the letters, as a fraction of the size (scaling origin).
mid_k = 0.45;
// Spine through the letter centres, as a fraction of the size: keeps the
// backing one piece across spaces and between stacked rows.
spine_k = 0.14;
// Magnet pocket clearance on diameter and depth.
magnet_clear_d = 0.3;
magnet_clear_h = 0.2;
// Keyhole: entry for the screw head, slot for the shank, lip under the head.
key_head_d = 9;
key_slot_d = 4.5;
key_len = 10;
key_lip = 1.5;
// Material kept between a pocket or keyhole and the backing's edge.
mount_wall = 1.5;
// Foot: slot clearance, lip height, floor under the slot, gap on the plate.
foot_clear = 0.3;
foot_lip = 8;
foot_floor = 2;
part_gap = 8;

// ------------------------------------------------------------ glyph metrics

// Advance, lowest and highest ink per character at size 1, measured from the
// fonts in the ScadBuddy image (halign="left", valign="baseline"). Shaped text
// places every character by these; any other face gets a generic advance.
DSB = [
    [0.484,0,0], [1.075,0,1.012], [1.059,0,1.012], [1.019,-0.02,1.031],
    [1.153,0,1.012], [0.949,0,1.012], [0.949,0,1.012], [1.14,-0.02,1.031],
    [1.162,0,1.012], [0.517,0,1.012], [0.517,-0.278,1.012], [1.076,0,1.012],
    [0.885,0,1.012], [1.382,0,1.012], [1.162,0,1.012], [1.181,-0.02,1.031],
    [1.018,0,1.012], [1.181,-0.203,1.031], [1.069,0,1.012], [1,-0.02,1.031],
    [0.947,0,1.012], [1.128,-0.02,1.012], [1.075,0,1.012], [1.532,0,1.012],
    [1.071,0,1.012], [1.006,0,1.012], [1.007,0,1.012], [0.937,-0.02,0.778],
    [0.994,-0.02,1.055], [0.823,-0.02,0.778], [0.994,-0.02,1.055],
    [0.942,-0.02,0.778], [0.604,0,1.055], [0.994,-0.3,0.776], [0.989,0,1.055],
    [0.476,0,1.055], [0.476,-0.3,1.055], [0.924,0,1.055], [0.476,0,1.055],
    [1.447,0,0.778], [0.989,0,0.778], [0.954,-0.02,0.778], [0.994,-0.289,0.778],
    [0.994,-0.289,0.776], [0.685,0,0.778], [0.827,-0.02,0.778], [0.664,0,0.975],
    [0.989,-0.02,0.76], [0.905,0,0.76], [1.283,0,0.76], [0.896,0,0.76],
    [0.905,-0.3,0.76], [0.808,0,0.76], [0.966,-0.02,1.031], [0.966,0,1.012],
    [0.966,0,1.031], [0.966,-0.02,1.031], [0.966,0,1.012], [0.966,-0.02,1.012],
    [0.966,-0.02,1.029], [0.966,0,1.012], [0.966,-0.02,1.031],
    [0.966,-0.02,1.029], [0.633,0,1.012], [1.211,-0.02,1.031], [0.425,0.636,1.012],
    [0.576,0.301,0.499], [0.528,0,0.262], [0.806,0,1.031]];
DSR = [
    [0.441,0,0], [0.95,0,1.012], [0.953,0,1.012], [0.97,-0.02,1.031],
    [1.069,0,1.012], [0.878,0,1.012], [0.799,0,1.012], [1.076,-0.02,1.031],
    [1.044,0,1.012], [0.41,0,1.012], [0.41,-0.278,1.012], [0.911,0,1.012],
    [0.774,0,1.012], [1.198,0,1.012], [1.039,0,1.012], [1.093,-0.02,1.031],
    [0.838,0,1.012], [1.093,-0.179,1.031], [0.965,0,1.012], [0.882,-0.02,1.031],
    [0.848,0,1.012], [1.017,-0.02,1.012], [0.95,0,1.012], [1.373,0,1.012],
    [0.951,0,1.012], [0.848,0,1.012], [0.951,0,1.012], [0.851,-0.02,0.778],
    [0.882,-0.02,1.055], [0.764,-0.02,0.778], [0.882,-0.02,1.055],
    [0.854,-0.02,0.778], [0.489,0,1.055], [0.882,-0.289,0.778], [0.88,0,1.055],
    [0.386,0,1.055], [0.386,-0.289,1.055], [0.804,0,1.055], [0.386,0,1.055],
    [1.353,0,0.778], [0.88,0,0.778], [0.85,-0.02,0.778], [0.882,-0.289,0.778],
    [0.882,-0.289,0.778], [0.571,0,0.778], [0.724,-0.02,0.778], [0.545,0,0.975],
    [0.88,-0.02,0.76], [0.822,0,0.76], [1.136,0,0.76], [0.822,0,0.76],
    [0.822,-0.289,0.76], [0.729,0,0.76], [0.884,-0.02,1.031], [0.884,0,1.012],
    [0.884,0,1.031], [0.884,-0.02,1.031], [0.884,0,1.012], [0.884,-0.02,1.012],
    [0.884,-0.02,1.031], [0.884,0,1.012], [0.884,-0.02,1.031],
    [0.884,-0.02,1.031], [0.557,0,1.012], [1.083,-0.02,1.031], [0.382,0.636,1.012],
    [0.501,0.325,0.436], [0.441,0,0.172], [0.737,0,1.031]];
LTB = [
    [0.299,0,0], [0.838,-0.158,1.042], [0.943,-0.024,1.042], [0.701,-0.022,1.042],
    [0.982,-0.013,1.042], [0.621,-0.025,1.044], [0.775,0,1.042],
    [0.824,-0.287,1.042], [0.964,0,1.042], [0.56,0,1.042], [0.603,-0.287,1.042],
    [0.901,-0.024,1.042], [0.629,-0.347,1.028], [1.094,0,1.042], [0.997,0,1.042],
    [0.968,-0.008,1.042], [0.836,0,1.042], [0.968,-0.157,1.042],
    [0.908,-0.024,1.042], [0.735,-0.025,1.042], [0.933,0,1.04], [1.071,-0.01,1.042],
    [0.99,0,1.042], [1.401,0,1.042], [0.901,0,1.042], [0.901,0,1.042],
    [0.772,0,1.042], [0.743,-0.008,0.701], [0.617,-0.008,0.972],
    [0.532,-0.008,0.701], [0.742,-0.008,0.972], [0.551,-0.008,0.701],
    [0.371,-0.347,1.04], [0.697,-0.347,0.701], [0.721,-0.008,0.972],
    [0.386,-0.008,0.99], [0.326,-0.347,0.99], [0.742,-0.008,0.972],
    [0.379,-0.008,0.972], [1.064,-0.008,0.701], [0.721,-0.008,0.7],
    [0.668,-0.008,0.701], [0.65,-0.347,0.746], [0.675,-0.347,0.701],
    [0.522,0,0.701], [0.546,-0.008,0.738], [0.392,-0.008,0.914],
    [0.714,-0.008,0.694], [0.575,-0.008,0.722], [0.918,-0.008,0.722],
    [0.749,-0.008,0.783], [0.669,-0.347,0.694], [0.594,0,0.694],
    [0.829,-0.008,1.042], [0.463,0,1.039], [0.708,-0.015,1.04], [0.743,-0.008,1.04],
    [0.713,0,1.042], [0.719,-0.008,1.04], [0.767,-0.008,1.042],
    [0.657,-0.013,1.04], [0.765,-0.008,1.042], [0.765,-0.008,1.042],
    [0.369,0,0.972], [0.931,-0.025,1.044], [0.267,0.779,1.042], [0.443,0.308,0.418],
    [0.329,0,0.221], [0.583,0,0.972]];
DFB = [
    [0.484,0,0], [1.078,0,1.012], [1.174,0,1.012], [1.105,-0.02,1.031],
    [1.204,0,1.012], [1.059,0,1.012], [0.986,0,1.012], [1.186,-0.02,1.031],
    [1.312,0,1.012], [0.65,0,1.012], [0.657,-0.289,1.012], [1.207,0,1.012],
    [0.977,0,1.012], [1.537,0,1.012], [1.27,0,1.012], [1.21,-0.02,1.031],
    [1.044,0,1.012], [1.21,-0.25,1.031], [1.154,0,1.012], [1.003,-0.02,1.031],
    [1.034,0,1.012], [1.211,-0.02,1.012], [1.078,0,1.012], [1.56,0,1.012],
    [1.078,0,1.012], [0.991,0,1.012], [1.014,0,1.012], [0.9,-0.02,0.74],
    [0.971,-0.02,1.055], [0.846,-0.02,0.74], [0.971,-0.02,1.055],
    [0.884,-0.02,0.74], [0.597,0,1.055], [0.971,-0.308,0.74], [1.01,0,1.055],
    [0.528,0,1.055], [0.503,-0.308,1.055], [0.962,0,1.055], [0.528,0,1.055],
    [1.47,0,0.74], [1.01,0,0.74], [0.926,-0.02,0.74], [0.971,-0.289,0.74],
    [0.971,-0.289,0.74], [0.732,0,0.74], [0.782,-0.02,0.74], [0.642,-0.02,0.945],
    [1.01,-0.02,0.721], [0.807,0,0.721], [1.196,0,0.721], [0.828,0,0.721],
    [0.807,-0.308,0.721], [0.789,0,0.721], [0.966,-0.02,1.031], [0.966,0,1.031],
    [0.966,0,1.031], [0.966,-0.02,1.031], [0.966,0,1.031], [0.966,-0.02,1.012],
    [0.966,-0.02,1.031], [0.966,0,1.012], [0.966,-0.02,1.031],
    [0.966,-0.02,1.031], [0.966,-0.02,1.012], [1.254,-0.02,1.031],
    [0.425,0.636,1.012], [0.576,0.281,0.464], [0.484,-0.02,0.239],
    [0.814,-0.02,1.031]];
METRIC_CHARS = " ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!&'-.?";
GENERIC = [0.62, -0.3, 1.06];

TABLE = font == "DejaVu Sans:style=Bold" ? DSB
      : (font == "DejaVu Sans" || font == "DejaVu Sans:style=Book") ? DSR
      : font == "Lobster Two:style=Bold" ? LTB
      : font == "DejaVu Serif:style=Bold" ? DFB
      : undef;

function met(c) = let(i = search(c, METRIC_CHARS))
    (TABLE != undef && len(i) > 0) ? TABLE[i[0]]
    : c == " " ? [0.3, 0, 0] : GENERIC;

// ------------------------------------------------------------ word layout

// An empty (or all-space) word would leave nothing to print; show a
// placeholder instead.
word = len([for (i = [0:1:len(text) - 1]) if (text[i] != " ") 1]) > 0 ? text : "Name";
n = len(word);
CH = [for (i = [0:1:n - 1]) word[i]];
M = [for (c = CH) met(c)];
has_text = len([for (c = CH) if (c != " ") 1]) > 0;

horizontal = layout != "vertical_stacked";
native = text_shape == "straight" || text_shape == "slant_up"
         || text_shape == "slant_down";
shape = horizontal ? text_shape : "straight";
slant = shape == "slant_up" ? tan(skew_angle)
      : shape == "slant_down" ? -tan(skew_angle) : 0;
sp = letter_spacing;
k_amt = shape_amount / 100;

// ------------------------------------------------------------ plate fit

// The H2C prints 300 mm wide with both nozzles and 320 mm deep. Nothing here
// measures the finished sign, so the longest run of letters is capped at the
// plate less the frame round it: backing border, outer ring, the thickening,
// and 3 % for arches that reach past their fitted length. In the vertical
// layouts a foot sits below the sign on the same plate, so its depth comes
// off too. The cap applies with auto_fit off as well: a sign that cannot go
// on the plate is not a useful result.
PLATE_W = 300;
PLATE_D = 320;
ring_w = outer_ring ? ring_width : 0;
frame = 2 * (border + ring_w + boldness) + 2;
foot_below = stand == "foot"
    ? max(0, 9 + (text_outline ? min(outline_width, border - 0.5) : 0) - border)
      + 8 + max(30, backing_thickness + 24.3)
    : 0;
plate_len = layout == "horizontal" ? (PLATE_W - frame) / 1.03
          : (PLATE_D - frame - foot_below) / 1.03;
fit_max = auto_fit ? min(max_length, plate_len) : plate_len;
// A circle badge is as tall as it is wide, and the widest the plate takes is
// its 300 mm.
circle_plate = PLATE_W - frame;

function cumsum(v, i) = i <= 0 ? 0 : cumsum(v, i - 1) + v[i - 1];
function total(v) = cumsum(v, len(v));

// Unscaled advance centres at size 1, for the size-change profiles.
A0 = [for (m = M) m[0] * sp];
L0 = max(0.001, total(A0));
C0 = [for (i = [0:1:n - 1]) -L0 / 2 + cumsum(A0, i) + A0[i] / 2];

function profile(t) =
    shape == "bulge" ? 1 + k_amt * (1 - t * t)
  : shape == "pinch" ? 1 - k_amt * (1 - t * t)
  : shape == "perspective" ? 1 - k_amt * (t + 1) / 2
  : 1;
F = [for (i = [0:1:n - 1]) profile(C0[i] / (L0 / 2))];

// Path length per unit size with the size profile applied.
W1 = [for (i = [0:1:n - 1]) A0[i] * F[i]];
L1 = max(0.001, total(W1));

// Circle: the radius is given, or chosen so the word wraps 80 % of the ring
// (never tighter than 2 letter sizes). With auto-fit an automatic circle
// shrinks as a whole until the badge's diameter fits max_length.
is_circle = shape == "circle";
R_nom = circle_radius > 0 ? circle_radius
      : max(2 * text_size, text_size * L1 / (0.8 * 2 * PI));
k_circ = min(1, (circle_radius == 0 && auto_fit ? min(max_length, circle_plate) : circle_plate)
                / (2 * (R_nom + 0.6 * text_size)));
R_circ = R_nom * k_circ;

// Longest path the shape allows before the word would run into itself.
shape_cap = is_circle ? 0.9 * 2 * PI * R_circ
          : (shape == "arch_up" || shape == "arch_down") ? 1.5 * PI * arc_radius
          : 1e9;
fit_len = min(shape_cap, horizontal && !is_circle ? fit_max : 1e9);

// Letter size after fitting. Straight and slanted text is fitted exactly by
// resize() below; this estimate (from the metrics, ignoring kerning) is only
// used there for placing the spine, the fixings and the stand.
s = !horizontal ? text_size
  : min(text_size * (is_circle ? k_circ : 1), fit_len / L1);
ym = mid_k * s;

// Advance centres along the path at the fitted size.
WS = [for (w = W1) w * s];
LS = total(WS);
CS = [for (i = [0:1:n - 1]) -LS / 2 + cumsum(WS, i) + WS[i] / 2];

if (auto_fit && max_length > plate_len && !is_circle)
    echo(str("NOTE: max_length capped from ", max_length, " to ", round(plate_len),
             " mm so the sign fits the 300 x 320 mm plate"));
if (is_circle && circle_radius > 0 && k_circ < 1)
    echo(str("NOTE: circle_radius reduced from ", circle_radius, " to ", round(R_circ),
             " mm so the badge fits the 300 mm plate"));

// Stairs rise across the plate's depth (its width when rotated); the step
// shrinks so the whole staircase, one letter tall at the top, still fits.
st_room = layout == "horizontal" ? PLATE_D - frame - foot_below : PLATE_W - frame;
st_step = n > 1 ? max(0, min(stair_step, (st_room - 1.2 * s) / (n - 1))) : stair_step;

if (shape == "stairs" && st_step < stair_step)
    echo(str("NOTE: stair_step reduced from ", stair_step, " to ", round(st_step * 10) / 10,
             " mm so the staircase fits the plate"));

R_arc = shape == "circle" ? R_circ : arc_radius;

// Placement of character i: [x, y, rotation in degrees, scale]. The glyph is
// drawn advance-centred on its baseline, scaled about its centre line. Arches
// and the circle bend the centre line (not the baseline) to radius R, so the
// letters open up above it and close up below it by the same amount.
function place(i) =
    let(c = CS[i], th = c / R_arc * 180 / PI)
    (shape == "arch_up" || shape == "circle")
        ? [(R_arc - ym) * sin(th), (R_arc - ym) * cos(th) - R_arc, -th, 1]
  : shape == "arch_down"
        ? [(R_arc + ym) * sin(th), R_arc - (R_arc + ym) * cos(th), th, 1]
  : shape == "wave"
        ? let(a = 360 * c / wave_length)
          let(r = atan(wave_amplitude * 2 * PI / wave_length * cos(a)))
          // tilted about the centre line, which follows the wave
          [c + ym * sin(r), wave_amplitude * sin(a) + ym * (1 - cos(r)), r, 1]
  : shape == "stairs" ? [c, (i - (n - 1) / 2) * st_step, 0, 1]
  : [c, c * slant, 0, F[i]];

P = [for (i = [0:1:n - 1]) place(i)];

function xf(p, q) =            // local glyph point q through placement p
    let(y = ym + (q[1] - ym) * p[3], x = q[0] * p[3])
    [p[0] + x * cos(p[2]) - y * sin(p[2]), p[1] + x * sin(p[2]) + y * cos(p[2])];

// Stacked layout: letter size and row pitch. A row is about 1.05 sizes of
// ink; the column fits max_length including the thickening.
s_st = max(1, min(text_size, (fit_max - (n - 1) * row_gap - 2 * boldness)
                              / (max(1, n) + 0.05)));
row_pitch = s_st + row_gap;
function row_y(i) = ((n - 1) / 2 - i) * row_pitch;

// Letter centres in world coordinates (spaces skipped).
function world(q) = layout == "vertical_rotated" ? [-q[1], q[0]] : q;
ANCH = [for (i = [0:1:n - 1]) if (CH[i] != " ")
            horizontal ? world(xf(P[i], [0, ym])) : [0, row_y(i)]];
na = len(ANCH);

// Estimated extent of the letters in world coordinates [xmin, xmax, ymin, ymax].
BOX_PTS = horizontal
    ? [for (i = [0:1:n - 1]) if (CH[i] != " ")
           for (qx = [-0.5, 0.5], qy = [M[i][1], M[i][2]])
               world(xf(P[i], [qx * M[i][0] * s, qy * s]))]
    : [for (i = [0:1:n - 1]) if (CH[i] != " ")
           for (qx = [-0.5, 0.5], qy = [-0.5, 0.5] * (M[i][2] - M[i][1]))
               [qx * M[i][0] * s_st, row_y(i) + qy * s_st]];
EXT = len(BOX_PTS) == 0 ? [-1, 1, -1, 1] :
    [min([for (p = BOX_PTS) p[0]]), max([for (p = BOX_PTS) p[0]]),
     min([for (p = BOX_PTS) p[1]]), max([for (p = BOX_PTS) p[1]])];
s_ref = horizontal ? s : s_st;
weld = weld_k * s_ref;
// Thickening follows the fitted size, so shrunk text keeps its counters open.
bold = boldness * s_ref / text_size;
// Lowest ink, with a margin for the estimate.
y_low = min(EXT[2], shape == "circle" && horizontal && layout == "horizontal"
                    ? -2 * R_circ : EXT[2])
        - 0.08 * s_ref - bold;

// ------------------------------------------------------------ 2D letters

module glyph(i, sz) {
    text(CH[i], size = sz, font = font, halign = "center", valign = "baseline");
}

// Shrink-only fit along x (see name-sign): resize() scales to the bounding
// box of its children; a hair-thin bar of the target length, parked at z=10,
// makes that box at least the target size, so text already shorter is left
// alone and longer text is scaled down uniformly about the origin.
module fit_x(w) {
    projection(cut = true) translate([0, 0, -0.5])
        resize([w, 0, 0], auto = [false, true, false])
            union() {
                linear_extrude(1) children();
                translate([-w / 2, 0, 10]) cube([w, 0.01, 0.01]);
            }
}

module native_word() {
    multmatrix([[1, 0, 0, 0], [slant, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])
        fit_x(fit_max)
            text(word, size = text_size, font = font, spacing = sp,
                 halign = "center", valign = "baseline");
}

module placed_glyph(i, convex = false) {
    p = P[i];
    translate([p[0], p[1]]) rotate(p[2])
        translate([0, ym * (1 - p[3])])
            if (convex) hull() glyph(i, s * p[3]);
            else glyph(i, s * p[3]);
}

// Stacked rows: one letter per row, centred.
module row_glyph(i, convex = false) {
    translate([0, row_y(i)])
        if (convex) hull() text(CH[i], size = s_st, font = font, halign = "center", valign = "center");
        else text(CH[i], size = s_st, font = font, halign = "center", valign = "center");
}

// One letter of a shaped word or of the stack, and its size profile/centre.
module unit(i, convex = false) {
    if (horizontal) placed_glyph(i, convex); else row_glyph(i, convex);
}
function unit_f(i) = horizontal ? F[i] : 1;
function unit_c(i) = horizontal ? xf(P[i], [0, ym]) : [0, row_y(i)];

// Shaped and stacked words are placed letter by letter, so they are welded
// pair by pair, each pair with a radius that follows the pair's own letter
// size. Where a pair still does not touch (letters placed one by one get no
// kerning; stacked rows have a gap), a short rod along the line between their
// centres fills just the space between the two letters -- the difference of
// the pair's hull and each letter's own hull, so no counter is filled.
module welded_units() {
    for (i = [0:1:n - 1]) if (CH[i] != " ") {
        offset(r = bold * unit_f(i)) unit(i);
        if (i + 1 < n && CH[i + 1] != " ") {
            f = min(unit_f(i), unit_f(i + 1));
            w = pair_weld_k * s_ref * max(unit_f(i), unit_f(i + 1));
            offset(r = -w) offset(r = w + bold * f) {
                unit(i);
                unit(i + 1);
                intersection() {
                    hull() {
                        translate(unit_c(i)) circle(r = bridge_k * s_ref * f);
                        translate(unit_c(i + 1)) circle(r = bridge_k * s_ref * f);
                    }
                    difference() {
                        hull() { unit(i, true); unit(i + 1, true); }
                        unit(i, true);
                        unit(i + 1, true);
                    }
                }
            }
        }
    }
}

module raw_letters() {
    if (layout == "vertical_rotated") rotate(90) native_word();
    else native_word();
}

// Letters, thickened and welded into one piece.
module letters_2d() {
    if (!horizontal) welded_units();
    else if (!native) {
        if (layout == "vertical_rotated") rotate(90) welded_units();
        else welded_units();
    } else
        offset(r = -weld) offset(r = weld + bold) raw_letters();
}

// ------------------------------------------------------------ 2D backing

// Chain of discs through the letter centres.
module spine_2d() {
    r = spine_k * s_ref;
    if (na == 1) translate(ANCH[0]) circle(r = r);
    if (na > 1) for (k = [0:1:na - 2])
        hull() { translate(ANCH[k]) circle(r = r); translate(ANCH[k + 1]) circle(r = r); }
}

module core_2d() {
    fill() union() {
        letters_2d();
        spine_2d();
        if (shape == "circle")
            translate(world([0, -R_circ])) circle(r = R_circ, $fn = 96);
    }
}

// Stand: the lower part of the outline hulled down to a flat line at y_cut,
// so the sign stands on it. The foot's lip must clear the lowest letter.
lip_clear = stand == "foot" ? foot_lip + 1 : 0;
out_w = text_outline ? min(outline_width, border - 0.5) : 0;
y_cut = y_low - max(border, lip_clear + out_w);
flat = stand != "none" && has_text;

module backing_sil() {
    fill() union() {
        offset(r = border) core_2d();
        if (flat)
            hull() {
                intersection() {
                    offset(r = border) core_2d();
                    translate([-5000, y_cut]) square([10000, y_low - y_cut + 0.6 * s_ref]);
                }
                translate([0, y_cut])
                    scale([1, 0.001]) intersection() {
                        offset(r = border) core_2d();
                        translate([-5000, y_low - 0.1]) square([10000, 0.6 * s_ref]);
                    }
            }
    }
}

// ------------------------------------------------------------ fixings

MOUNT_PTS = na == 0 ? [] :
    !horizontal ? (na > 1 ? [ANCH[0], ANCH[na - 1]] : [ANCH[0]])
  : shape == "circle"
        ? let(cc = [0, -R_circ], d = mount_spacing > 0 ? mount_spacing / 2
                                                      : R_circ * 0.45)
          [world(cc + [-d, 0]), world(cc + [d, 0])]
  : let(want = mount_spacing > 0 ? mount_spacing / 2 : LS / 4,
        a = nearest(-want), b = nearest(want))
    a == b ? [world(xf(P[a], [0, ym]))]
           : [world(xf(P[a], [0, ym])), world(xf(P[b], [0, ym]))];

// Non-space character whose advance centre is nearest path position x.
function nearest(x) =
    let(idx = [for (i = [0:1:n - 1]) if (CH[i] != " ") i],
        d = [for (j = idx) abs(CS[j] - x)])
    idx[[for (k = [0:1:len(d) - 1]) if (d[k] == min(d)) k][0]];

pocket_d = magnet_d + magnet_clear_d;
pocket_h = min(magnet_h + magnet_clear_h, backing_thickness - 1);
key_ch = min(3, backing_thickness - key_lip - 1.2);

module mount_cut_3d() {
    if (mount == "magnets" && pocket_h > 0.4)
        for (p = MOUNT_PTS) translate([p[0], p[1], -1])
            cylinder(d = pocket_d, h = pocket_h + 1);
    if (mount == "keyholes" && key_ch >= 1)
        for (p = MOUNT_PTS) translate([p[0], p[1], 0]) {
            // lip layer: head entry plus the narrow slot for the shank
            translate([0, 0, -1]) linear_extrude(key_lip + 1) {
                circle(d = key_head_d);
                hull() { circle(d = key_slot_d); translate([0, key_len]) circle(d = key_slot_d); }
            }
            // channel the head slides along, bridged over
            translate([0, 0, key_lip]) linear_extrude(key_ch) hull() {
                circle(d = key_head_d); translate([0, key_len]) circle(d = key_head_d);
            }
        }
}

module mounts_3d() {
    intersection() {
        mount_cut_3d();
        translate([0, 0, -2]) linear_extrude(backing_thickness + 4)
            offset(delta = -mount_wall) backing_sil();
    }
}

// ------------------------------------------------------------ bevelled solids

// Extrude children(0) to h with a stepped chamfer of size b on the top edge;
// children(1), when given, is kept out of every step (unbevelled inner edge).
module bevel_extrude(h, b) {
    bb = max(0, min(b, h / 2));
    if (bb <= 0.05) linear_extrude(h) difference() { children(0); if ($children > 1) children(1); }
    else {
        linear_extrude(h - bb) difference() { children(0); if ($children > 1) children(1); }
        for (k = [1:bevel_steps])
            translate([0, 0, h - bb + (k - 1) * bb / bevel_steps])
                linear_extrude(bb / bevel_steps)
                    difference() {
                        offset(delta = -bb * k / bevel_steps) children(0);
                        if ($children > 1) children(1);
                    }
    }
}

T = backing_thickness;
LH = letter_height;
b_back = outer_ring ? 0 : min(bevel, (border - out_w) * 0.8);
b_ring = min(bevel, ring_width * 0.8);

if (has_text) {
    color(backing_color) difference() {
        bevel_extrude(T, b_back) backing_sil();
        mounts_3d();
    }
    if (outer_ring)
        color(ring_color) bevel_extrude(T, b_ring) { offset(r = ring_width) backing_sil(); backing_sil(); }
    color(text_color) translate([0, 0, T]) bevel_extrude(LH, min(bevel, 0.25 * s_ref)) letters_2d();
    if (text_outline && out_w > 0)
        color(outline_color) translate([0, 0, T])
            bevel_extrude(LH, min(bevel, out_w * 0.8)) { offset(r = out_w) letters_2d(); letters_2d(); }
}

// ------------------------------------------------------------ foot

// Separate foot printed below the sign: a block with a straight slot the flat
// bottom drops into. The lip is lower than the gap under the letters.
foot_len = horizontal && layout == "horizontal"
    ? max(40, min(200, 0.6 * (EXT[1] - EXT[0])))
    : max(40, EXT[1] - EXT[0] + 2 * border);
slot_w = T + foot_clear;
foot_d = max(30, slot_w + 24);
foot_h = foot_floor + foot_lip;
ring_extra = outer_ring ? ring_width : 0;

if (has_text && stand == "foot")
    color(backing_color)
        translate([-foot_len / 2, y_cut - ring_extra - part_gap - foot_d, 0])
            difference() {
                hull() for (x = [2, foot_len - 2], y = [2, foot_d - 2])
                    translate([x, y, 0]) cylinder(r = 2, h = foot_h);
                translate([-1, (foot_d - slot_w) / 2, foot_floor])
                    cube([foot_len + 2, slot_w, foot_h]);
            }
