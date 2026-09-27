// Plant label — a plant name on a flat label, as a garden stake, an
// arrow-shaped stake, a clip that hooks over a pot rim, or a hanging tag.
//
// Every style is one flat outline extruded to `thickness` and printed face
// up, so nothing needs supports. The text is raised on the face or inlaid
// flush with it, and always shrinks to fit the label.
//
// Written to the MakerWorld Parametric Model Maker customizer conventions so
// the same file works unchanged on MakerWorld and in ScadBuddy.
//
// The two colour parameters are the extruder order: label_color is extruder 1,
// text_color is extruder 2.

/* [Text] */

// Plant name
text = "Basil"; // 20

// Typeface (the app fills this dropdown from the fonts installed in the image)
font = "Lobster Two:style=Bold"; // font

// Letter height in mm; shrinks automatically if the name does not fit the label
text_size = 12; // [6:1:40]

// How the letters are made
text_style = "raised"; // [raised:Raised, inlay:Flush inlay]

/* [Shape] */

// Label style
style = "stake"; // [stake:Stake, arrow_stake:Arrow stake, pot_rim_clip:Pot rim clip, hanging_tag:Hanging tag]

// Width of the label area in mm
label_w = 70; // [30:5:150]

// Height of the label area in mm
label_h = 22; // [12:1:50]

// Stake length below the label, to the tip, in mm (stake styles)
stake_len = 80; // [30:5:200]

// Label thickness in mm
thickness = 2.5; // [1.6:0.2:5]

// Thickness of the pot rim the clip fits over, in mm (pot rim clip)
rim_thickness = 3; // [1:0.5:8]

/* [Colors] */

// Label colour (extruder 1)
label_color = "#6BA368"; // color

// Text colour (extruder 2)
text_color = "#FFFFFF"; // color

/* [Hidden] */

$fn = 48;

// Height of raised letters; depth of inlay pockets (capped for thin labels).
relief = 1.0;
// Clear space between the label edge and the text.
text_pad = 2.5;
// Stake width and the length of its pointed tip.
stake_w = 8;
tip_len = 12;
// Pot rim clip: leg width, leg length, and the grip bump on each leg tip.
leg_w = 4;
clip_len = 18;
clip_grip = 0.3;
// Hanging tag hole and the material around it.
hole_d = 4;
hole_wall = 2.5;

W = label_w;
H = label_h;
T = thickness;
has_text = len(text) > 0;

rel = text_style == "raised" ? relief : min(relief, T * 0.4);
corner_r = min(3, H / 4);
sw = min(stake_w, W - 2 * corner_r);

// The arrow's fletching notch reaches H / 4 into the left end, so its text
// box starts past the notch and is shifted right by half that.
notch = style == "arrow_stake" ? H / 4 : 0;
text_w = max(1, W - 2 * text_pad - notch);
text_x = notch / 2;
text_h = max(1, H - 2 * text_pad);

// ---------------------------------------------------------------- outline

module body_2d() {
    if (style == "arrow_stake")
        // Arrow pointing right, with a fletching notch on the left.
        polygon([[-W / 2, H / 2], [W / 2, H / 2], [W / 2 + H / 2, 0],
                 [W / 2, -H / 2], [-W / 2, -H / 2], [-W / 2 + H / 4, 0]]);
    else
        offset(r = corner_r) square([W - 2 * corner_r, H - 2 * corner_r], center = true);
}

module stake_2d() {
    y0 = -H / 2 - stake_len;
    polygon([[-sw / 2, -H / 2 + 1], [sw / 2, -H / 2 + 1],
             [sw / 2, y0 + tip_len], [0, y0], [-sw / 2, y0 + tip_len]]);
}

// Two legs hanging from the label's bottom edge; the pot rim slides up
// between them. A small bump at each leg tip grips the rim.
leg_x = rim_thickness / 2 + leg_w / 2;
module clip_2d() {
    for (s = [-1, 1]) {
        translate([s * leg_x - leg_w / 2, -H / 2 - clip_len])
            square([leg_w, clip_len + 1]);
        translate([s * (rim_thickness / 2), -H / 2 - clip_len + 1.5])
            scale([clip_grip * 2, 3]) circle(d = 1, $fn = 24);
    }
}

hole_r = hole_d / 2 + hole_wall;
hole_cx = -W / 2 - hole_d / 2 - 1;

module outline_2d() {
    difference() {
        union() {
            // Fillet the concave corners where the stake or tab meets the label.
            offset(r = -1.5) offset(r = 1.5) union() {
                body_2d();
                if (style == "stake" || style == "arrow_stake") stake_2d();
                if (style == "hanging_tag")
                    hull() {
                        translate([hole_cx, 0]) circle(r = hole_r);
                        translate([-W / 2 + corner_r, -hole_r]) square([1, 2 * hole_r]);
                    }
            }
            // Not filleted: that would narrow the gap the rim slides into.
            if (style == "pot_rim_clip") clip_2d();
        }
        if (style == "hanging_tag") translate([hole_cx, 0]) circle(d = hole_d);
    }
}

// ---------------------------------------------------------------- text

// Shrink-only fit. resize() scales to the bounding box of its children; a
// hair-thin bar of the target length, parked at z=10, makes that box at least
// the target size, so text already smaller is left alone and larger text is
// scaled down uniformly. z is left unscaled, so a cut at z=0.5 returns just
// the text.
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

module text_2d() {
    if (has_text)
        intersection() {
            translate([text_x, 0]) fit_y(text_h) fit_x(text_w)
                text(text, size = text_size, font = font,
                     halign = "center", valign = "center");
            offset(delta = -1) body_2d();
        }
}

// ---------------------------------------------------------------- label

if (text_style == "raised") {
    color(label_color) linear_extrude(T) outline_2d();
    color(text_color) translate([0, 0, T]) linear_extrude(rel) text_2d();
} else {
    color(label_color) difference() {
        linear_extrude(T) outline_2d();
        translate([0, 0, T - rel]) linear_extrude(rel + 1) text_2d();
    }
    color(text_color) translate([0, 0, T - rel]) linear_extrude(rel) text_2d();
}
