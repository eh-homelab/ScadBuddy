// Filament swatch card — a flat card for cataloguing spools: brand, material,
// colour name and print temperature in debossed or inlaid text, a row of
// stepped windows from 0.2 mm upwards in 0.2 mm increments (each labelled
// with its thickness) to show how the filament looks at different
// thicknesses, and an optional hole for a swatch ring.
//
// Written to the MakerWorld Parametric Model Maker customizer conventions so
// the same file works unchanged on MakerWorld and in ScadBuddy.
//
// Colour order is the extruder order: swatch_color is extruder 1 (the card),
// text_color is extruder 2 (the lettering, inlay mode only). Debossed mode
// prints in swatch_color alone.

/* [Info] */

// Brand
brand = "Bambu"; // 16

// Material / product line
material = "PLA Basic"; // 16

// Colour name
color_name = "Cyan"; // 20

// Print temperature
temp = "220°C"; // 10

// Typeface (the app fills this dropdown from the fonts installed in the image)
font = "DejaVu Sans:style=Bold"; // font

/* [Card] */

// Card length along its long side in landscape, mm
width = 80; // [60:5:100]

// Card length along its short side in landscape, mm
height = 40; // [30:5:60]

// Card thickness, mm
thickness = 3.2; // [2:0.2:4]

// Number of stepped windows: 0.2 mm, 0.4 mm, ... in 0.2 mm increments
step_count = 5; // [3:1:8]

// Hole for a swatch ring
hole = true;

// Debossed prints in one filament; inlaid fills the lettering with the text colour, flush
text_mode = "deboss"; // [deboss:Debossed (one colour), inlay:Inlaid (two colours)]

// Landscape puts the windows in a row along the bottom; portrait stands the card up with the windows stacked
orientation = "landscape"; // [landscape:Landscape, portrait:Portrait]

/* [Colors] */

// Card (extruder 1)
swatch_color = "#0086D6"; // color

// Lettering, inlay mode only (extruder 2)
text_color = "#FFFFFF"; // color

/* [Hidden] */

$fn = 48;

T = thickness;
portrait = orientation == "portrait";
CW = portrait ? height : width;     // card width on the plate (x)
CH = portrait ? width : height;     // card height on the plate (y)

margin = 3;
corner_r = 3;
hole_d = 5;
text_depth = 0.6;
step = 0.2;
n = step_count;

// ---------------------------------------------------------------------------
// Text width estimate for fitting. textmetrics() is still experimental and
// disabled on MakerWorld, so this uses per-glyph advance widths measured at
// size 10 with textmetrics() offline (ASCII 32..126; degree sign separately).

ADV_DEJAVU_BOLD = [4.84, 6.33, 7.24, 11.64, 9.66, 13.92, 12.11, 4.25, 6.35, 6.35, 7.26, 11.64, 5.28, 5.76, 5.28, 5.07, 9.66, 9.66, 9.66, 9.66, 9.66, 9.66, 9.66, 9.66, 9.66, 9.66, 5.55, 5.55, 11.64, 11.64, 11.64, 8.06, 13.89, 10.75, 10.59, 10.19, 11.53, 9.49, 9.49, 11.4, 11.62, 5.17, 5.17, 10.76, 8.85, 13.82, 11.62, 11.81, 10.18, 11.81, 10.69, 10, 9.47, 11.28, 10.75, 15.32, 10.71, 10.06, 10.07, 6.35, 5.07, 6.35, 11.64, 6.94, 6.94, 9.37, 9.94, 8.23, 9.94, 9.42, 6.04, 9.94, 9.89, 4.76, 4.76, 9.24, 4.76, 14.47, 9.89, 9.54, 9.94, 9.94, 6.85, 8.27, 6.64, 9.89, 9.05, 12.83, 8.96, 9.05, 8.08, 9.89, 5.07, 9.89, 11.64];

function adv10(c) = let(o = ord(c))
    (o >= 32 && o <= 126) ? ADV_DEJAVU_BOLD[o - 32] : o == 176 ? 6.94 : ADV_DEJAVU_BOLD[78 - 32];
function sum_list(v, i = 0) = i >= len(v) ? 0 : v[i] + sum_list(v, i + 1);
function est_width(s, sz) = len(s) == 0 ? 0 : sz / 10 * sum_list([for (i = [0:len(s) - 1]) adv10(s[i])]);
function fit_size(s, want, w) =
    len(s) == 0 ? want : min(want, want * 0.95 * w / est_width(s, want));

function join2(a, b) = len(a) == 0 ? b : len(b) == 0 ? a : str(a, " ", b);

// Step label: "0.2" .. "1.6", always one decimal.
function step_label(k) = let(tenths = 2 * k) str(floor(tenths / 10), ".", tenths % 10);

