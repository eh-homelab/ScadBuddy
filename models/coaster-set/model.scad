// Coaster set — flat coasters with a pattern, letter, text or picture inlaid
// flush in a second colour, laid out as a set on one plate, with an optional
// border ring in a third colour, an optional recess underneath for a cork or
// felt pad, and an optional holder that the stack drops into.
//
// Coasters are for standing drinks on; they are not food-contact items.
//
// The inlay is 0.6 mm deep by default, so only the first or last few layers
// change colour. By default the decorated face prints up. "Decorated face
// down" puts it on the plate for the plate's smooth or textured finish; the
// underside recess always prints that way, because on the bed it would be
// an unsupported ceiling. The holder prints upright; its finger slots are
// open at the top, so nothing needs supports.
//
// The picture overlay takes an SVG or PNG (a `// file:svg,png` parameter;
// before ScadBuddy's file parameters land it is a plain text field that
// takes a bare file name in this directory, e.g. sample-overlay.svg).
//
// Written to the MakerWorld Parametric Model Maker customizer conventions so
// the same file works unchanged on MakerWorld and in ScadBuddy.
//
// The colour parameters are the extruder order: coaster_color is extruder 1,
// pattern_color 2, border_color 3, overlay_color 4, holder_color 5. Parts
// that are switched off take no extruder, and equal colours merge into one
// part and one filament.

/* [Coaster] */

// Outline of each coaster
shape = "round"; // [round:Round, square:Square, hexagon:Hexagon, rounded_square:Rounded square]

// Size in mm: diameter (round), side (square, rounded square), across the flats (hexagon)
size = 95; // [60:1:150]

// Thickness in mm
thickness = 5; // [3:0.5:10]

// Corner radius of the rounded square, in mm (at most half the size less 1 mm)
corner_radius = 14; // [2:1:40]

// Width of the border ring in its own colour, in mm (0 = no border)
border_width = 3; // [0:0.5:12]

// Depth of the inlay (pattern, border, picture), in mm
inlay_depth = 0.6; // [0.4:0.2:2]

// Which face prints on the plate (a cork recess always prints face down)
face = "up"; // [up:Decorated face up, down:Decorated face down - plate finish on top]

/* [Pattern] */

// What is inlaid in the middle of the coaster
pattern = "honeycomb"; // [none:None - plain, stripes:Diagonal stripes, chevron:Chevron, checker:Checkerboard, rings:Concentric rings, honeycomb:Honeycomb, dots:Polka dots, sunburst:Sunburst, monogram:Monogram letter, text:Text]

// Pattern spacing in mm (stripe pitch, cell or ring size)
spacing = 12; // [5:1:40]

// Line width of the line patterns, in mm
line_width = 2; // [0.8:0.2:6]

// Rotate the pattern, in degrees (not the monogram or text)
pattern_rotation = 0; // [0:5:180]

// Monogram letters: one per coaster, in order, repeating ("ABCD" gives each coaster its own)
letters = "A"; // 12

// Text for the text pattern
text = "CHEERS"; // 20

// Typeface for the monogram and text
font = "DejaVu Serif:style=Bold"; // font

// Swap coaster and pattern colours on every other coaster
alternate_colors = false;

/* [Overlay] */

// Picture to inlay: upload an SVG or PNG (or a bare file name in this model's directory, e.g. sample-overlay.svg). Empty = off.
overlay_file = ""; // file:svg,png

// File type: auto picks by extension; an SVG outline, or a PNG cut at a brightness threshold
overlay_type = "auto"; // [auto:Auto - by file extension, svg:SVG outline, png_threshold:Image threshold - PNG]

// Picture width as a percentage of the coaster size
overlay_scale = 60; // [10:5:100]

// Move the picture right, mm
overlay_x = 0; // [-50:1:50]

// Move the picture up, mm
overlay_y = 0; // [-50:1:50]

// Rotate the picture, degrees
overlay_rotation = 0; // [-180:5:180]

// PNG: pixels darker than this brightness (percent) form the picture
image_threshold = 50; // [1:1:99]

// Swap picture and background (fills the pattern area except the picture)
overlay_invert = false;

// Clear the pattern under and around the picture
overlay_clears_pattern = true;

