// Bookmark — a flat, printable bookmark with a choice of outlines, a picture
// used as a mask (the outline itself, cut-through negative space, a flush
// colour inlay, or raised / recessed relief), a second picture as a colour
// overlay, optional text along the strip, a coloured rim, two-tone top
// layers, colour stripes and tassel beads. The page-corner shape is a
// triangular pocket that slips over the corner of a page.
//
// Everything prints flat, face up, without supports. The page-corner pocket
// has a roof that bridges between its two joined edges.
//
// Pictures are uploaded through ScadBuddy's file widget (`// file:svg,png`),
// which stores the upload beside model.scad under a generated bare name, or
// named directly from files in the model's own directory: an SVG is imported
// as its outline; a PNG is read through surface() and the pixels darker than
// image_threshold are kept. Only a bare file name is accepted (no folders, no
// leading dot); anything else is refused with a NOTE and treated as empty. A
// name that does not exist only logs an ERROR from import() and the bookmark
// renders without the picture (a custom outline falls back to the classic
// rounded rectangle).
//
// Colours: each distinct colour is one part and one filament. The colour
// parameters, in source order, are the extruder order: base_color,
// stripe_color_2 .. stripe_color_6, topper_color, border_color, top_color,
// text_color, mask_color, overlay_color, bead_color, bead_color_2. A colour
// the chosen options do not use produces no part and takes no extruder.
//
// Written to the MakerWorld Parametric Model Maker customizer conventions.

/* [Shape] */

// Outline of the bookmark
shape = "tassel"; // [classic:Classic - rounded rectangle, tassel:Tassel - with a cord hole, pointed:Pointed end, ribbon:Ribbon - swallowtail end, corner:Page corner clip, rounded_tab:Rounded tab top, scalloped:Scalloped edges, star_top:Star on top, heart_top:Heart on top, custom_mask:Custom - outline from the mask file]

// Length in mm, top to bottom (Y)
length = 150; // [60:5:250]

// Width of the strip in mm (X)
width = 40; // [20:1:80]

// Thickness in mm
thickness = 2; // [1.2:0.2:4]

// Corner radius in mm; also rounds the pointed and ribbon ends
corner_radius = 4; // [1:0.5:15]

// Cord hole diameter in mm (tassel shape)
hole_diameter = 5; // [3:0.5:10]

// Cord hole centre, mm below the top end
hole_from_top = 9; // [5:0.5:30]

// Punch the cord hole in the other shapes too
hole_on_any_shape = false;

// Pointed and ribbon shapes: length of the point, or depth of the notch, mm
end_length = 18; // [5:1:60]

// Scalloped shape: scallop diameter in mm
scallop_size = 8; // [4:0.5:16]

// Star / heart on top: width of the shape in mm
topper_size = 56; // [20:1:100]

/* [Corner clip] */

// Page corner: length of the two joined edges, mm
corner_size = 55; // [30:1:100]

// Page corner: air gap the page slides into, mm (one page is about 0.1 mm)
page_gap = 0.6; // [0.3:0.1:1.5]

// Page corner: thickness of the front and back plates, mm
plate_thickness = 1; // [0.6:0.2:2]

// Page corner: width of the two joined edges, mm
wall_width = 4; // [2:0.5:10]

/* [Mask] */

// Mask picture. Upload an SVG or PNG (or a file name in this model's directory: sample-lattice.svg, sample-cat.svg, sample-leaf.png). Empty = off.
mask_file = "sample-lattice.svg"; // file:svg,png

// File type: auto picks by the name (.png is read as an image, anything else as an SVG outline)
mask_type = "auto"; // [auto:Auto - by file extension, svg:SVG outline, image_threshold:Image threshold - PNG]

// What the picture does
mask_mode = "cutout"; // [outline:Outline - the picture is the bookmark shape, cutout:Cutout - cut through as negative space, inlay:Inlay - flush colour in the top layers, emboss:Emboss - raised, deboss:Deboss - recessed]

// How the picture is fitted into its area
mask_fit = "fit"; // [fit:Fit inside - keep aspect, fill:Fill and crop - keep aspect, stretch:Stretch to the area]

