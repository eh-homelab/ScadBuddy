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

// Typeface (the app fills this dropdown from the fonts installed in the image)
font = "Lobster Two:style=Bold"; // font

/* [Size] */

// Letter height in mm, from the top of the capitals to the bottom of the descenders
text_size = 20; // [8:0.5:40]

// How far the letters stand proud of the base
letter_height = 2.8; // [1:0.1:5]

// Thickness of the base plate under the letters
base_thickness = 4; // [2:0.5:8]

// Width of the border the base adds around the letters
outline = 3.5; // [1:0.5:6]

/* [Keyring] */

// Add a keyring hole at the left-hand end
hole = true;

// Keyring hole diameter
hole_diameter = 4; // [2:0.5:8]

// Material left around the keyring hole
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

// The word itself. halign/valign put the text origin at the left-hand end, on
// the vertical centre of the line, which is what the keyring tab is placed
// against — no text measurement needed, so this stays portable.
module glyphs_2d() {
    text(name, size = text_size, font = font, spacing = spacing,
         halign = "left", valign = "center");
}

module name_2d() {
    if (weld > 0) offset(r = -weld) offset(r = weld) glyphs_2d();
    else glyphs_2d();
}

// Teardrop tab carrying the keyring hole, necked into the base's border.
module ring_tab_2d() {
    hull() {
        translate(ring_centre()) circle(r = ring_radius());
        circle(r = outline * 0.6);
    }
}

// Base plate: the word's footprint grown outwards by `outline`, plus the
// keyring tab, less the hole.
module base_2d() {
    difference() {
        union() {
            offset(r = outline) name_2d();
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
