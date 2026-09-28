// Alphabet tiles — one chunky tile per character of the text, laid out in rows
// on the plate: letters and numbers for learning to read and count, spelling
// names on the fridge, or matching games. The letter is a flush inlay (or
// raised, to feel with a finger) in its own colour, with an optional inlaid
// border ring in a third. Magnet pockets are optional, sized for 6 x 2 mm or
// 8 x 3 mm disc magnets: a glue-in pocket in the back, or a sealed cavity with
// a slot out through the top edge that the magnet is pushed into after
// printing, so there is no pause mid-print.
//
// Prints face up, flat on the bed, without supports: the magnet pocket or
// cavity is bridged over.
//
// Written to the MakerWorld Parametric Model Maker customizer conventions so
// the same file works unchanged on MakerWorld and in ScadBuddy.
//
// The colour parameters are the extruder order: tile_color is extruder 1,
// letter_color extruder 2, border_color extruder 3. The border only exists
// when switched on, so the defaults print in two colours.

/* [Text] */

// Characters to make tiles for, one tile each (spaces are skipped)
text = "ABC123"; // 24

// Typeface (the app fills this dropdown from the fonts installed in the image)
font = "DejaVu Sans:style=Bold"; // font

// Letter height as a percentage of the room inside the tile (wide letters like W shrink to fit)
letter_scale = 75; // [40:5:95]

// Underline 6 and 9 so they cannot be mixed up when a tile is turned over
underline_6_9 = true;

/* [Tile] */

// Tile shape
shape = "rounded_square"; // [rounded_square:Rounded square, circle:Circle, hexagon:Hexagon, scalloped:Scalloped, heart:Heart]

// Tile width and height in mm
tile_size = 30; // [20:1:50]

// Tile thickness in mm (raised automatically if a magnet needs more room)
thickness = 5; // [3:0.5:10]

// Corner radius in mm (rounded square only)
corner_radius = 5; // [0:0.5:12]

// Rounding on the tile's top edge in mm (0 = sharp)
edge_round = 1.2; // [0:0.2:2]

// Letter set flush into the top, or standing up from it
letter_style = "inlay"; // [inlay:Flush inlay, raised:Raised]

// Inlay depth, or how far raised letters stand up, in mm
letter_depth = 0.6; // [0.4:0.2:2]

// Gap between tiles on the plate in mm (shrinks if the rows would not fit the bed)
gap = 5; // [2:1:15]

/* [Border] */

// Add a border ring around the letter in its own colour
border = false;

// Border ring width in mm (with the border on; narrowed on small tiles to leave room for the letter)
border_width = 2; // [1:0.5:5]

// Distance from the tile's edge to the border in mm (at least the edge rounding; reduced on small tiles)
border_inset = 1.5; // [0.5:0.5:5]

/* [Magnets] */

// Magnet size (diameter x thickness)
magnet = "none"; // [none:None, 6x2:6 x 2 mm, 8x3:8 x 3 mm]

// How the magnet goes in (with a magnet only): glued into a pocket in the back, or pushed into a sealed cavity through a slot in the top edge (no pause)
mount = "glue_in"; // [glue_in:Glue-in pocket in the back, slide_in:Slide-in cavity through the top edge]

// Extra room around the magnet in mm, on the diameter and the depth
magnet_clearance = 0.2; // [0.1:0.05:0.4]

/* [Colours] */

// Tile colour (extruder 1)
tile_color = "#FFD54F"; // color

// Letter colour (extruder 2)
letter_color = "#1565C0"; // color

// Border ring colour (extruder 3)
border_color = "#E53935"; // color

/* [Hidden] */

$fn = 64;

// Bed the rows are wrapped to.
bed_w = 300;
// Bottom-edge set-in against elephant's foot.
foot = 0.3;
// Steps in the top-edge rounding.
round_steps = 4;
// Floor under a slide-in cavity, and material over a pocket or cavity.
magnet_skin = 0.6;
magnet_roof = 1.2;
// Slide-in: the slot's mouth is 0.2 mm narrower than the magnet (0.1 mm a
// side), so the magnet clicks past it and stays in.
detent = magnet_clearance / 2 + 0.1;
detent_len = 1.5;

// Test hook for verify.sh: "seat" renders each tile's overlap with a magnet of
// the nominal size in its seat; "sweep" with the magnet swept from its seat
// out through the slot; "sweep_small" the same with a magnet 0.3 mm smaller.
probe_magnet = "none";

S = tile_size;
chars = [for (ch = text) if (ch != " ") ch];
n = len(chars);

mag_d = magnet == "6x2" ? 6 : magnet == "8x3" ? 8 : 0;
mag_h = magnet == "6x2" ? 2 : magnet == "8x3" ? 3 : 0;
has_mag = mag_d > 0;
pocket_d = mag_d + magnet_clearance;
pocket_h = mag_h + magnet_clearance;
inlay_d = letter_style == "inlay" ? letter_depth : 0;
// Thickness a magnet needs: the pocket plus a roof under the inlay.
mag_needs = !has_mag ? 0
          : mount == "glue_in" ? pocket_h + magnet_roof + inlay_d
          : magnet_skin + pocket_h + magnet_roof + inlay_d;