// Picture size as a percentage of its area
mask_scale = 100; // [10:5:300]

// Move the picture right, mm
mask_x = 0; // [-100:1:100]

// Move the picture up, mm
mask_y = 0; // [-150:1:150]

// Rotate the picture, degrees
mask_rotation = 0; // [-180:5:180]

// Swap picture and background (cutout: keep the picture and cut the rest out inside a frame)
mask_invert = false;

// Repeat the picture this many times along the strip (not for outlines)
mask_repeat = 1; // [1:1:10]

// Images: pixels darker than this brightness (percent) form the picture (mask and overlay)
image_threshold = 50; // [1:1:99]

// Thinnest bridge left between cutouts and thinnest part of an outline, mm
min_feature = 1.2; // [0.8:0.1:3]

// Cutout: solid margin kept along the edge, mm (raised to the rim width plus min_feature)
edge_margin = 3; // [1:0.5:10]

// Outline: grow the picture by this much so thin parts stay printable, mm
outline_border = 1.5; // [0:0.5:6]

// Cutout: bars that tie loose pieces to the frame (auto = centre cross when inverted, else none)
tie_bars = "auto"; // [auto:Auto, none:None, centre:Centre cross, grid:Grid]

// Cutout: spacing of the grid tie bars, mm
tie_pitch = 15; // [5:1:50]

/* [Overlay] */

// Overlay picture, in its own colour. Upload an SVG or PNG (or a file name in this model's directory: sample-overlay.svg, sample-leaf.png). Empty = off.
overlay_file = "sample-overlay.svg"; // file:svg,png

// File type: auto picks by the name (.png is read as an image, anything else as an SVG outline)
overlay_type = "auto"; // [auto:Auto - by file extension, svg:SVG outline, image_threshold:Image threshold - PNG]

// Flush inlay in the top layers, or raised on top
overlay_style = "inlay"; // [inlay:Flush inlay, raised:Raised]

// Picture width as a percentage of the bookmark width
overlay_scale = 55; // [10:5:300]

// Move the picture right, mm (0 = centre of the strip)
overlay_x = 0; // [-100:1:100]

// Move the picture up, mm (0 = centre of the strip)
overlay_y = 46; // [-150:1:150]

// Rotate the picture, degrees
overlay_rotation = 0; // [-180:5:180]

// Swap picture and background
overlay_invert = false;

/* [Text] */

// Text on the bookmark (empty = none)
label = "Keep reading"; // 40

// Typeface (the app fills this dropdown from the fonts installed in the image)
font = "Lobster Two:style=Bold"; // font

// Text direction
text_direction = "vertical"; // [vertical:Along the strip - reads bottom to top, horizontal:Across the strip]

// Flush inlay in the top layers, or raised on top
text_style = "inlay"; // [inlay:Flush inlay, raised:Raised]

// Letter height in mm; auto-fit only ever shrinks it
text_size = 11; // [4:0.5:30]

// Shrink the text so it always fits the bookmark (never enlarges it)
auto_fit = true;

// Move the text along the strip, mm (0 = centre)
text_y = -12; // [-120:1:120]

/* [Layers] */

// Depth of flush inlays (text, overlay, mask inlay), mm
inlay_depth = 0.6; // [0.2:0.2:1.6]

// Height of raised parts and depth of the deboss, mm
relief = 0.6; // [0.2:0.2:2]

// Two-tone: the top layers take top_color
two_tone = false;

// Two-tone: thickness of the top colour, mm
top_thickness = 0.4; // [0.2:0.2:1.2]

// Rim around the edge in its own colour
border = true;

// Rim width, mm
border_width = 2; // [1:0.5:6]

// Base colour: one colour, or bands across the strip
base_style = "solid"; // [solid:Solid, stripes:Stripes across the strip]

// Number of stripe colours (base_color then stripe_color_2 ...)
stripe_count = 4; // [2:1:6]

/* [Beads] */

// Tassel beads printed beside the bookmark (only with a cord hole)
bead_count = 2; // [0:1:6]

