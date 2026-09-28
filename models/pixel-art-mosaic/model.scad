// Pixel Art Mosaic — a flat multi-colour pixel-art plaque, fridge magnet or
// keychain. Each pixel is a square tile in its own colour, on a backing plate
// with an optional raised frame.
//
// The art comes from a built-in pattern (heart, star, smiley, mushroom,
// blocky green face) or from your own PNG. A PNG is read through surface(),
// whose height is the pixel brightness (0-100), resampled to `columns` x
// `rows` mosaic pixels, and each mosaic pixel takes the brightness band its
// centre falls in: up to 8 bands, darkest band = color_1. A PNG with exactly
// `columns` x `rows` pixels is reproduced pixel for pixel. Only a bare file
// name in the model's own directory is accepted (the upload widget stores
// files there); anything else is refused with a NOTE and the built-in pattern
// is used instead.
//
// Colour lives only in the top pixel layer (pixel_height), so the colour
// changes are confined to the last few layers. Everything prints flat, face
// up, without supports; magnet pockets open onto the bed and their roofs are
// short bridges.
//
// Colours: each distinct colour is one part and one filament. The colour
// parameters, in source order, are the extruder order: base_color (backing,
// frame and mount), background_color, then color_1 .. color_8. A colour the
// art does not use produces no part and takes no extruder.
//
// Written to the MakerWorld Parametric Model Maker customizer conventions.

/* [Picture] */

// Your own picture: upload a PNG (or a file name in this model's directory: sample-cat.png, sample-sunset.png). Empty = use the built-in pattern.
image_file = ""; // file:png

// How the picture's brightness becomes colours
image_type = "png_bands"; // [png_bands:Brightness bands - up to 8 colours, png_threshold:Image threshold - two colours]

// Brightness bands (colours) the picture is cut into; the darkest band is color_1
bands = 4; // [2:1:8]

// Image threshold: brightness (0-100) that splits the two colours
image_threshold = 50; // [1:1:99]

// Reverse the colour order: the lightest band becomes color_1
invert = false;

// Mosaic pixels across. The picture is resampled to this; use your PNG's own pixel width for exact pixel art.
columns = 16; // [4:1:64]

// Mosaic pixels down; use your PNG's own pixel height for exact pixel art
rows = 16; // [4:1:64]

// Use one end band as the background (then the Background setting applies to it)
png_background = "none"; // [none:None - every band is a colour, lightest:Lightest band is the background, darkest:Darkest band is the background]

/* [Pattern] */

// Built-in pixel art, used when no picture is given
pattern = "mushroom"; // [heart:Heart, star:Star, smiley:Smiley, mushroom:Mushroom, blocky_face:Blocky green face]

/* [Pixels] */

// Size of one mosaic pixel in mm. Shrunk automatically if the mosaic would not fit the 300 x 320 mm plate.
pixel_size = 5; // [2:0.5:15]

// Height of the coloured pixel layer in mm
pixel_height = 1.2; // [0.4:0.2:4]

// Groove between neighbouring pixels in mm, showing the backing colour. 0 = one flat, seamless surface.
pixel_gap = 0; // [0:0.1:1.5]

// Background pixels
background_mode = "fill"; // [fill:Filled in the background colour, empty:Empty - the backing shows through, cut:Cut away - the outline follows the art]

/* [Backing and frame] */

// Backing plate thickness in mm (grown automatically to hold the magnets)
base_thickness = 2; // [1:0.2:8]

// Frame width in mm around the mosaic. 0 = no frame.
frame_width = 3; // [0:0.5:15]

// How far the frame stands above the pixels, in mm
frame_raise = 0.6; // [0:0.2:3]

// Corner radius of the rectangular plate in mm
corner_radius = 2; // [0:0.5:10]

/* [Mount] */

// How it hangs or sticks
mount = "hanger"; // [none:None - flat plaque, hanger:Hanging tab at the top, keyring:Keyring loop at the top left, magnet:Magnet pockets in the back]

// Hole diameter in mm for the hanging tab or keyring loop
hole_diameter = 5; // [3:0.5:10]

// Magnet diameter in mm (the pocket adds magnet_clearance)
magnet_diameter = 10; // [4:0.5:25]

