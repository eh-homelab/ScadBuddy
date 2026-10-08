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

// The enclosed holes of the word: the inside of an O, or a gap two joined script
// letters close off.
module counters_2d() {
    difference() {
        fill() name_2d();
        name_2d();
    }
}

// Every glyph drawn down (or up) to the line's vertical centre, inside its own
// width: a run of glyphs that sits wholly above or below the centre band, such
// as a trailing "...", "_" or a quote, then still meets it (#1450).
module drop_to_centre_2d() {
    intersection() {
        minkowski() {
            glyphs_2d();
            translate([0, -2 * text_size]) square([0.01, 2 * text_size]);
        }
        translate([-bed_x, 0]) square([2 * bed_x, 2 * text_size]);
    }
    intersection() {
        minkowski() {
            glyphs_2d();
            square([0.01, 2 * text_size]);
        }
        translate([-bed_x, -2 * text_size]) square([2 * bed_x, 2 * text_size]);
    }
}

// What holds the base together (#920): a band `outline` tall along the line's
// vertical centre, from the text origin to the last glyph, and every glyph drawn
// to it. Without it, a space, a gap left by a glyph the font lacks, two script
// runs that do not meet, or a run off the centre line splits the base into
// islands, and every piece but the first falls off the keyring. The word's
// counters are cut back out, so a counter wider than `2 * outline` stays
// see-through, as it would without the spine (#1450).
module spine_2d() {
    difference() {
        union() {
            intersection() {
                square([2 * bed_x, outline], center = true);
                // From the text origin, where the keyring tab's neck ends, not the
                // first glyph: leading spaces or a leading missing glyph would
                // otherwise leave the tab on its own island.
                hull() {
                    drop_to_centre_2d();
                    translate([0, -outline / 2]) square([0.01, outline]);
                }
                // Only where there is a glyph (#1453): the glyphs drawn to the centre
                // and their mirror image span the origin, but a name that renders
                // nothing (empty, spaces only, or only glyphs the font lacks) leaves
                // this empty, so the origin's square does not grow into a stray lump
                // of base.
                hull() {
                    drop_to_centre_2d();
                    mirror([1, 0]) drop_to_centre_2d();
                }
            }
            drop_to_centre_2d();
        }
        counters_2d();
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