// Bead diameter, mm
bead_size = 12; // [8:1:20]

// Bead hole diameter, mm
bead_hole = 4; // [2:0.5:8]

/* [Colours] */

// Base colour (extruder 1); the first stripe
base_color = "#2E86DE"; // color

// Stripe 2 (extruder 2)
stripe_color_2 = "#F8A5C2"; // color

// Stripe 3 (extruder 3)
stripe_color_3 = "#1DD1A1"; // color

// Stripe 4 (extruder 4)
stripe_color_4 = "#A55EEA"; // color

// Stripe 5 (extruder 5)
stripe_color_5 = "#FF6B81"; // color

// Stripe 6 (extruder 6)
stripe_color_6 = "#48DBFB"; // color

// Star / heart on top
topper_color = "#EE5A24"; // color

// Rim around the edge
border_color = "#FECA57"; // color

// Two-tone top layers
top_color = "#222F3E"; // color

// Text
text_color = "#FFFFFF"; // color

// Mask inlay or emboss
mask_color = "#10AC84"; // color

// Overlay
overlay_color = "#FF4F8B"; // color

// Beads 1, 3, 5
bead_color = "#FF9F43"; // color

// Beads 2, 4, 6
bead_color_2 = "#EE5253"; // color

/* [Hidden] */

$fn = 64;

// Diagnostics for verify.sh, each one colour: "solid" the whole bookmark
// without beads, "face" the top face's solid area, "thin" only the parts of
// it thinner than min_feature, "mask" / "allowed" / "text" the plan regions.
debug = "none";

STRIPES = [base_color, stripe_color_2, stripe_color_3, stripe_color_4, stripe_color_5, stripe_color_6];

BIG = 1000;           // larger than any bookmark
SKIN = 0.4;           // material always left under a pocket or recess
TEXT_PAD = 2;         // clear space around the text

// ===========================================================================
// File guard: only a bare file name in the model's directory is read.
// ===========================================================================

// No "/" or "\\" means no directory part, so ".." can only traverse as the
// whole name, and the leading-dot rule refuses that (and dotfiles).
function safe_file(f) = is_string(f) && f != "" && len(search("/", f)) == 0 && len(search("\\", f)) == 0
                        && f[0] != ".";

if (mask_file != "" && !safe_file(mask_file))
    echo(str("NOTE: mask_file \"", mask_file, "\" ignored - only a bare file name in the model's directory is accepted"));
if (overlay_file != "" && !safe_file(overlay_file))
    echo(str("NOTE: overlay_file \"", overlay_file, "\" ignored - only a bare file name in the model's directory is accepted"));

// Type "auto" reads a name ending .png (any case) as an image and anything
// else as an SVG outline; "svg" / "image_threshold" force the reader.
function lower(c) = let (o = ord(c)) (o >= 65 && o <= 90) ? chr(o + 32) : c;
function ext_is(f, e) = len(f) > len(e)
    && [for (i = [0 : len(e) - 1]) lower(f[len(f) - len(e) + i])] == [for (i = [0 : len(e) - 1]) e[i]];
function file_type(f, type) = type != "auto" ? type : ext_is(f, ".png") ? "image_threshold" : "svg";

// A picture in 2D, centred. An SVG is imported as-is; an image goes through
// surface(), whose height is the pixel brightness (0-100), and the pixels
// darker than image_threshold are kept.
module file_2d(f, type) {
    if (safe_file(f)) {
        if (file_type(f, type) == "image_threshold")
            difference() {
                projection() surface(file = f, center = true);
                projection(cut = true)
                    translate([0, 0, -image_threshold]) surface(file = f, center = true);
            }
        else
            import(f, center = true);
    }
}

// ===========================================================================
// Geometry tricks. OpenSCAD cannot ask "is this shape empty?" or "is it taller
// than h?", so both are answered with geometry.
// ===========================================================================

// Everything, if the child is non-empty; nothing if it is empty.
module flood() offset(delta = 1e4) hull() children();

