// Name keychain — a word in a bold script face, raised on a base plate cut to
// the outline of the word, with a keyring hole on the left.
//
// Written to the MakerWorld Parametric Model Maker customizer conventions so
// the same file works unchanged on MakerWorld and in ScadBuddy.
//
// The two colour parameters are the extruder order: base_color is extruder 1,
// text_color is extruder 2.

/* [Text] */

// Word to put on the keychain
name = "Reagan"; // 20

// Typeface
font = "Lobster Two:style=Bold"; // font

/* [Size] */

// Letter height in mm, from the top of the capitals to the bottom of the descenders (a name too long for the 300 mm bed is shrunk to fit)
text_size = 20; // [8:0.5:40]

// How far the letters stand proud of the base, in mm
letter_height = 2.8; // [1:0.1:5]

// Thickness of the base plate under the letters, in mm
base_thickness = 4; // [2:0.5:8]

// Width of the border the base adds around the letters, in mm
outline = 3.5; // [1:0.5:6]

/* [Keyring] */

// Add a keyring hole at the left-hand end
hole = true;

// Keyring hole diameter in mm
hole_diameter = 4; // [2:0.5:8]

// Material left around the keyring hole, in mm
ring_wall = 1.6; // [1:0.1:4]

/* [Colours] */

// Base and border colour (extruder 1)
base_color = "#0047BB"; // color

// Letter colour (extruder 2)
text_color = "#FF1493"; // color

/* [Hidden] */

$fn = 64;

// Script faces do not always let every glyph pair touch. Welding closes gaps
// up to 2 * weld between glyphs (a dilate followed by an erode), so the word
// comes out as one connected piece without changing its overall size.
weld = 0.8;

// Tracking. Script faces are drawn to join, but not every pair touches at the
// nominal advance; a touch of negative tracking closes the join.
spacing = 0.95;

// Radius of the solid tab the keyring hole is punched through.
function ring_radius() = hole_diameter / 2 + ring_wall;

// Centre of the keyring hole. Sits clear of the word, to the left of the text
// origin, far enough out that the tab still overlaps the base's border.
function ring_centre() = [-(outline + ring_radius() * 0.6), 0];

// Longest word that still fits the H2C's 300 mm two-nozzle width, with the
// border on both ends, the keyring tab and a little margin.
bed_x = 300;
max_word = bed_x - 4 - 2 * outline - (hole ? 1.6 * ring_radius() : 0);

// Shrink-only fit: resize() the children together with their mirror image
// and a hair-thin bar from x = -w to w, all on separate layers, then cut the
// children's layer back out. The bounding box is symmetric about x = 0, so a
// word that ends before x = w is left alone (a glyph reaching a little left
// of the origin does not count) and a longer one is scaled down uniformly
// about the origin, keeping it centred on the keyring tab, until it ends at w.
module fit_x(w) {
    projection(cut = true) translate([0, 0, -0.5])
        resize([2 * w, 0, 0], auto = [false, true, false])
            union() {
                linear_extrude(1) children();
                translate([0, 0, 5]) linear_extrude(1) mirror([1, 0]) children();
                translate([-w, 0, 10]) cube([2 * w, 0.01, 0.01]);
            }
}

// The word itself. halign/valign put the text origin at the left-hand end, on
// the vertical centre of the line, which is what the keyring tab is placed
// against — no text measurement needed, so this stays portable.
module glyphs_2d() {
    fit_x(max_word)
        text(name, size = text_size, font = font, spacing = spacing,
             halign = "left", valign = "center");
}

module name_2d() {
    if (weld > 0) offset(r = -weld) offset(r = weld) glyphs_2d();
    else glyphs_2d();
}

// Tab carrying the keyring hole. Its neck is as tall as the ring and runs into the
// first letter, so the ring hangs off solid base rather than a pinch where a script
// capital's outline only grazes the text origin.
module ring_tab_2d() {
    hull() {
        translate(ring_centre()) circle(r = ring_radius());
        translate([outline / 2, 0]) circle(r = ring_radius());
    }
}

// A strip along the line's vertical centre from the text origin to the last glyph
// (the word's hull, cut to a band `outline` tall). Without it, a space, a gap
// left by a glyph the font lacks, or two script runs that do not meet splits
// the base into islands, and every piece but the first falls off the keyring
// (#920).
module spine_2d() {
    intersection() {
        // From the text origin, where the keyring tab's neck ends, not the
        // first glyph: leading spaces or a leading missing glyph would
        // otherwise leave the tab on its own island.
        hull() {
            glyphs_2d();
            // Not for an empty name: there is no word to join, and the square alone
            // would leave a stray lump of base.
            if (name != "") translate([0, -outline / 2]) square([0.01, outline]);
        }
        square([2 * bed_x, outline], center = true);
    }
}

// Base plate: the word's footprint and the spine grown outwards by `outline`,
// plus the keyring tab, less the hole.
module base_2d() {
    difference() {
        union() {
            offset(r = outline) union() {
                name_2d();
                spine_2d();
            }
            if (hole) ring_tab_2d();
        }
        if (hole) translate(ring_centre()) circle(d = hole_diameter);
    }
}

color(base_color)
    linear_extrude(height = base_thickness)
        base_2d();

color(text_color)
    translate([0, 0, base_thickness])
        linear_extrude(height = letter_height)
            name_2d();