T = max(thickness, mag_needs, edge_round + inlay_d + 1);

er = min(edge_round, T - inlay_d - 1);
inset_req = max(border_inset, er + 0.3);
has_border = border;

// Rows wrap at the bed width. If they would run off the bed's depth, the gap
// between tiles shrinks (to 2 mm at the least, where 24 tiles of any size fit).
bed_d = 320;
function cols_for(g) = max(1, floor((bed_w + g) / (S + g)));
function rows_for(g) = max(1, ceil(n / cols_for(g)));
function gap_fit(g) = g <= 2 || rows_for(g) * (S + g) - g <= bed_d ? g : gap_fit(g - 1);
G = gap_fit(gap);
cols = cols_for(G);
rows = rows_for(G);

// ---------------------------------------------------------------- shapes

// Heart: a square of side k on its corner with a circle of diameter k on each
// upper edge. It is h_w * k wide and (h_top + h_bot) * k tall; k makes the
// width S, and it is shifted so its bounding box is centred.
h_w = 1 / sqrt(2) + 1;
h_top = 1 / (2 * sqrt(2)) + 0.5;
h_bot = 1 / sqrt(2);
heart_k = S / max(h_w, h_top + h_bot);
heart_dy = (h_bot - h_top) / 2 * heart_k;

module heart_2d() {
    k = heart_k;
    translate([0, heart_dy])
        offset(r = S * 0.05) offset(r = -S * 0.05) union() {
            rotate(45) square(k, center = true);
            for (s = [-1, 1]) translate([s * k / (2 * sqrt(2)), k / (2 * sqrt(2))]) circle(d = k);
        }
}

// Scalloped: ten round bumps on a circle, reaching S / 2 at the bumps (one
// each side on the x axis, so it is exactly S wide; a valley at the top).
scallop_rb = S / 2 * 0.23;
scallop_r0 = S / 2 - scallop_rb;
module scalloped_2d() {
    union() {
        circle(r = scallop_r0 + 0.01);
        for (i = [0:9]) rotate(i * 36) translate([scallop_r0, 0]) circle(r = scallop_rb);
    }
}

module shape_2d() {
    if (shape == "circle") circle(d = S);
    else if (shape == "hexagon") circle(d = S, $fn = 6);
    else if (shape == "scalloped") scalloped_2d();
    else if (shape == "heart") heart_2d();
    else {
        cr = min(corner_radius, S / 2 - 0.5);
        if (cr > 0) offset(r = cr) square(S - 2 * cr, center = true);
        else square(S, center = true);
    }
}

// Room for the letter. The square gives a box of half-size room(); the other
// shapes a circle of radius room() that fits inside them, centred at box_y()
// (the heart's roomy part is above its middle).
// Half-size of the room with no margin at all.
base_r = shape == "circle" ? S / 2
       : shape == "hexagon" ? S / 2 * cos(30)
       : shape == "scalloped" ? scallop_r0
       : shape == "heart" ? heart_k * 0.62
       : S / 2;
// A border may take at most 60 % of that, so there is always room for a
// letter: on a small tile a wide or far-inset border is narrowed first (to
// 1 mm), then moved out (to just inside the edge rounding).
margin_cap = 0.6 * base_r;
bw = !has_border ? border_width : max(1, min(border_width, margin_cap - inset_req - 1));
inset = !has_border ? inset_req : max(er + 0.3, min(inset_req, margin_cap - bw - 1));
margin = has_border ? inset + bw + 1 : er + 1;
function room() = base_r - margin;
function box_y() = shape == "heart" ? heart_k * 0.1 : 0;

// Everything inside the border (or the flat top), less a margin.
module inner_2d() {
    offset(r = -margin) shape_2d();
}

module border_2d() {
    difference() {
        offset(r = -inset) shape_2d();
        offset(r = -(inset + bw)) shape_2d();
    }
}

// ---------------------------------------------------------------- letters

// Ink width and height at size 1, from DejaVu Sans Bold (textmetrics); other
// faces are close enough, and the letter is clipped to the tile regardless.
function has(set, ch) = search(ch, set) != [];
function wide_of(ch) = ch == "W" ? 1.45 : has("MmwQ@&%", ch) ? 1.22 : 1.07;
function tall_of(ch) = has("JQjgpqy@|", ch) ? 1.36 : 1.06;
function underlined(ch) = underline_6_9 && (ch == "6" || ch == "9");

// Letter size: letter_scale of the room's height, shrunk so the glyph's box
// fits the square, or its corners fit the circle (with 5 % to spare: text()
// centres on the advance, not the ink, so a glyph like K sits a little off).
function glyph_size(ch) = let(
    w = wide_of(ch), h = tall_of(ch) + (underlined(ch) ? 0.24 : 0),
    s = 2 * room() * letter_scale / 100 / h)
    shape == "rounded_square" ? min(s, 2 * room() / w, 2 * room() / h)
                              : min(s, 2 * room() / (1.05 * sqrt(w * w + h * h)));