// children(1) if children(0) is non-empty, else children(2).
module pick() {
    intersection() { children(1); flood() children(0); }
    difference() { children(2); flood() children(0); }
}

// Non-empty when the child reaches beyond +-h/2 in Y.
module taller_than(h) {
    difference() {
        hull() scale([1e-3, 1]) children();
        square([1, h + 0.01], center = true);
    }
}

// Fit a centred picture into a bw x bh box: "fit" keeps the aspect and stays
// inside, "fill" keeps the aspect, covers the box and is cropped to it,
// "stretch" matches both sides.
module fit_box(bw, bh, how) {
    if (how == "stretch")
        resize([bw, bh]) children();
    else if (how == "fill")
        intersection() {
            square([bw, bh], center = true);
            pick() {
                taller_than(bh) resize([bw, 0], auto = true) children();
                resize([bw, 0], auto = true) children();
                resize([0, bh], auto = true) children();
            }
        }
    else
        pick() {
            taller_than(bh) resize([bw, 0], auto = true) children();
            resize([0, bh], auto = true) children();
            resize([bw, 0], auto = true) children();
        }
}

// Shrink-only text fit (as in name-sign). resize() scales to the bounding box
// of its children; a hair-thin bar of the target length, parked at z=10,
// makes that box at least the target, so smaller text is left alone.
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

function dir(a) = [cos(a), sin(a)];
function fit_pts(P, W, H) =
    let (xs = [for (p = P) p[0]], ys = [for (p = P) p[1]],
         x0 = min(xs), x1 = max(xs), y0 = min(ys), y1 = max(ys))
    [for (p = P) [(p[0] - (x0 + x1) / 2) * W / (x1 - x0), (p[1] - (y0 + y1) / 2) * H / (y1 - y0)]];
function heart_pts(n = 72) = [for (i = [0 : n - 1]) let (t = 360 * i / n)
    [16 * pow(sin(t), 3), 13 * cos(t) - 5 * cos(2 * t) - 2 * cos(3 * t) - cos(4 * t)]];
function star_pts() = [for (k = [0 : 9]) (k % 2 == 0 ? 1 : 0.48) * dir(90 + 36 * k)];

// Round every corner, convex and concave, with radius r.
module round_all(r) offset(r = r) offset(r = -2 * r) offset(r = r) children();

// ===========================================================================
// Resolve the design
// ===========================================================================

L = length;
W = width;
CORNER = shape == "corner";
MASK_ON = safe_file(mask_file);
OVERLAY_ON = safe_file(overlay_file);
TEXT_ON = len(label) > 0;

// The mask file is the outline for the custom shape, or in outline mode.
OUTLINE_FROM_FILE = !CORNER && (shape == "custom_mask" || mask_mode == "outline");
MASK_EFFECT = MASK_ON && !OUTLINE_FROM_FILE && mask_mode != "outline";
if (CORNER && mask_mode == "outline")
    echo("NOTE: the page-corner shape keeps its triangle; mask_mode outline is ignored");
if (shape == "custom_mask" && MASK_ON && mask_mode != "outline")
    echo("NOTE: shape custom_mask uses the mask file as the outline; mask_mode is ignored");

HOLE = !CORNER && (shape == "tassel" || hole_on_any_shape);
HOLE_Y = L / 2 - hole_from_top;
BEADS = HOLE ? bead_count : 0;

TOPPER = shape == "star_top" || shape == "heart_top";
TS = min(topper_size, 2.5 * W);                          // topper width
TH = shape == "heart_top" ? TS * 0.9 : TS * 0.95;        // topper height
TOPPER_C = L / 2 - TH / 2;                               // topper centre (Y)

// Corner clip: right angle at the origin, legs along -X and -Y.
A = corner_size;
P = plate_thickness;
G = page_gap;

FACE_T = CORNER ? P : thickness;                         // thickness of the decorated slab
Z1 = CORNER ? 2 * P + G : thickness;                     // top of the body
DI = min(inlay_depth, FACE_T - SKIN);
DD = min(relief, FACE_T - SKIN);
TT = min(top_thickness, FACE_T - SKIN);
BW = border ? border_width : 0;
M = min_feature;