// ---------------------------------------------------------------------------
// Layout. Every cell is [x, y, w, h]; cell k (0-based) has a floor
// (k+1) * 0.2 mm thick. Labels are [x, y, size, halign] per cell.

hole_c = portrait ? [CW / 2, CH - margin - hole_d / 2 - 0.5]
                  : [margin + hole_d / 2 + 0.5, CH - margin - hole_d / 2 - 0.5];

// Landscape: a row of cells along the bottom, labels above them, text
// left-aligned above the labels (beside the hole).
L_band = 0.34 * CH;
L_cw = (CW - 2 * margin) / n;
L_label = min(2.6, 0.75 * L_cw / 2.46);
L_label_y = margin + L_band + 1 + L_label / 2;
L_text_x = hole ? hole_c[0] + hole_d / 2 + 2 : margin + 1;
L_text_y0 = L_label_y + L_label / 2 + 1.8;
L_text_y1 = CH - margin;

// Portrait: cells stacked in the lower part with labels to their left, text
// centred above them, hole top centre.
P_band = 0.45 * CH;
P_label_w = 9;
P_ch = P_band / n;
P_label = min(2.6, 0.6 * P_ch, 0.9 * P_label_w / 2.46);
P_text_y0 = margin + P_band + 2;
P_text_y1 = hole ? hole_c[1] - hole_d / 2 - 1.5 : CH - margin;

cells = portrait
    ? [for (k = [0:n - 1]) [margin + P_label_w, margin + k * P_ch, CW - 2 * margin - P_label_w, P_ch]]
    : [for (k = [0:n - 1]) [margin + k * L_cw, margin, L_cw, L_band]];
labels = portrait
    ? [for (k = [0:n - 1]) [margin + P_label_w / 2, margin + (k + 0.5) * P_ch, P_label]]
    : [for (k = [0:n - 1]) [margin + (k + 0.5) * L_cw, L_label_y, L_label]];

// Text lines: [string, weight]. Landscape puts brand and material on one line.
lines = portrait
    ? [[color_name, 1.3], [brand, 1], [material, 1], [temp, 1]]
    : [[color_name, 1.3], [join2(brand, material), 1], [temp, 1]];
text_x = portrait ? CW / 2 : L_text_x;
text_w = portrait ? CW - 2 * margin - 1 : CW - margin - L_text_x;
text_y0 = portrait ? P_text_y0 : L_text_y0;
text_y1 = portrait ? P_text_y1 : L_text_y1;
line_gap = 0.35;   // gap between lines, in units of the line's size

// Size unit u so the weighted lines plus gaps fill the text area; a line of
// weight w is nominally w*u (u capped at 6 mm), then shrunk to fit the width.
weights = [for (l = lines) l[1]];
unit = min(6, (text_y1 - text_y0) / (sum_list(weights) + line_gap * (len(lines) - 1)));
// The other lines never come out larger than the colour name.
title_size = fit_size(lines[0][0], lines[0][1] * unit, text_w);
sizes = [for (i = [0:len(lines) - 1]) i == 0 ? title_size
         : min(fit_size(lines[i][0], lines[i][1] * unit, text_w),
               len(lines[0][0]) > 0 ? title_size / lines[0][1] : 100)];
// Baseline of each line, top to bottom, centring the glyphs in the line's box. DejaVu's glyphs run ~0.76 x size
// above the baseline and ~0.24 x size below it.
function line_bottom(i) = text_y1 - sum_list([for (j = [0:i]) weights[j] * unit])
                     - (i > 0 ? line_gap * unit * i : 0);
baselines = [for (i = [0:len(lines) - 1])
    line_bottom(i) + 0.5 * (weights[i] * unit - sizes[i]) + 0.24 * sizes[i]];

// ---------------------------------------------------------------------------

module card_2d() {
    difference() {
        offset(r = corner_r) offset(delta = -corner_r) square([CW, CH]);
        if (hole) translate(hole_c) circle(d = hole_d);
    }
}

module lettering_2d() {
    for (i = [0:len(lines) - 1]) if (len(lines[i][0]) > 0)
        translate([text_x, baselines[i]])
            text(lines[i][0], size = sizes[i], font = font,
                 halign = portrait ? "center" : "left", valign = "baseline");
    for (k = [0:n - 1])
        translate([labels[k][0], labels[k][1]])
            text(step_label(k + 1), size = labels[k][2], font = font,
                 halign = "center", valign = "center");
}

module card() {
    difference() {
        linear_extrude(height = T) card_2d();
        for (k = [0:n - 1]) {
            c = cells[k];
            translate([c[0], c[1], (k + 1) * step]) cube([c[2], c[3], T]);
        }
        translate([0, 0, T - text_depth]) linear_extrude(height = text_depth + 1) lettering_2d();
    }
}

color(swatch_color) card();

if (text_mode == "inlay")
    color(text_color)
        translate([0, 0, T - text_depth]) linear_extrude(height = text_depth) lettering_2d();
