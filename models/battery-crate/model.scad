// Stackable battery crate — an open crate with a grid of cells sized for one
// battery type, an interlocking foot so crates stack, handle cut-outs in the
// ends and a label inlaid in the front wall.
//
// Cells hold the batteries standing up, `height_pct` of their length deep.
// A stackable crate's outer walls rise past the tops of the batteries, and
// its chamfered foot nests inside the rim of the crate below. Prints upright
// with no supports: the foot's step is a 45 degree chamfer, the handle slots
// have 45 degree shoulders under a short bridge, and the label is inlaid in a
// vertical wall.
//
// Written to the MakerWorld Parametric Model Maker customizer conventions so
// the same file works unchanged on MakerWorld and in ScadBuddy.
//
// The colour parameters are the extruder order: crate_color is extruder 1,
// label_color is extruder 2.

/* [Battery] */

// Battery type the cells are sized for
cell = "AA"; // [AAA, AA, C, D, 9V, 18650, CR2032]

// Clearance in mm added to the battery's diameter (or to each dimension of a 9V / coin cell)
clearance = 0.6; // [0.2:0.1:1.5]

/* [Layout] */

// Cells along X (capped so the crate fits a 300 mm wide plate)
cols = 4; // [1:1:12]

// Cells along Y (capped so the crate fits a 320 mm deep plate)
rows = 2; // [1:1:8]

// Cell depth as a percentage of the battery's length
height_pct = 60; // [30:5:100]

// Raise the walls past the batteries and add a nesting foot so crates stack
stackable = true;

// Cut handle slots into the two end walls
handle_cutouts = true;

// Open crate (square cells, thin grid walls, push-out holes) or solid block with fitted pockets
style = "crate"; // [crate, solid_block]

/* [Label] */

// Label on the front wall (leave empty to use the battery name)
label_text = ""; // 12

// Label typeface
font = "DejaVu Sans:style=Bold"; // font

/* [Colors] */

// Crate colour (extruder 1)
crate_color = "#D23C2A"; // color

// Label colour (extruder 2)
label_color = "#FFFFFF"; // color

/* [Hidden] */

$fn = 64;

// [name, kind, x, y, length] -- maximum dimensions from the IEC 60086 /
// manufacturer datasheets. Round cells stand on end; the 9V stands terminals
// up with its long side along X; coin cells stand on edge in slots, thin
// side along X, so "length" is the diameter.
cells = [
    ["AAA",    "round", 10.5, 10.5, 44.5],   // R03 / LR03
    ["AA",     "round", 14.5, 14.5, 50.5],   // R6 / LR6
    ["C",      "round", 26.2, 26.2, 50.0],   // R14 / LR14
    ["D",      "round", 34.2, 34.2, 61.5],   // R20 / LR20
    ["9V",     "rect",  26.5, 17.5, 48.5],   // 6LR61
    ["18650",  "round", 18.6, 18.6, 65.2],   // unprotected flat/button top
    ["CR2032", "rect",   3.2, 20.0, 20.0],   // coin cell on edge
];
spec = cells[search([cell], cells, 1, 0)[0]];
kind = spec[1];
cell_len = spec[4];
cx = spec[2] + clearance;
cy = spec[3] + clearance;

crate = style == "crate";
t = crate ? 1.2 : 1.6;               // grid wall between cells
fl = 1.6;                            // floor
rim_t = 1.6;                         // wall that rises round the top when stackable
stack_tol = 0.3;                     // foot-to-rim clearance
foot_h = 4;                          // how far the foot drops into the crate below
headroom = 1;                        // battery top to the crate above
fi = rim_t + stack_tol;              // foot inset from the outside
ow = stackable ? 3.2 : 2.4;          // side walls
recess = 5;                          // blind handle recess depth (solid block)
end_t = (handle_cutouts && !crate) ? ow + recess : ow;
label_depth = 0.8;
corner = 2;

// Printable area of the H2C with both nozzles. Big cells overflow it well
// inside the slider ranges (12 x 8 D cells is 437 x 293 mm), so the grid is
// capped to the most cells that fit.
bed_x = 300;
bed_y = 320;
ncols = max(1, min(cols, floor((bed_x - 2 * end_t + t) / (cx + t))));
nrows = max(1, min(rows, floor((bed_y - 2 * ow + t) / (cy + t))));