// Cutout keep-out from the edge.
KEEP = max(edge_margin, BW + M, CORNER ? wall_width : 0);

// Extent of the bookmark in plan: [x0, x1, y0, y1].
EXT = CORNER ? [-A, 0, -A, 0]
    : [-max(W, TOPPER ? TS : 0) / 2, max(W, TOPPER ? TS : 0) / 2, -L / 2, L / 2];

// Area the mask picture is fitted into: inside the cutout margin, below the
// cord hole. [centre x, centre y, width, height]
MB_Y1 = min(HOLE ? HOLE_Y - hole_diameter / 2 - KEEP : EXT[3] - KEEP,
            TOPPER ? TOPPER_C - TH / 2 - KEEP : EXT[3]);
MB = CORNER ? [-A / 2, -A / 2, A - 2 * KEEP, A - 2 * KEEP]
    : [0, (EXT[2] + KEEP + MB_Y1) / 2, W - 2 * KEEP, MB_Y1 - EXT[2] - KEEP];

// ===========================================================================
// Outline
// ===========================================================================

module rrect(w, h, r) {
    rr = min(r, min(w, h) / 2 - 0.01);
    offset(r = rr) square([w - 2 * rr, h - 2 * rr], center = true);
}

module builtin_outline(s) {
    R = corner_radius;
    E = min(end_length, L / 2);
    if (s == "pointed")
        round_all(min(R, W / 4))
            polygon([[0, -L / 2], [W / 2, -L / 2 + E], [W / 2, L / 2], [-W / 2, L / 2], [-W / 2, -L / 2 + E]]);
    else if (s == "ribbon")
        round_all(min(R, W / 8))
            polygon([[-W / 2, -L / 2], [0, -L / 2 + E], [W / 2, -L / 2], [W / 2, L / 2], [-W / 2, L / 2]]);
    else if (s == "rounded_tab")
        hull() {
            translate([0, L / 2 - W / 2]) circle(d = W);
            translate([0, -L / 2 + R]) offset(r = R) square([W - 2 * R, 0.01], center = true);
        }
    else if (s == "scalloped") {
        sr = scallop_size / 2;
        iw = W - 2 * sr; ih = L - 2 * sr;
        nx = max(1, round(iw / (1.6 * sr)));
        ny = max(1, round(ih / (1.6 * sr)));
        square([iw, ih], center = true);
        for (i = [0 : nx], sy = [-1, 1]) translate([-iw / 2 + i * iw / nx, sy * ih / 2]) circle(r = sr);
        for (j = [0 : ny], sx = [-1, 1]) translate([sx * iw / 2, -ih / 2 + j * ih / ny]) circle(r = sr);
    }
    else if (TOPPER && (s == "star_top" || s == "heart_top")) {
        topper_2d();
        // The strip runs up into the topper.
        top = s == "heart_top" ? TOPPER_C : TOPPER_C - 0.48 * TH / 2;
        translate([0, (-L / 2 + top) / 2]) rrect(W, top + L / 2, corner_radius);
    }
    else
        rrect(W, L, corner_radius);
}

module topper_2d() {
    translate([0, TOPPER_C])
        if (shape == "heart_top") polygon(fit_pts(heart_pts(), TS, TH));
        else round_all(min(2, TS / 20)) polygon(fit_pts(star_pts(), TS, TH));
}

module corner_triangle() {
    // Right angle at the origin; the acute tips rounded, the page corner square.
    // A rounded 45-degree tip loses sqrt(2) * r along the edge; start longer.
    r = min(corner_radius, A / 6);
    a = A + sqrt(2) * r;
    intersection() {
        offset(r = r) offset(delta = -r) polygon([[0, 0], [-a, 0], [0, -a]]);
        translate([-A, -A]) square([A, A]);
    }
    translate([-2 * r, -2 * r]) square([2 * r, 2 * r]);
}