/* [Set] */

// How many coasters
count = 4; // [1:1:12]

// Gap between parts on the plate, in mm
gap = 5; // [3:1:20]

// Underside
underside = "plain"; // [plain:Plain, recess:Recess for a cork or felt pad]

// Recess depth, in mm (2 for 2 mm cork, 1 for felt; with the recess only)
recess_depth = 2; // [0.6:0.2:4]

// Rim left around the recess, in mm (with the recess only)
recess_rim = 4; // [2:0.5:15]

// Add a holder the stack of coasters drops into
holder = false;

// Clearance between the coasters and the holder, per side, in mm
holder_clearance = 1; // [0.4:0.1:3]

/* [Colors] */

// Coaster colour (extruder 1)
coaster_color = "#1E3A5F"; // color

// Pattern, letter and text colour (extruder 2)
pattern_color = "#F2C14E"; // color

// Border ring colour (extruder 3)
border_color = "#F2F2F2"; // color

// Picture colour (extruder 4)
overlay_color = "#E4572E"; // color

// Holder colour (extruder 5)
holder_color = "#1E3A5F"; // color

/* [Hidden] */

$fn = 96;

BED_X = 300;          // H2C plate with both nozzles
BED_Y = 320;

d = inlay_depth;
face_down = face == "down" || underside == "recess";
// The recess leaves at least 1.2 mm of solid above the inlay; a thin coaster
// with a deep inlay gets a shallower recess, or none, and says so.
recess_max = max(0, thickness - d - 1.2);
recess_d = underside == "recess" ? min(recess_depth, recess_max) : 0;
if (underside == "recess" && recess_depth > recess_max)
    echo(str("NOTE: recess reduced from ", recess_depth, " to ", recess_d,
             " mm to leave 1.2 mm above the ", d, " mm inlay",
             recess_d == 0 ? "; no recess cut (raise thickness or lower inlay_depth)" : ""));

ext_x = shape == "hexagon" ? size / cos(30) : size;
ext_y = size;
cr = min(corner_radius, size / 2 - 1);

// Coaster outline grown (o > 0) or shrunk (o < 0) by o.
module shape_2d(o = 0) {
    if (shape == "round") circle(d = size + 2 * o);
    else if (shape == "square") offset(delta = o) square(size, center = true);
    else if (shape == "hexagon") offset(delta = o) circle(r = size / 2 / cos(30), $fn = 6);
    else offset(r = cr + o) square(size - 2 * cr, center = true);
}

// ---- pattern area --------------------------------------------------------------

has_border = border_width > 0;
pat_inset = border_width + 2;              // plain gap between border and pattern
pat_size = size - 2 * pat_inset;
R = size * 0.75;                           // half-extent the patterns fill

module pattern_area() { shape_2d(-pat_inset); }

module stripes_2d() {
    for (i = [-ceil(R / spacing) : ceil(R / spacing)])
        translate([i * spacing, 0]) square([line_width, 2 * R], center = true);
}

module chevron_2d() {
    amp = spacing / 2;
    for (j = [-ceil(R / spacing) : ceil(R / spacing)], k = [-ceil(R / spacing) - 1 : ceil(R / spacing)]) {
        y = j * spacing;
        x = k * spacing;
        hull() { translate([x, y]) circle(d = line_width, $fn = 16);
                 translate([x + spacing / 2, y + amp]) circle(d = line_width, $fn = 16); }
        hull() { translate([x + spacing / 2, y + amp]) circle(d = line_width, $fn = 16);
                 translate([x + spacing, y]) circle(d = line_width, $fn = 16); }
    }
}

module checker_2d() {
    n = ceil(R / spacing);
    for (i = [-n : n], j = [-n : n]) if ((i + j) % 2 == 0)
        translate([i * spacing, j * spacing]) square(spacing, center = true);
}

module rings_2d() {
    for (r = [spacing / 2 : spacing : R])
        difference() { circle(r = r + line_width / 2); circle(r = max(0.01, r - line_width / 2)); }
    circle(r = min(spacing / 4, line_width));
}

