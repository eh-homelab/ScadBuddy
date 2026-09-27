// Whistle — a pea-less referee-style whistle that prints lying on its side.
//
// It works like a Helmholtz resonator driven by an edge tone: breath goes down
// a flat windway about 1.2 mm high, crosses an open window, and splits on a
// sharp labium edge. Half the jet spills out of the window and half goes into
// the round chamber, and the chamber's air springs it back — that oscillation
// is the note.
//
// Printed on its side, the whole whistle is one 2D profile extruded straight
// up, closed by a floor and a roof:
//   - the windway's 1.2 mm height lies in the XY plane, where the printer is
//     most accurate, and its walls are vertical, so no overhang can sag into
//     it and close it;
//   - the labium edge is a vertical edge drawn by the nozzle, not a layer step;
//   - the only unsupported spans are flat bridges: the roof over the chamber,
//     the windway and the window.
//
// Written to the MakerWorld Parametric Model Maker customizer conventions so
// the same file works unchanged on MakerWorld and in ScadBuddy.
//
// The colour parameters are the extruder order: whistle_color is extruder 1,
// text_color extruder 2.

/* [Whistle] */

// Body shape
style = "classic_referee"; // [classic_referee:Classic referee, round_chamber:Round pebble, keychain_mini:Keychain mini]

// Scale factor (1 = a 43 mm long classic whistle); the windway never goes below 1 mm high
size = 1; // [0.8:0.1:1.5]

// Lanyard loop at the back of the chamber
loop = true;

/* [Decor] */

// Name inlaid in the upper side, over the chamber (leave empty for none; it shrinks to fit, so about 6 letters stay legible, fewer on the mini)
name = ""; // 12

// Typeface (the app fills this dropdown from the fonts installed in the image)
font = "DejaVu Sans:style=Bold"; // font

/* [Colors] */

// Whistle colour (extruder 1)
whistle_color = "#FF7043"; // color

// Name colour (extruder 2)
text_color = "#FFFFFF"; // color

/* [Hidden] */

$fn = 96;

// Per style at size 1: chamber radius, internal width (the windway's and the
// window's width, across the layers), mouthpiece reach from the chamber
// centre, windway height, wall thickness.
//                         Rc    wi   Lm   hw   t
spec = style == "round_chamber" ? [11,  9,   24, 1.2, 2.0]
     : style == "keychain_mini" ? [6.5, 6,   16, 1.0, 1.6]
     :                            [10,  9,   27, 1.2, 2.0];

s = size;
Rc = spec[0] * s;                          // chamber radius (inside)
wi = spec[1] * s;                          // internal width
Lm = spec[2] * s;                          // mouth end, from the chamber centre
hw = max(1, spec[3] * s);                  // windway height: never below 1 mm
t = max(1.6, spec[4] * s);                 // outer walls in the profile
ws = max(1.6, 1.6 * s);                    // floor and roof (the flat sides)
fl = max(1.2, 1.2 * s);                    // wall between windway and chamber
tm = max(2, 2.5 * s);                      // material under the windway
Wz = wi + 2 * ws;                          // overall thickness, as printed

// Window (windway exit to labium edge) about 3.3 windway heights long, and a
// 25-degree bevel on the labium's outer face.
Lw = max(3, 3.3 * hw);
bevel = 25;

y_f = Rc + fl;                             // windway floor
y_e = y_f + hw / 2;                        // labium edge, on the jet's centre line
top = y_f + hw + t;                        // top of the mouthpiece
x_l = Lw * 0.3;                            // labium edge
x_w = x_l - Lw;                            // windway exit
ramp = (top - y_e) / tan(bevel);           // run of the labium bevel
k = 1.5 * hw;                              // flat underside of the labium
x_r = x_l + ramp + 2;                      // back end of the mouthpiece block

// Lanyard loop, on the back of the chamber, a little below centre.
loop_r = max(3, 3.5 * s);
loop_hole = max(3, 3.2 * s);
loop_a = -25;
loop_c = (Rc + t + loop_r * 0.55) * [cos(loop_a), sin(loop_a)];
loop_h = min(Wz, max(3, 4 * s));

inlay = 0.6;

// ---------------------------------------------------------------- profile

module mouthpiece_2d() {
    translate([-Lm, y_f - tm]) square([x_r + Lm, top - (y_f - tm)]);
}

// Corners rounded to 1 mm, outside and in.
module body_2d() {
    offset(r = -1) offset(r = 2) offset(r = -1)
        if (style == "round_chamber") hull() { circle(r = Rc + t); mouthpiece_2d(); }
        else union() { circle(r = Rc + t); mouthpiece_2d(); }
}

module loop_2d() {
    difference() {
        hull() {
            translate(loop_c) circle(r = loop_r);
            circle(r = Rc + t - 1);
        }
        translate(loop_c) circle(d = loop_hole);
    }
}

// Window: open to the outside above the jet, from the windway exit to the
// labium edge and up the labium's bevel.
module window_2d() {
    polygon([[x_w, y_f], [x_l, y_f], [x_l, y_e],
             [x_l + (top + 1 - y_e) / tan(bevel), top + 1], [x_w, top + 1]]);
}

// Everything hollow between the floor and the roof.
module cavity_2d() {
    circle(r = Rc);
    // Windway, through the mouth end.
    translate([-Lm - 1, y_f]) square([x_w + Lm + 1, hw]);
    // Throat under the jet, joining the window to the chamber, with the
    // labium's flat underside at the edge height.
    translate([x_w, Rc - 3]) square([x_l + k - x_w, y_e - (Rc - 3)]);
    window_2d();
}

// ---------------------------------------------------------------- name

// Shrink-only fit: resize() to the box of the text plus a hair-thin bar of the
// target length, so text already smaller is left alone.
module fit_x(w) {
    projection(cut = true) translate([0, 0, -0.5])
        resize([w, 0, 0], auto = [false, true, false])
            union() {
                linear_extrude(1) children();
                translate([-w / 2, 0, 10]) cube([w, 0.01, 0.01]);
            }
}

// The name shrinks to the chord of the chamber's flat side (1.5 mm in from
// its edge) across the letters' height.
name_size = 0.5 * Rc;
name_w = 2 * sqrt(pow(Rc + t - 1.5, 2) - pow(name_size / 2, 2));
module name_2d() {
    if (len(name) > 0)
        intersection() {
            fit_x(name_w)
                text(name, size = name_size, font = font, halign = "center", valign = "center");
            offset(delta = -1.5) difference() { body_2d(); window_2d(); }
        }
}

// ---------------------------------------------------------------- whistle

color(whistle_color) difference() {
    union() {
        linear_extrude(Wz) body_2d();
        if (loop) linear_extrude(loop_h) loop_2d();
    }
    translate([0, 0, ws]) linear_extrude(wi) cavity_2d();
    if (len(name) > 0) translate([0, 0, Wz - inlay]) linear_extrude(inlay + 1) name_2d();
}

if (len(name) > 0)
    color(text_color) translate([0, 0, Wz - inlay]) linear_extrude(inlay) name_2d();