// Magnet thickness in mm (the pocket is 0.2 mm deeper)
magnet_thickness = 3; // [1:0.5:6]

// Number of magnet pockets
magnet_count = 1; // [1:One in the middle, 2:Two side by side, 4:Four in a square]

// Extra diameter of the magnet pocket in mm
magnet_clearance = 0.2; // [0:0.05:0.6]

/* [Colours] */

// Backing plate, frame and mount
base_color = "#263238"; // color

// Background pixels (Background: Filled)
background_color = "#81D4FA"; // color

// Colour 1 (pattern digit 1; darkest brightness band)
color_1 = "#1A1A1A"; // color

// Colour 2
color_2 = "#E53935"; // color

// Colour 3
color_3 = "#FFFFFF"; // color

// Colour 4
color_4 = "#F5CBA7"; // color

// Colour 5
color_5 = "#FDD835"; // color

// Colour 6
color_6 = "#43A047"; // color

// Colour 7
color_7 = "#1E88E5"; // color

// Colour 8 (lightest brightness band when 8 bands are used)
color_8 = "#FF80AB"; // color

/* [Hidden] */

$fn = 48;
BED_X = 300;          // H2C plate using both nozzles
BED_Y = 320;
PIN = 0.004;          // probe size, in mosaic pixels
TAB_WALL = 2.5;       // material around the mount hole
HOLE_CLEAR = 1.2;     // hole edge to the mosaic, at least
MAGNET_SKIN = 0.6;    // backing left above a magnet pocket
COLORS = [color_1, color_2, color_3, color_4, color_5, color_6, color_7, color_8];

// ===========================================================================
// Built-in patterns: one string per row, top row first. "." is background,
// "1".."8" pick color_1 .. color_8.
// ===========================================================================

PATTERNS = [
    ["heart", [
        "..111...111..",
        ".12221.12221.",
        "1223221222221",
        "1233222222221",
        "1232222222221",
        "1222222222221",
        ".12222222221.",
        "..122222221..",
        "...1222221...",
        "....12221....",
        ".....121.....",
        "......1......"]],
    ["star", [
        "......1......",
        ".....151.....",
        ".....151.....",
        "....15551....",
        "1111155511111",
        "1555555555551",
        ".15555555551.",
        "..155555551..",
        "...1555551...",
        "..155515551..",
        ".15551.15551.",
        ".1551...1551.",
        ".111.....111."]],
    ["smiley", [
        "....1111....",
        "..11555511..",
        ".1555555551.",
        ".1551551551.",
        "155515515551",
        "155555555551",
        "151555555151",
        "155155551551",
        ".1555111551.",
        ".1555555551.",
        "..11555511..",
        "....1111...."]],
    ["mushroom", [
        ".....1111.....",
        "...11223311...",
        "..1222233221..",
        ".123322222221.",
        ".133332222331.",
        "12333222223331",
        "12232222222321",
        "12222222222221",
        ".111144441111.",
        "...14144141...",
        "...14444441...",
        "...14444441...",
        "....111111...."]],
    ["blocky_face", [
        "66666666",
        "66666666",
        "61166116",
        "61166116",
        "66611666",
        "66111166",
        "66111166",
        "66166166"]]
];

function lookup_pattern(name) =
    let (hit = [for (p = PATTERNS) if (p[0] == name) p[1]]) len(hit) ? hit[0] : PATTERNS[0][1];

// ===========================================================================
// File guard: only a bare file name in the model's directory is read.
// ===========================================================================

// No "/" or "\\" means no directory part, so ".." can only traverse as the
// whole name, and the leading-dot rule refuses that (and dotfiles).
function safe_file(f) = is_string(f) && f != "" && len(search("/", f)) == 0 && len(search("\\", f)) == 0
                        && f[0] != ".";

if (image_file != "" && !safe_file(image_file))
    echo(str("NOTE: image_file \"", image_file,
             "\" ignored - only a bare file name in the model's directory is accepted; using the built-in pattern"));

USE_IMAGE = safe_file(image_file);
ART = lookup_pattern(pattern);

// Grid size in mosaic pixels.
G = USE_IMAGE ? columns : len(ART[0]);
R = USE_IMAGE ? rows : len(ART);