module honeycomb_2d() {
    rc = spacing / 2 / cos(30);                    // centre to corner of a cell
    n = ceil(R / spacing) + 1;
    for (i = [-n : n], j = [-n : n]) {
        x = i * 1.5 * rc;
        y = j * spacing + (i % 2 == 0 ? 0 : spacing / 2);
        translate([x, y]) difference() {
            circle(r = rc + line_width / 2 / cos(30), $fn = 6);
            circle(r = rc - line_width / 2 / cos(30), $fn = 6);
        }
    }
}

module dots_2d() {
    n = ceil(R / spacing) + 1;
    for (i = [-n : n], j = [-n : n])
        translate([i * spacing + (j % 2 == 0 ? 0 : spacing / 2), j * spacing * cos(30)])
            circle(d = spacing * 0.55, $fn = 32);
}

module sunburst_2d() {
    rays = max(8, 2 * round(PI * pat_size / 2 / spacing / 2));
    for (k = [0 : 2 : rays - 1])
        polygon([[0, 0], R * 2 * [cos(360 * k / rays), sin(360 * k / rays)],
                 R * 2 * [cos(360 * (k + 1) / rays), sin(360 * (k + 1) / rays)]]);
}

// Shrink-only fit (see models/name-sign): resize() scales to the bounding box
// of its children, and a hair-thin bar of the target length makes that box at
// least the target, so text already smaller is left alone and larger text is
// scaled down uniformly.
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
module fit(w, h) { fit_y(h) fit_x(w) children(); }

module monogram_2d(i) {
    ch = letters == "" ? "" : letters[i % len(letters)];
    if (ch != "" && ch != " ")
        fit(pat_size * 0.62, pat_size * 0.62)
            text(ch, size = pat_size * 0.62, font = font, halign = "center", valign = "center");
}

module text_2d() {
    if (text != "") {
        // A 0.88 x 0.3 box fits inside every outline's pattern area.
        fit(pat_size * 0.88, pat_size * 0.3)
            text(text, size = pat_size * 0.18, font = font, halign = "center", valign = "center");
    }
}

if (pattern == "text" && text == "")
    echo("NOTE: pattern is Text but text is empty; the coasters are plain");
if (pattern == "monogram" && len([for (ch = letters) if (ch != " ") ch]) == 0)
    echo("NOTE: pattern is Monogram but letters is empty; the coasters are plain");

module pattern_raw(i) {
    if (pattern == "monogram") monogram_2d(i);
    else if (pattern == "text") text_2d();
    else rotate(pattern_rotation) {
        if (pattern == "stripes") rotate(45) stripes_2d();
        else if (pattern == "chevron") chevron_2d();
        else if (pattern == "checker") checker_2d();
        else if (pattern == "rings") rings_2d();
        else if (pattern == "honeycomb") honeycomb_2d();
        else if (pattern == "dots") dots_2d();
        else if (pattern == "sunburst") sunburst_2d();
    }
}

// ---- overlay (picture) -----------------------------------------------------------

// overlay_file reaches import() and surface() verbatim, and OpenSCAD reads any
// path it is given: only a bare file name in the model's own directory is
// accepted. A path, a leading dot or a backslash turns the overlay off.
function safe_file(f) =
    f != "" && len(search("/", f)) == 0 && len(search("\\", f)) == 0 && f[0] != ".";

OVERLAY_REFUSED = overlay_file != "" && !safe_file(overlay_file);
if (OVERLAY_REFUSED)
    echo(str("NOTE: overlay_file \"", overlay_file,
             "\" is not a bare file name in the model directory; overlay off"));
OVERLAY_ON = safe_file(overlay_file);

function lower(s) = s == "" ? "" : chr([for (c = s) let (o = ord(c)) (o >= 65 && o <= 90) ? o + 32 : o]);
function ends_with(f, suffix) =
    len(f) >= len(suffix)
    && [for (i = [0 : len(suffix) - 1]) f[len(f) - len(suffix) + i]] == [for (c = suffix) c];
// "auto" reads the extension (any case), so an uploaded PNG needs no second setting.
// "png_threshold" was "image_threshold" until #318 renamed it (it shared a name
// with the numeric image_threshold parameter); saved presets and past outputs
// still hold the old value, so both force the image reader.
// ScadBuddy accepts the old value in a render or a preset save (#432):
// retired overlay_type = "image_threshold"
OVERLAY_IS_IMAGE = overlay_type == "png_threshold" || overlay_type == "image_threshold"
    || (overlay_type == "auto" && ends_with(lower(overlay_file), ".png"));