// The mask picture placed for an outline: fitted inside W x L less the
// border, grown by outline_border and opened so nothing thinner than
// min_feature remains. Falls back to the classic outline when the file gives
// nothing.
module outline_art() {
    ob = outline_border;
    translate([mask_x, mask_y]) rotate(mask_rotation)
        fit_box(W - 2 * ob, L - 2 * ob, mask_fit) file_2d(mask_file, mask_type);
}
module file_outline() {
    offset(r = M / 2) offset(r = -M / 2) offset(r = outline_border) outline_art();
    difference() { rrect(W, L, corner_radius); flood() outline_art(); }
}

module outline_2d() {
    if (CORNER) corner_triangle();
    else if (OUTLINE_FROM_FILE && MASK_ON) file_outline();
    else builtin_outline(shape == "custom_mask" ? "classic" : shape);
}

module hole_2d() {
    if (HOLE) translate([0, HOLE_Y]) circle(d = hole_diameter);
}

// ===========================================================================
// Pictures, text and the cutout
// ===========================================================================

// The mask picture placed in its area (tiled with mask_repeat), inverted if asked.
module mask_art() {
    n = mask_repeat;
    translate([MB[0] + mask_x, MB[1] + mask_y])
        for (i = [0 : n - 1])
            translate([0, (i - (n - 1) / 2) * MB[3] / n])
                rotate(mask_rotation)
                    fit_box(MB[2] * mask_scale / 100, MB[3] / n * mask_scale / 100, mask_fit)
                        file_2d(mask_file, mask_type);
}

module mask_area() {
    translate([MB[0], MB[1]]) square([MB[2], MB[3]], center = true);
}

module mask_2d() {
    if (MASK_EFFECT) {
        if (mask_invert)
            difference() { intersection() { mask_area(); flood() mask_art(); } mask_art(); }
        else
            mask_art();
    }
}

REF_W = CORNER ? A : W;

module overlay_art() {
    translate([overlay_x, overlay_y]) rotate(overlay_rotation)
        resize([REF_W * overlay_scale / 100, 0], auto = true) file_2d(overlay_file, overlay_type);
}

module overlay_raw() {
    if (OVERLAY_ON) {
        if (overlay_invert)
            difference() { intersection() { square(2 * BIG, center = true); flood() overlay_art(); } overlay_art(); }
        else
            overlay_art();
    }
}

// Text box: [centre, rotation, length along the text, height across it].
T_ACROSS = W - 2 * (BW + TEXT_PAD);
T_ALONG = L - 2 * (BW + TEXT_PAD + 1) - (HOLE ? hole_from_top + hole_diameter / 2 : 0) - 2 * abs(text_y);
TC = 0.32 * A;
TBOX = CORNER ? [[-TC, -TC], -45,
                 max(5, 2 * sqrt(2) * TC - 2 * sqrt(2) * (wall_width + TEXT_PAD)),
                 max(3, 2 * (A - 2 * TC) / sqrt(2) - 2 * (BW + TEXT_PAD))]
     : text_direction == "horizontal" ? [[0, text_y], 0, max(5, T_ACROSS), max(3, T_ALONG)]
     : [[0, text_y], 90, max(5, T_ALONG), max(3, T_ACROSS)];

module text_raw() {
    text(label, size = text_size, font = font, halign = "center", valign = "center");
}

module text_raw_fitted() {
    if (auto_fit) fit_y(TBOX[3]) fit_x(TBOX[2]) text_raw();
    else text_raw();
}

module text_placed() {
    if (TEXT_ON) translate(TBOX[0]) rotate(TBOX[1]) text_raw_fitted();
}

// Where the cutout may go: inside the edge margin, clear of the cord hole,
// the text and the overlay.
module cut_allowed() {
    difference() {
        offset(r = -KEEP) outline_2d();
        if (HOLE) translate([0, HOLE_Y]) circle(d = hole_diameter + 2 * KEEP);
        offset(r = M) text_placed();
        offset(r = M) overlay_raw();
        if (TOPPER) offset(r = M) topper_2d();
    }
}

TIES = tie_bars == "auto" ? (mask_invert ? "centre" : "none") : tie_bars;

