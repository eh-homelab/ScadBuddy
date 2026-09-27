// Clip-on cable label — a C-clip that snaps sideways onto a cable, carrying a
// text label.
//
// Print orientation is the point of this design. The cable axis is vertical
// (Z) in every style, so the C-clip's cross-section lies in the build plane
// and each layer is a complete C. Opening the clip to push a cable in bends
// the C within its layers, never across them, so the clip does not split
// along a layer line the way a clip printed lying down does.
//
//   flag          a flat tag lying on the bed, the clip standing up at one
//                 end. Text is inlaid flush into the top face (the last few
//                 layers). Read it looking along the cable.
//   double_sided  the same tag with the text inlaid flush into the bottom
//                 face as well, mirrored so it reads the right way round from
//                 underneath. The bottom text is the first layers on the bed,
//                 so neither side needs supports.
//   wrap_band     the clip is stretched along the cable into a band with a
//                 flat face, text inlaid into that face running along the
//                 cable, like a wrap-around label. The text is on a vertical
//                 wall, so it prints supportless but changes colour on every
//                 layer of the band's height (more purge than the flat tags).
//
// The two colour parameters are the extruder order: body_color is extruder 1,
// text_color is extruder 2.

/* [Cable] */

// Cable outer diameter in mm
cable_d = 5; // [2:0.5:15]

// Width of the clip's opening as a percentage of the cable diameter. Lower grips harder and snaps on less easily
clip_opening_pct = 70; // [55:5:85]

// Length of the clip along the cable (its print height). Not used by wrap_band, where the band is the label length
clip_len = 10; // [6:1:20]

/* [Label] */

// Label text
text = "HDMI"; // 16

// Typeface (the app fills this dropdown from the fonts installed in the image)
font = "DejaVu Sans Mono:style=Bold"; // font

// Letter height in mm
text_size = 6; // [3:0.5:12]

// Length of the label: tag length beyond the clip, or band length along the cable for wrap_band
flag_len = 35; // [15:1:80]

// Height of the label face (the tag's width across, or the band face's width)
flag_h = 10; // [6:1:20]

// Label style
style = "flag"; // [flag:Flag tag, double_sided:Flag tag (text both sides), wrap_band:Wrap band along the cable]

/* [Colors] */

// Clip and label body (extruder 1)
body_color = "#FFFFFF"; // color

// Text (extruder 2)
text_color = "#000000"; // color

/* [Hidden] */

$fn = 64;

// Diametral clearance of the clip's bore over the cable.
clearance = 0.2;

// Clip wall: thicker for fatter cables, never under 4 perimeters of 0.4.
wall = max(1.6, 0.15 * cable_d + 1.0);

// Thickness of the flat tag.
tag_t = 2.4;

// Depth of the inlaid text: 3 layers at 0.2 mm.
inlay = 0.6;

// Clear margin between the text and the edge of its face.
margin = 1;

// Extra material between the clip's outer wall and the band's flat face.
face_standoff = 1;

eps = 0.01;

r_in = (cable_d + clearance) / 2;
r_out = r_in + wall;
opening = cable_d * clip_opening_pct / 100;
lead = wall * 0.6;          // lead-in chamfer on each jaw
face_x = r_out + face_standoff;
has_text = len(text) > 0;

// Cable slot through the -X side of the ring, widening outwards into a
// lead-in so the cable finds the opening.
module slot_2d() {
    polygon([[0, -opening / 2], [-r_in, -opening / 2],
             [-r_out - 1, -opening / 2 - lead], [-r_out - 1, opening / 2 + lead],
             [-r_in, opening / 2], [0, opening / 2]]);
}

module clip_2d() {
    difference() {
        circle(r = r_out);
        circle(r = r_in);
        slot_2d();
    }
}

// ---- flag / double_sided ------------------------------------------------

tag_end = r_out + flag_len;
corner = min(2, flag_h / 4);

module tag_2d() {
    difference() {
        hull() {
            translate([0, -flag_h / 2]) square([eps, flag_h]);
            translate([tag_end - corner, flag_h / 2 - corner]) circle(r = corner);
            translate([tag_end - corner, -flag_h / 2 + corner]) circle(r = corner);
        }
        circle(r = r_in);
    }
}

// Area of the tag the text may occupy: clear of the clip and the edges.
module tag_text_area_2d() {
    translate([r_out + margin, -flag_h / 2 + margin])
        square([flag_len - 2 * margin, flag_h - 2 * margin]);
}

module tag_text_2d() {
    intersection() {
        translate([r_out + flag_len / 2, 0])
            text(text, size = text_size, font = font,
                 halign = "center", valign = "center");
        tag_text_area_2d();
    }
}

module tag_top_text_3d(extra = 0) {
    translate([0, 0, tag_t - inlay]) linear_extrude(height = inlay + extra) tag_text_2d();
}

// Mirrored across the tag's long axis, so it reads correctly when the tag is
// turned over about that axis.
module tag_bottom_text_3d(extra = 0) {
    translate([0, 0, -extra]) linear_extrude(height = inlay + extra)
        mirror([0, 1]) tag_text_2d();
}

module tag_label() {
    color(body_color) difference() {
        union() {
            linear_extrude(height = clip_len) clip_2d();
            linear_extrude(height = tag_t) tag_2d();
        }
        if (has_text) {
            tag_top_text_3d(eps);
            if (style == "double_sided") tag_bottom_text_3d(eps);
        }
    }
    if (has_text) color(text_color) {
        tag_top_text_3d();
        if (style == "double_sided") tag_bottom_text_3d();
    }
}

// ---- wrap_band ------------------------------------------------------------

band_len = flag_len;

module band_2d() {
    difference() {
        hull() {
            circle(r = r_out);
            translate([face_x - eps, -flag_h / 2]) square([eps, flag_h]);
        }
        circle(r = r_in);
        slot_2d();
    }
}

// Text in the face's own frame: a = world Y, b = world Z. Rotated a quarter
// turn so it runs up the band, along the cable.
module band_text_2d() {
    intersection() {
        translate([0, band_len / 2]) rotate(90)
            text(text, size = text_size, font = font,
                 halign = "center", valign = "center");
        translate([-flag_h / 2 + margin, 1.5]) square([flag_h - 2 * margin, band_len - 3]);
    }
}

// Maps the 2D frame onto the face: x -> Y, y -> Z, extrusion -> X.
module band_text_3d(extra = 0) {
    translate([face_x - inlay, 0, 0]) rotate([90, 0, 90])
        linear_extrude(height = inlay + extra) band_text_2d();
}

module band_label() {
    color(body_color) difference() {
        linear_extrude(height = band_len) band_2d();
        if (has_text) band_text_3d(eps);
    }
    if (has_text) color(text_color) band_text_3d();
}

if (style == "wrap_band") band_label();
else tag_label();