// ===========================================================================
// Brightness bands (picture mode). "png_threshold" is two bands split at
// image_threshold; "image_threshold" is accepted too (the value's name
// before #318) so saved presets keep working.
// ===========================================================================

THRESHOLD_MODE = image_type == "png_threshold" || image_type == "image_threshold";
N = THRESHOLD_MODE ? 2 : bands;
// Upper brightness bound of band b (1 = darkest); t(0) is below black.
function t(b) = b <= 0 ? -1 : b >= N ? 101 : THRESHOLD_MODE ? image_threshold : 100 * b / N;

BG_BAND = png_background == "lightest" ? N : png_background == "darkest" ? 1 : 0;
// Colour bands in order darkest..lightest, reversed by invert.
COLOUR_BANDS_UP = [for (b = [1 : N]) if (b != BG_BAND) b];
COLOUR_BANDS = invert ? [for (i = [len(COLOUR_BANDS_UP) - 1 : -1 : 0]) COLOUR_BANDS_UP[i]] : COLOUR_BANDS_UP;
// Colour index (1..8) of band b, or 0 for the background band.
function band_colour(b) = b == BG_BAND ? 0 : search(b, COLOUR_BANDS)[0] + 1;

// ===========================================================================
// Size.
// ===========================================================================

MAGNET = mount == "magnet";
TAB = mount == "hanger" || mount == "keyring";
TAB_R = hole_diameter / 2 + TAB_WALL;
// Room the tab takes beyond the mosaic's edge.
TAB_REACH = TAB ? hole_diameter / 2 + HOLE_CLEAR + TAB_R : 0;
PS_FIT = min((BED_X - 2 * frame_width - 2 * TAB_REACH) / G, (BED_Y - 2 * frame_width - 2 * TAB_REACH) / R);
PS = min(pixel_size, PS_FIT);
if (PS < pixel_size)
    echo(str("NOTE: pixel_size reduced from ", pixel_size, " to ", PS, " mm so the mosaic fits the plate"));

POCKET_DEPTH = magnet_thickness + 0.2;
B = MAGNET ? max(base_thickness, POCKET_DEPTH + MAGNET_SKIN) : base_thickness;
if (B > base_thickness)
    echo(str("NOTE: base_thickness raised from ", base_thickness, " to ", B, " mm to hold the magnets"));

GW = G * PS;          // mosaic size in mm
GH = R * PS;
TOP = B + pixel_height;
FRAME_TOP = TOP + frame_raise;
CUT = background_mode == "cut";

echo(MOSAIC = [G, R, PS, B, TOP, frame_width > 0 ? FRAME_TOP : TOP, USE_IMAGE ? N : 0]);

// ===========================================================================
// Mosaic regions, in mosaic-pixel units: pixel (i, j) is the unit square
// centred on (i, j), i = 0 .. G-1 left to right, j = 0 .. R-1 bottom to top.
// ===========================================================================

module cell(i, j) { translate([i - 0.5, j - 0.5]) square(1); }
module all_cells() { translate([-0.5, -0.5]) square([G, R]); }

// The picture as a heightfield, stretched so its pixel centres land on the
// mosaic's pixel centres (a hair wider, so the edge probes sit inside it).
module picture_3d() {
    translate([-PIN / 2, -PIN / 2, 0])
        resize([G - 1 + PIN, R - 1 + PIN, 0]) surface(file = image_file, center = false);
}

// Pixels whose centre is brighter than v: a thin probe stands on each pixel
// centre from height v up; the heightfield keeps the probes it reaches above
// v, and each kept probe is grown back to its whole pixel.
module brighter_than(v) {
    if (v < 0)
        // Every pixel, but only if the picture loaded (a missing file gives
        // an empty heightfield and so no mosaic at all).
        intersection() {
            all_cells();
            offset(delta = G + R) projection() picture_3d();
        }
    else if (v <= 100)
        offset(delta = (1 - PIN) / 2) projection() intersection() {
            picture_3d();
            for (i = [0 : G - 1], j = [0 : R - 1])
                translate([i - PIN / 2, j - PIN / 2, v]) cube([PIN, PIN, 200]);
        }
}