// An SVG is imported as-is; an image goes through surface(), whose height is
// the pixel brightness (0-100), and the pixels darker than image_threshold
// are kept. A missing file only logs an error and gives an empty picture, so
// the coasters still render.
module overlay_source() {
    if (OVERLAY_IS_IMAGE)
        difference() {
            projection() surface(file = overlay_file, center = true);
            projection(cut = true)
                translate([0, 0, -image_threshold]) surface(file = overlay_file, center = true);
        }
    else
        import(overlay_file, center = true);
}

module overlay_placed() {
    translate([overlay_x, overlay_y]) rotate(overlay_rotation)
        resize([size * overlay_scale / 100, 0], auto = true) overlay_source();
}

module overlay_2d() {
    intersection() {
        pattern_area();
        if (overlay_invert) difference() { pattern_area(); overlay_placed(); }
        else overlay_placed();
    }
}

// ---- one coaster, parts by colour role ----------------------------------------------

// The inlay artwork is drawn as seen from above the decorated face; face
// down, it is mirrored so it reads correctly once the coaster is turned over.
module face_2d() { if (face_down) mirror([1, 0]) children(); else children(); }

module border_2d() { if (has_border) difference() { shape_2d(0); shape_2d(-border_width); } }

module pattern_2d(i) {
    difference() {
        intersection() { pattern_area(); pattern_raw(i); }
        if (OVERLAY_ON && overlay_clears_pattern) offset(delta = 1) overlay_2d();
        if (OVERLAY_ON) overlay_2d();
    }
}

z_inlay = face_down ? 0 : thickness - d;

module inlay(z = z_inlay) { translate([0, 0, z]) linear_extrude(d) children(); }

module body(i) {
    difference() {
        linear_extrude(thickness) shape_2d(0);
        translate([0, 0, z_inlay - (face_down ? 1 : 0)]) linear_extrude(d + 1) face_2d() {
            border_2d();
            pattern_2d(i);
            if (OVERLAY_ON) overlay_2d();
        }
        if (recess_d > 0)
            translate([0, 0, thickness - recess_d]) linear_extrude(recess_d + 1) shape_2d(-recess_rim);
    }
}

// ---- layout ------------------------------------------------------------------------

h_wall = 2.4;
h_base = 2.4;
h_in = holder_clearance;
// A hexagon grown by delta gains delta across the flats, delta / cos(30)
// across the corners.
h_ext_x = ext_x + 2 * (h_in + h_wall) / (shape == "hexagon" ? cos(30) : 1);
h_ext_y = ext_y + 2 * (h_in + h_wall);
// Coasters sit in a grid of cells their own size. The holder is a few mm
// larger than a coaster, so it does not take a grid cell (that would size
// every cell for it, #411): it goes beside the grid, below it, or in the
// empty end of the last row, whichever fits and is squarest.
px = ext_x + gap;
py = ext_y + gap;
HOLDER_MODES = ["beside", "below", "row end"];

// n coasters in c columns, the holder (if any) placed by mode m:
// [plate width, plate height, holder corner x, y, grid x] from the top left,
// or undef where the mode does not apply (no empty end in a full last row).
// Below the grid, the narrower of grid and holder is centred on the other.
function arrange(c, n, m) =
    let (R = ceil(n / c), k = n - (R - 1) * c, gw = c * px - gap, gh = R * py - gap,
         bw = max(gw, h_ext_x))
    !holder ? [gw, gh, 0, 0, 0]
    : m == 0 ? [c * px + h_ext_x, max(gh, h_ext_y), c * px, 0, 0]
    : m == 1 ? [bw, R * py + h_ext_y, (bw - h_ext_x) / 2, R * py, (bw - gw) / 2]
    : k < c ? [max(gw, k * px + h_ext_x), (R - 1) * py + h_ext_y, k * px, (R - 1) * py, 0]
    : undef;