// Tie bars: hair-lines that the erosion below widens to min_feature.
module ties_2d() {
    cx = MB[0] + mask_x; cy = MB[1] + mask_y;
    if (TIES == "centre") {
        translate([cx, 0]) square([0.02, 2 * BIG], center = true);
        translate([0, cy]) square([2 * BIG, 0.02], center = true);
    } else if (TIES == "grid") {
        n = ceil(BIG / tie_pitch);
        for (i = [-n : n]) {
            translate([cx + i * tie_pitch, 0]) square([0.02, 2 * BIG], center = true);
            translate([0, cy + i * tie_pitch]) square([2 * BIG, 0.02], center = true);
        }
    }
}

// The cut: shrunk by min_feature / 2 on every side, so any bridge between
// two holes (or a hole and the margin) is at least min_feature wide; opened
// (by M/4) to drop hole slivers too thin to print; then closed (by just under
// M/2), which re-joins a hole the shrinking split at a narrow neck instead of
// leaving a hair-thin wall across it. Different holes stay apart: they are
// at least min_feature apart after the shrinking, more than the closing spans.
module cut_2d() {
    if (MASK_EFFECT && mask_mode == "cutout")
        offset(r = -0.49 * M) offset(r = M / 4 + 0.49 * M) offset(r = -(M / 4 + M / 2))
            difference() {
                intersection() { mask_2d(); cut_allowed(); }
                ties_2d();
            }
}

// ===========================================================================
// Plan regions
// ===========================================================================

// The top face's solid area.
module face_2d() {
    difference() { outline_2d(); hole_2d(); cut_2d(); }
}

module rim_ring() {
    if (border) difference() { outline_2d(); offset(r = -BW) outline_2d(); }
}

// Where decorations go: the face inside the rim.
module inner_2d() {
    if (border) difference() { face_2d(); rim_ring(); }
    else face_2d();
}

// Decorations, highest priority first, each clipped to the face and to the
// ones before it so they never overlap.
module text_reg() {
    if (TEXT_ON) intersection() { text_placed(); inner_2d(); }
}
module overlay_reg() {
    if (OVERLAY_ON) difference() { intersection() { overlay_raw(); inner_2d(); } text_reg(); }
}
module mask_reg() {
    if (MASK_EFFECT && (mask_mode == "inlay" || mask_mode == "emboss" || mask_mode == "deboss"))
        difference() { intersection() { mask_2d(); inner_2d(); } text_reg(); overlay_reg(); }
}

module pockets_2d() {
    if (text_style == "inlay") text_reg();
    if (overlay_style == "inlay") overlay_reg();
    if (mask_mode == "inlay") mask_reg();
}
module deboss_2d() {
    if (mask_mode == "deboss") mask_reg();
}

// ===========================================================================
// Solids and parts
// ===========================================================================

module slab(z0, z1) {
    if (z1 - z0 > 1e-6) translate([0, 0, z0]) linear_extrude(z1 - z0) children();
}

// Everything but the raised parts, before colouring.
module body3d() {
    if (CORNER) {
        slab(0, P) outline_2d();
        slab(P, P + G) intersection() {
            outline_2d();
            union() {
                translate([-wall_width, -BIG]) square([wall_width, BIG]);
                translate([-BIG, -wall_width]) square([BIG, wall_width]);
            }
        }
        slab(P + G, Z1) face_2d();
    } else
        slab(0, Z1) face_2d();
}

module rim3d() {
    intersection() { body3d(); slab(-1, Z1 + 1) rim_ring(); }
}

// What the base colours leave to the others at the top of the face.
module top_cuts() {
    slab(Z1 - DI, Z1 + 1) pockets_2d();
    slab(Z1 - DD, Z1 + 1) deboss_2d();
    if (two_tone) slab(Z1 - TT, Z1 + 1) inner_2d();
}

module base3d() {
    difference() {
        body3d();
        if (border) slab(-1, Z1 + 1) rim_ring();
        top_cuts();
    }
}