module band_2d(b) { difference() { brighter_than(t(b - 1)); brighter_than(t(b)); } }

// Colour c (1..8), or the background for c = 0.
module region_2d(c) {
    if (USE_IMAGE) {
        for (b = [1 : N]) if (band_colour(b) == c) band_2d(b);
    } else {
        ch = c == 0 ? "." : str(c);
        for (r = [0 : R - 1], i = [0 : G - 1])
            if (ART[r][i] == ch) cell(i, R - 1 - r);
    }
}

// Which colours the art uses, so empty colours make no part.
USED = USE_IMAGE
    ? [for (c = [1 : 8]) len([for (b = [1 : N]) if (band_colour(b) == c) b]) > 0]
    : [for (c = [1 : 8]) len([for (row = ART) for (ch = row) if (ch == str(c)) ch]) > 0];
HAS_BG = USE_IMAGE ? BG_BAND > 0 : len([for (row = ART) for (ch = row) if (ch == ".") ch]) > 0;

module art_2d() { for (c = [1 : 8]) if (USED[c - 1]) region_2d(c); }

// Grooves between pixels, in mosaic-pixel units.
module grooves_2d() {
    g = pixel_gap / PS;
    for (i = [0 : G]) translate([i - 0.5 - g / 2, -1]) square([g, R + 2]);
    for (j = [0 : R]) translate([-1, j - 0.5 - g / 2]) square([G + 2, g]);
}

// A mosaic region, in mm, centred on the origin, with the grooves cut.
module to_mm() {
    translate([-GW / 2, -GH / 2]) scale(PS) translate([0.5, 0.5]) difference() {
        children();
        if (pixel_gap > 0) grooves_2d();
    }
}
module to_mm_solid() { translate([-GW / 2, -GH / 2]) scale(PS) translate([0.5, 0.5]) children(); }

// ===========================================================================
// Backing, frame and mount.
// ===========================================================================

// The area the mosaic covers: every pixel, or only the art when cut.
module footprint_2d() {
    if (CUT) to_mm_solid() art_2d();
    else square([GW, GH], center = true);
}

module plate_2d() {
    if (CUT) offset(r = frame_width) footprint_2d();
    else {
        rr = min(corner_radius, frame_width + min(GW, GH) / 2 - 0.01);
        offset(r = rr) offset(delta = frame_width - rr) square([GW, GH], center = true);
    }
}

HOLE_AT = mount == "hanger"
    ? [0, GH / 2 + HOLE_CLEAR + hole_diameter / 2]
    : [-GW / 2 - (HOLE_CLEAR + hole_diameter / 2) / sqrt(2), GH / 2 + (HOLE_CLEAR + hole_diameter / 2) / sqrt(2)];

module tab_2d() {
    hull() {
        translate(HOLE_AT) circle(r = TAB_R);
        circle(r = TAB_R);
    }
}

MAGNETS = magnet_count == 4 ? [[-GW / 4, -GH / 4], [GW / 4, -GH / 4], [-GW / 4, GH / 4], [GW / 4, GH / 4]]
        : magnet_count == 2 ? [[-GW / 4, 0], [GW / 4, 0]] : [[0, 0]];

module backing() {
    difference() {
        union() {
            linear_extrude(B) plate_2d();
            if (TAB) linear_extrude(B) tab_2d();
            if (frame_width > 0)
                translate([0, 0, B]) linear_extrude(FRAME_TOP - B) difference() {
                    plate_2d();
                    footprint_2d();
                }
        }
        if (TAB) translate([HOLE_AT[0], HOLE_AT[1], -1]) cylinder(d = hole_diameter, h = FRAME_TOP + 2);
        if (MAGNET) for (m = MAGNETS)
            translate([m[0], m[1], -1]) cylinder(d = magnet_diameter + magnet_clearance, h = POCKET_DEPTH + 1);
    }
}

// ===========================================================================
// Output.
// ===========================================================================

module pixels(c) { translate([0, 0, B]) linear_extrude(pixel_height) to_mm() region_2d(c); }

color(base_color) backing();
if (background_mode == "fill" && HAS_BG) color(background_color) pixels(0);
for (c = [1 : 8]) if (USED[c - 1]) color(COLORS[c - 1]) pixels(c);