grid_x = ncols * cx + (ncols - 1) * t;
grid_y = nrows * cy + (nrows - 1) * t;
X = grid_x + 2 * end_t;
Y = grid_y + 2 * ow;

grid_top = fl + cell_len * height_pct / 100;
H = stackable ? fl + cell_len + headroom + foot_h : grid_top;
rim_z = stackable ? H - foot_h : H;  // top of the full-thickness wall
z0 = stackable ? foot_h + fi : 0;    // bottom of the vertical outside faces

label = len(label_text) > 0 ? label_text : cell;

// ---- outline ---------------------------------------------------------------

module outer_2d(inset = 0) {
    r = max(0.5, corner - inset);
    offset(r = r) square([X - 2 * inset - 2 * r, Y - 2 * inset - 2 * r], center = true);
}

module outer_solid() {
    if (stackable) {
        linear_extrude(foot_h) outer_2d(fi);
        hull() {
            translate([0, 0, foot_h - 0.01]) linear_extrude(0.01) outer_2d(fi);
            translate([0, 0, foot_h + fi]) linear_extrude(0.01) outer_2d(0);
        }
        translate([0, 0, foot_h + fi]) linear_extrude(H - foot_h - fi) outer_2d(0);
    } else {
        linear_extrude(H) outer_2d(0);
    }
}

// ---- cells -------------------------------------------------------------------

function cell_centre(i, j) = [-grid_x / 2 + cx / 2 + i * (cx + t),
                              -grid_y / 2 + cy / 2 + j * (cy + t)];

module pocket_2d() {
    if (crate || kind != "round") square([cx, cy], center = true);
    else circle(d = cx);
}

hole_d = 0.5 * min(cx, cy);

module cells_cut() {
    for (i = [0 : ncols - 1], j = [0 : nrows - 1])
        translate(cell_centre(i, j)) {
            translate([0, 0, fl]) linear_extrude(H) pocket_2d();
            if (crate && hole_d >= 3)
                translate([0, 0, -1]) cylinder(d = hole_d, h = fl + 2);
        }
}

// ---- handles -------------------------------------------------------------------

h_top = rim_z - 3;
h_bot = max(z0 + 2, fl + 2);
h_h = min(12, h_top - h_bot);
h_w = min(40, grid_y * 0.6);
handles = handle_cutouts && h_h >= 6 && h_w >= 10;

module handle_2d() {
    c = min(h_h / 2, h_w / 4);
    polygon([[-h_w / 2, 0], [h_w / 2, 0], [h_w / 2, h_h - c], [h_w / 2 - c, h_h],
             [-h_w / 2 + c, h_h], [-h_w / 2, h_h - c]]);
}

// A handle slot extruded along +X from x = 0 for `len`.
module handle_slot(len) {
    rotate([90, 0, 90]) linear_extrude(len) handle_2d();
}

// ---- label -------------------------------------------------------------------

l_bot = z0 + 2;
l_top = rim_z - 2;
l_h = l_top - l_bot;
l_w = X - 2 * end_t - 4;
l_size = min(0.75 * l_h, l_w / (1.1 * max(1, len(label))));
has_label = l_h >= 4 && l_w >= 10;

module label_2d() {
    intersection() {
        text(label, size = l_size, font = font, halign = "center", valign = "center");
        square([l_w, l_h], center = true);
    }
}

// The inlay: label_depth into the front wall, flush with its face.
module label_solid(extra = 0) {
    translate([0, -Y / 2 + label_depth, (l_bot + l_top) / 2])
        rotate([90, 0, 0]) linear_extrude(label_depth + extra) label_2d();
}

// ---- crate -------------------------------------------------------------------

module crate_body() {
    difference() {
        outer_solid();
        cells_cut();
        // open space above the cells, inside the walls
        translate([0, 0, grid_top]) linear_extrude(H) square([grid_x, grid_y], center = true);
        // rim the crate above nests into
        if (stackable) translate([0, 0, rim_z]) linear_extrude(H) outer_2d(rim_t);
        if (handles) for (s = [-1, 1])
            translate([s * X / 2, 0, h_top - h_h]) rotate(s > 0 ? 180 : 0)
                translate([-1, 0, 0]) handle_slot((crate ? end_t + 0.01 : recess) + 1);
        if (has_label) label_solid(1);
    }
}

color(crate_color) crate_body();
if (has_label) color(label_color) label_solid();