function fits(a) = is_list(a) && a[0] <= BED_X && a[1] <= BED_Y;
// Every arrangement of n that fits, as [c, m, W, H, hx, hy, gx].
function options(n) = [for (c = [1 : n], m = holder ? [0 : 2] : [0])
                       let (a = arrange(c, n, m)) if (fits(a)) concat([c, m], a)];
// Most coasters that fit (up to count), then the squarest arrangement.
function best(n) =
    let (o = options(n), sc = [for (a = o) max(a[2], a[3])])
    len(o) == 0 ? undef : o[search(min(sc), sc)[0]];
function layout(n) = (!is_undef(best(n)) || n == 1) ? [n, best(n)] : layout(n - 1);
LAYOUT = layout(count);
N = LAYOUT[0];
if (N < count)
    echo(str("NOTE: only ", N, N == 1 ? " coaster" : " coasters", " of ", count, N == 1 ? " fits" : " fit",
             " on the plate; print the rest as a second plate"));

// A big coaster and its holder can be too big to sit side by side or one
// above the other even alone (150 mm round with a 20 mm gap: 150 + 20 +
// 156.8 is deeper than the plate, side by side wider). Then the one coaster
// sits above the holder, with the gap cut to fit.
STACKED = holder && is_undef(best(1));
s_gap = min(gap, BED_Y - ext_y - h_ext_y);
// 150 mm + a 3 mm clearance holder leaves 9.2 mm; a wider size or clearance
// range must fail here, not overlap the coaster and the holder.
assert(!STACKED || s_gap >= 0, str("a ", size, " mm coaster and its holder do not fit the plate"));
if (STACKED && s_gap < gap)
    echo(str("NOTE: gap reduced from ", gap, " to ", s_gap, " mm to fit the coaster and the holder on the plate"));

// BEST is undef when STACKED; every use below sits in the false branch of a
// STACKED ternary, which OpenSCAD evaluates lazily, so undef is never indexed.
BEST = LAYOUT[1];
COLS = STACKED ? 1 : BEST[0];
ROWS = STACKED ? 1 : ceil(N / COLS);         // rows of coasters; the holder is extra
W = STACKED ? max(ext_x, h_ext_x) : BEST[2];
assert(!STACKED || W <= BED_X, str("a ", size, " mm coaster's holder is wider than the plate"));
H = STACKED ? ext_y + s_gap + h_ext_y : BEST[3];
function pos(i) = STACKED ? [0, H / 2 - ext_y / 2]
                : [-W / 2 + BEST[6] + ext_x / 2 + (i % COLS) * px, H / 2 - ext_y / 2 - floor(i / COLS) * py];
h_pos = STACKED ? [0, -H / 2 + h_ext_y / 2]
      : holder ? [-W / 2 + BEST[4] + h_ext_x / 2, H / 2 - BEST[5] - h_ext_y / 2] : [0, 0];
if (holder)
    echo(HOLDER = STACKED ? "stacked" : HOLDER_MODES[BEST[1]]);

// Holder: stack height is the coasters that fit, 70% of it is walled.
stack = count * thickness;
h_height = h_base + max(10, 0.7 * stack);
slot_w = min(0.4 * size, 40);
module holder_part() {
    difference() {
        linear_extrude(h_height) shape_2d(h_in + h_wall);
        translate([0, 0, h_base]) linear_extrude(h_height) shape_2d(h_in);
        // Finger slots front and back, open at the top.
        translate([-slot_w / 2, -h_ext_y, h_base + 3]) cube([slot_w, 2 * h_ext_y, h_height]);
    }
}

echo(COASTERS = [N, COLS, ROWS, W, H, face_down, recess_d]);

function swapped(i) = alternate_colors && i % 2 == 1;

for (i = [0 : N - 1]) translate(pos(i)) {
    color(swapped(i) ? pattern_color : coaster_color) body(i);
    if (pattern != "none")
        color(swapped(i) ? coaster_color : pattern_color) inlay() face_2d() pattern_2d(i);
    if (has_border) color(border_color) inlay() border_2d();
    if (OVERLAY_ON) color(overlay_color) inlay() face_2d() overlay_2d();
}

if (holder) color(holder_color) translate(h_pos) holder_part();