// Plan region of base colour k: stripes 0-5, then 6 = the topper.
module base_region(k) {
    if (k == 6) topper_2d();
    else difference() {
        if (base_style == "stripes") {
            n = stripe_count;
            h = (EXT[3] - EXT[2]) / n;
            translate([-BIG, EXT[2] + k * h - (k == 0 ? 1 : 0)])
                square([2 * BIG, h + (k == 0 ? 1 : 0) + (k == n - 1 ? 1 : 0)]);
        } else if (k == 0)
            square(2 * BIG, center = true);
        if (TOPPER) topper_2d();
    }
}

module top3d() {
    difference() {
        slab(Z1 - TT, Z1) inner_2d();
        slab(Z1 - DI, Z1 + 1) pockets_2d();
        slab(Z1 - DD, Z1 + 1) deboss_2d();
    }
}

module text3d() {
    if (text_style == "inlay") slab(Z1 - DI, Z1) text_reg();
    else slab(Z1, Z1 + relief) text_reg();
}
module overlay3d() {
    if (overlay_style == "inlay") slab(Z1 - DI, Z1) overlay_reg();
    else slab(Z1, Z1 + relief) overlay_reg();
}
module mask3d() {
    if (mask_mode == "inlay") slab(Z1 - DI, Z1) mask_reg();
    else if (mask_mode == "emboss") slab(Z1, Z1 + relief) mask_reg();
}

// Tassel beads: chamfered 45 degrees top and bottom, hole upright.
module bead() {
    R = bead_size / 2; h = bead_size * 0.8; c = h * 0.3; rh = min(bead_hole / 2, R - 1.5);
    rotate_extrude($fn = 48) polygon([[rh, 0], [R - c, 0], [R, c], [R, h - c], [R - c, h], [rh, h]]);
}
module beads(parity) {
    for (i = [0 : BEADS - 1]) if (i % 2 == parity)
        translate([EXT[1] + 6 + bead_size / 2, EXT[3] - bead_size / 2 - i * (bead_size + 4), 0]) bead();
}

// Numbers verify.sh reads.
echo(BOOKMARK = [shape, EXT[0], EXT[1], EXT[2], EXT[3], Z1,
                 Z1 + ((TEXT_ON && text_style == "raised") || (OVERLAY_ON && overlay_style == "raised")
                       || (MASK_EFFECT && mask_mode == "emboss") ? relief : 0),
                 BEADS, KEEP, OUTLINE_FROM_FILE, MASK_EFFECT, P, G, A]);

if (debug == "face")
    color("#000000") linear_extrude(1) face_2d();
else if (debug == "solid")
    color("#000000") union() {
        body3d();
        if (TEXT_ON && text_style == "raised") text3d();
        if (OVERLAY_ON && overlay_style == "raised") overlay3d();
        if (MASK_EFFECT && mask_mode == "emboss") mask3d();
    }
else if (debug == "mask")
    color("#000000") linear_extrude(1) mask_2d();
else if (debug == "allowed")
    color("#000000") linear_extrude(1) cut_allowed();
else if (debug == "text")
    color("#000000") linear_extrude(1) text_placed();
else if (debug == "thin")
    color("#000000") linear_extrude(1) difference() {
        face_2d();
        offset(r = 0.95 * M / 2) offset(r = -0.95 * M / 2) face_2d();
    }
else {
    for (k = [0 : (base_style == "stripes" ? stripe_count - 1 : 0)])
        color(STRIPES[k]) intersection() { base3d(); slab(-1, Z1 + 1) base_region(k); }
    if (TOPPER)
        color(topper_color) intersection() { base3d(); slab(-1, Z1 + 1) base_region(6); }
    if (border) color(border_color) rim3d();
    if (two_tone) color(top_color) top3d();
    if (TEXT_ON) color(text_color) text3d();
    if (MASK_EFFECT && (mask_mode == "inlay" || mask_mode == "emboss")) color(mask_color) mask3d();
    if (OVERLAY_ON) color(overlay_color) overlay3d();
    if (BEADS > 0) color(bead_color) beads(0);
    if (BEADS > 1) color(bead_color_2) beads(1);
}