module glyph_2d(ch) {
    bar = underlined(ch);
    s2 = glyph_size(ch);
    intersection() {
        inner_2d();
        translate([0, box_y()]) {
            translate([0, bar ? s2 * 0.12 : 0])
                text(ch, size = s2, font = font, halign = "center", valign = "center");
            if (bar) translate([0, -s2 * 0.53 - s2 * 0.06]) square([s2 * 0.62, s2 * 0.12], center = true);
        }
    }
}

// ---------------------------------------------------------------- tile

// Extrude the outline with a set-in bottom edge and a stepped round top edge.
module rounded_slab(h) {
    linear_extrude(foot) offset(delta = -foot) shape_2d();
    top = h - er;
    translate([0, 0, foot]) linear_extrude(top - foot) shape_2d();
    if (er > 0)
        for (i = [1:round_steps]) {
            z0 = top + er * (i - 1) / round_steps;
            in = er - sqrt(er * er - pow(er * i / round_steps, 2));
            translate([0, 0, z0]) linear_extrude(er / round_steps) offset(r = -in) shape_2d();
        }
}

module magnet_cut() {
    if (has_mag) {
        if (mount == "glue_in")
            translate([0, 0, -1]) cylinder(d = pocket_d, h = pocket_h + 1);
        else translate([0, 0, magnet_skin]) {
            cylinder(d = pocket_d, h = pocket_h);
            // Slot out through the top edge, narrowed at the mouth.
            difference() {
                translate([-pocket_d / 2, 0, 0]) cube([pocket_d, S, pocket_h]);
                for (s = [-1, 1])
                    translate([s * (pocket_d / 2 - detent / 2) - detent / 2, slot_mouth() - detent_len, -1])
                        cube([detent, S, pocket_h + 2]);
            }
        }
    }
}

// y where the slot leaves the tile: the shape's top edge on the centre line.
function slot_mouth() = shape == "heart" ? heart_k / sqrt(2) + heart_dy
                      : shape == "hexagon" ? S / 2 * sqrt(3) / 2
                      : shape == "scalloped" ? scallop_r0 : S / 2;

module tile_body(ch) {
    difference() {
        rounded_slab(T);
        magnet_cut();
        if (letter_style == "inlay") {
            translate([0, 0, T - inlay_d]) linear_extrude(inlay_d + 1) {
                if (ch != "") glyph_2d(ch);
                if (has_border) border_2d();
            }
        }
    }
}

module tile_letter(ch) {
    if (ch != "")
        translate([0, 0, letter_style == "inlay" ? T - inlay_d : T])
            linear_extrude(letter_depth) glyph_2d(ch);
}

module tile_border() {
    if (has_border)
        translate([0, 0, letter_style == "inlay" ? T - inlay_d : T])
            linear_extrude(letter_depth) border_2d();
}

// ---------------------------------------------------------------- layout

function tile_pos(j) = [(j % cols) * (S + G) + S / 2, -floor(j / cols) * (S + G) - S / 2];

if (T > thickness)
    echo(str("NOTE: thickness raised from ", thickness, " to ", T, " mm",
             has_mag ? str(" to fit the ", magnet, " mm magnet") : " for the edge rounding and inlay"));
if (G < gap)
    echo(str("NOTE: gap reduced from ", gap, " to ", G, " mm to fit ", n, " tiles on the plate"));
if (has_border && (bw < border_width || abs(inset - border_inset) > 1e-6))
    echo(str("NOTE: border set to ", bw, " mm wide, ", round(inset * 100) / 100,
             " mm from the edge, to leave room for the letter"));
assert(room() >= 0.35 * base_r, str("no room for a letter: ", room(), " mm"));
echo(str("SB_TILES n=", n, " cols=", cols, " rows=", rows, " gap=", G, " thickness=", T, " inset=", inset,
         " border_width=", bw, " margin=", margin, " room=", room(), " heart_k=", heart_k));
assert(rows * (S + G) - G <= bed_d, "too many tiles for the bed at this size");

// With no characters, one blank tile, so there is always something to print.
module magnet_at_seat(d) {
    translate([0, 0, (mount == "glue_in" ? 0 : magnet_skin) + magnet_clearance / 2]) cylinder(d = d, h = mag_h);
}

if (probe_magnet != "none") {
    for (j = [0:max(n, 1) - 1]) translate(tile_pos(j)) intersection() {
        tile_body(n == 0 ? "" : chars[j]);
        if (probe_magnet == "seat") magnet_at_seat(mag_d);
        else hull() {
            d = probe_magnet == "sweep" ? mag_d : mag_d - 0.3;
            magnet_at_seat(d);
            translate([0, S, 0]) magnet_at_seat(d);
        }
    }
    translate([-1000, 0, 0]) cube(1);
} else
for (j = [0:max(n, 1) - 1]) translate(tile_pos(j)) {
    ch = n == 0 ? "" : chars[j];
    color(tile_color) tile_body(ch);
    color(letter_color) tile_letter(ch);
    color(border_color) tile_border();
}
