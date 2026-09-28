// Dollhouse kit -- a modular 1:6 ("playscale", 29-30 cm fashion doll)
// dollhouse built from printed panels on one grid. Pick a piece from the
// dropdown; every piece made with the same Grid settings joins every other.
//
// Scale: 1 ft = 50.8 mm. A 30 cm doll is a 5'9" person, the default grid
// module of 150 mm is 0.9 m, and a room is two wall courses of 210 mm
// (420 mm = 2.5 m ceiling). Every piece fits a 300 x 300 x 300 mm envelope,
// which is inside the Bambu H2C's two-nozzle volume (300 x 320 x 325 mm).
//
// How pieces join (see README for the diagram):
//  * wall to wall in line, wall to corner post, course to course, floor tile
//    to floor tile, stair half to stair half: an hourglass key (45-degree
//    flares, 14 x 20 mm) pressed into matching pockets that straddle the
//    joint. Pockets open on the inside face of walls and on the underside of
//    floors, so the outside of the house stays clean.
//  * wall to floor, and storey to storey: pegs through the floor tile's edge
//    holes into holes in the bottom and top edges of the walls.
//  * Walls sit on the outer band of the floor tiles with their outside face
//    flush with the tile edge. Corner posts are L-shaped with half-module arms,
//    so every wall joint falls half a module along the grid and walls of whole
//    (or half) modules always meet.
//
// Print orientation, all supportless:
//  * walls, door leaves, floors, roof panels, railings and connectors lie
//    flat. Walls lie on their INSIDE face: wallpaper, wainscoting, baseboard
//    and crown moulding are flush inlays in the first 3 layers; siding, brick,
//    stone, frames, shutters and window boxes are relief on top. All colour
//    detail of a wall lives in its bottom 3 and top ~20 layers.
//  * corner posts stand upright (L footprint, one course tall).
//  * stair halves lie on one side stringer.
//
// Written to the MakerWorld Parametric Model Maker customizer conventions so
// the same file works unchanged on MakerWorld and in ScadBuddy.
//
// Extruder order is the order of the colour parameters below: wall_color is
// extruder 1, exterior_color 2, ... clip_color 31. Only the colours a piece
// uses become parts; colours with the same value merge into one filament.

/* [Piece] */

// Which piece to make
piece = "wall_window"; // [wall:Wall, wall_window:Wall with window, wall_door_lower:Door wall - lower course, wall_door_upper:Door wall - upper course, door_leaf_lower:Door leaf - lower half, door_leaf_upper:Door leaf - upper half, corner_post:Corner post, floor_tile:Floor tile, roof_panel:Roof panel, stairs_lower:Stairs - lower half, stairs_upper:Stairs - upper half, railing:Railing, connectors:Connectors (keys / pegs / hinge pins)]

// Which course of the storey a wall, window wall or corner post is: lower stands on the floor (baseboard, wainscoting), upper meets the ceiling (crown moulding). Door walls set this themselves
course = "upper"; // [lower:Lower course, upper:Upper course]

/* [Grid] */

// Grid module in mm; all pieces are multiples of it (150 = 0.9 m at 1:6). Use the same value for every piece of a house
module_size = 150; // [100:5:150]

// Length of a wall, railing, floor tile or roof panel in modules (door walls are at least 1)
width_units = 1; // [0.5:0.5:2]

// Depth of a floor tile or roof panel in modules
depth_units = 2; // [0.5:0.5:2]

// Height of one wall course; a room is two courses (210 = 420 mm ceiling = 2.5 m at 1:6). Same value for every piece of a house
course_height = 210; // [180:5:240]

// Wall thickness (also railings and the keys' pocket depth)
wall_thickness = 6; // [4:0.5:10]

// Floor tile and roof panel thickness
floor_thickness = 6; // [5:0.5:10]

/* [Exterior] */

// Outside face of walls and corner posts
exterior = "plain"; // [plain, siding, brick, stone, stucco]

// Brick course height in mm; siding boards are 1.6x this and stone rows 2.2x
texture_size = 12; // [8:1:20]

/* [Interior] */

// Wallpaper on the inside face (flush inlay, two colours)
wallpaper = "stripes"; // [none, stripes, polka_dots, diamonds, hearts, stars]

// Wallpaper repeats per grid module (the pattern lines up across wall joints)
pattern_repeats = 6; // [2:1:12]

// Wainscoting (panelled lower wall with a rail) on lower courses
wainscoting = true;

// Wainscoting height from the floor
wainscot_height = 90; // [40:5:150]

// Baseboard along the bottom of lower courses
baseboard = true;

// Crown moulding along the top of upper courses
crown_moulding = true;

/* [Windows] */

// Window shape
window_style = "square"; // [square, arched, round]

// Window opening width (round: diameter); shrinks to fit the wall
window_width = 70; // [40:5:180]

// Window opening height (ignored for round)
window_height = 120; // [40:5:200]

// Window sill height above the bottom of the course
window_sill = 40; // [20:5:120]

// Panes across
window_panes_x = 2; // [1:1:4]

// Panes down
window_panes_y = 2; // [1:1:4]

// Thin glass pane in the window (print it in a translucent filament)
glass = true;

// Shutters either side of square and arched windows (when there is room)
shutters = true;

// Flower box under the window, with flowers
flower_box = true;

// Window and door frames, sills and thresholds in their own colours (off: wall colour; glazing bars keep the frame colour)
trim = true;

/* [Doors] */

// Door shape: plain and arched hang one leaf (two halves), french hangs a glazed pair
door_style = "plain"; // [plain, arched, french]

// Clear door width
door_width = 100; // [80:5:140]

// Clear door height above the threshold (320 or more lets a 30 cm doll walk through)
door_height = 330; // [320:5:400]

// Raised panels per door leaf half (plain and arched doors)
door_panels = 2; // [1:1:4]

/* [Floors & roof] */

// Floor tile top
floor_texture = "wood_planks"; // [plain, wood_planks, herringbone, tiles, checkerboard]

// Planks per grid module (wood planks and herringbone)
planks_per_module = 8; // [4:1:12]

// Tiles per grid module (tiles, checkerboard, roof shingle and tile rows)
tiles_per_module = 5; // [2:1:10]

// Rug inlaid in the middle of a floor tile
rug = "none"; // [none, rectangle, oval]

// Cut a stairwell along the left edge of a floor tile (for the upper floor over the stairs)
stairwell = false;

// Roof panel top
roof_style = "shingles"; // [flat, shingles, tiles]

/* [Stairs & railing] */

// Stair width, both stringers included
stair_width = 100; // [60:5:140]

// Tread depth (the rise follows from the storey height, about 35 mm)
tread_depth = 24; // [20:1:32]

// Railing height (150 = 0.9 m at 1:6)
railing_height = 150; // [100:5:180]

/* [Connectors] */

// Which connectors the Connectors piece prints
connector_type = "keys"; // [keys:Hourglass keys, pegs:Pegs, hinge_pins:Door hinge pins]

// How many (hinge pins: sets per door)
connector_count = 12; // [1:1:30]

/* [Fit] */

// Gap per side between a key and its pocket, and around pegs and hinge pins
clip_clearance = 0.2; // [0.1:0.05:0.5]

/* [Walls outside colours] */

// Wall body, and the outside face when the exterior is plain
wall_color = "#F8BBD0"; // color

// Siding, brick, stone or stucco
exterior_color = "#F48FB1"; // color

// Mortar between bricks and stones
mortar_color = "#FFFFFF"; // color

// Window sills and door thresholds
trim_color = "#FFFFFF"; // color

/* [Walls inside colours] */

// Inside face background
wall_inside_color = "#FFF8E1"; // color

// Wallpaper pattern, first colour
wallpaper_color = "#F06292"; // color

// Wallpaper pattern, second colour
wallpaper_color_2 = "#CE93D8"; // color

// Wainscoting
wainscot_color = "#FFFFFF"; // color

// Baseboard and wainscot rail
baseboard_color = "#FFFFFF"; // color

// Crown moulding
crown_color = "#FFFFFF"; // color

/* [Windows & doors colours] */

// Window frame, sill-side architrave and glazing bars
window_frame_color = "#FFFFFF"; // color

// Window glass
window_pane_color = "#B3E5FC"; // color

// Shutters
shutter_color = "#BA68C8"; // color

// Flower box
flower_box_color = "#A1887F"; // color

// Flowers
flower_color = "#FF4081"; // color

// Flowers, second colour
flower_color_2 = "#FFEB3B"; // color

// Leaves
leaf_color = "#66BB6A"; // color

// Door frame and hinge blocks
door_frame_color = "#FFFFFF"; // color

// Door leaf
door_color = "#9575CD"; // color

// Door knob
door_knob_color = "#FFD54F"; // color

/* [Floors colours] */

// Floor
floor_color = "#D7A86E"; // color

// Plank seams, herringbone, grout, checkerboard
floor_accent_color = "#8D6E63"; // color

// Rug
rug_color = "#80DEEA"; // color

// Rug border
rug_border_color = "#FFFFFF"; // color

/* [Roof, stairs & railing colours] */

// Roof
roof_color = "#7E57C2"; // color

// Roof accent: odd shingles, tile valleys, flat-roof border
roof_accent_color = "#B39DDB"; // color

// Stair treads
stair_tread_color = "#C8945A"; // color

// Stair risers and stringers
stair_riser_color = "#FFFFFF"; // color

// Railing posts and rails
railing_color = "#FFFFFF"; // color

// Railing spindles
spindle_color = "#F06292"; // color

// Keys, pegs and hinge pins
clip_color = "#FFFFFF"; // color

/* [Hidden] */

// "room" assembles a corner of a room for the README picture
preview = "";

$fn = 48;

M = module_size;
G = 0.4;                    // joint gap between pieces along the grid
CH = course_height;
T = wall_thickness;
FT = floor_thickness;
C = clip_clearance;
S = 0.6;                    // inlay skin: 3 layers at 0.2
REL = 1.2;                  // exterior relief
EPS = 0.01;

// Hourglass key: neck KN at the joint, 45-degree flares out to KH at +/-KA.
KA = 7; KN = 6; KH = KN + 2 * KA;
KT = min(3, T / 2);         // key thickness
KD = KT + C;                // pocket depth

// Pegs: octagonal, in round holes; wall holes PD deep.
PEG = min(3, T - 2.2);
PH = PEG + 2 * C;
PD = 12;
PL = PD + FT - 1.5;

LIN = trim ? 1.6 : 0;       // frame lining inside openings
FW = trim ? 7 : 0;          // architrave width
FR = 1.6;                   // architrave relief
SILL_H = 6; SILL_R = 4; SILL_X = 6;
MT = min(T, 3);             // glazing bar depth
FBH = 22; FBD = 10;         // flower box height and depth

THR = 5;                    // door threshold height
TAB = 8;                    // hinge block height
LT = T - 1;                 // door leaf thickness
HP = max(1.2, min(2.4, LT - 2));   // hinge pin
HH = HP + 2 * C;

BB = 14; CR = 12; RAIL = 4; // baseboard, crown, wainscot rail heights

function wl(u) = u * M - G;
function du(u) = max(u, 1);
function is_door(k) = k == "door_lower" || k == "door_upper";
function kind_len(k, u) = wl(is_door(k) ? du(u) : u);
function seq(n) = n > 0 ? [0 : n - 1] : [];

module slab(z0, h) { translate([0, 0, z0]) linear_extrude(h) children(); }
module rect(x0, y0, x1, y1) { translate([x0, y0]) square([x1 - x0, y1 - y0]); }

// ------------------------------------------------------------ connectors

module key2d(grow = 0) {
    offset(delta = grow)
        polygon([[-KA, -KH / 2], [0, -KN / 2], [KA, -KH / 2],
                 [KA, KH / 2], [0, KN / 2], [-KA, KH / 2]]);
}

// Horizontal hole along +y, point up: circle plus a 45-degree roof.
module teardrop2d(r) {
    circle(r = r, $fn = 24);
    polygon([[-r / sqrt(2), -r / sqrt(2)], [0, -r * sqrt(2)],
             [r / sqrt(2), -r / sqrt(2)], [0, 0]]);
}
module teardrop_y(r, len) { rotate([-90, 0, 0]) linear_extrude(len) teardrop2d(r); }

// Along-edge positions, measured from a piece's own origin.
function peg_xs(L) = [for (k = [0 : 20]) let (x = (2 * k + 1) * M / 4 - G / 2)
                      if (x + PH / 2 + KA + C + 3 <= L) x];
function edge_key_xs(L) = [for (k = [0 : 10]) let (x = (k + 0.5) * M - G / 2)
                           if (x + KH / 2 + C + 3 <= L) x];

// ------------------------------------------------------------ windows

function win_top_max() = CH - PD - 4 - LIN;
function win_sill() = min(window_sill, win_top_max() - 30);
function win_room() = win_top_max() - win_sill();
function win_wmax(u) = wl(u) - 2 * (KA + C + 6 + LIN);
function win_w(u) =
    min(window_width, win_wmax(u), win_w_tall());
// the width an arched or round window may have for the height it gets
function win_w_tall() =
    window_style == "round" ? win_room()
  : window_style == "arched" ? 2 * (min(window_height, win_room()) - 5)
  : window_width;
function win_h(u) = window_style == "round" ? win_w(u) : min(window_height, win_room());
// height of the straight-sided part (shutters run this high)
function win_hs(u) = window_style == "arched" ? win_h(u) - win_w(u) / 2 : win_h(u);

module win_open2d(u) {
    w = win_w(u); h = win_h(u);
    translate([wl(u) / 2 - w / 2, win_sill()])
        if (window_style == "round") translate([w / 2, w / 2]) circle(d = w, $fn = 96);
        else if (window_style == "arched") {
            square([w, h - w / 2]);
            translate([w / 2, h - w / 2]) circle(d = w, $fn = 96);
        } else square([w, h]);
}

// ------------------------------------------------------------ doors
// Door geometry is in storey coordinates: y = 0 is the floor, the lower
// course is 0..CH and the upper course CH..2CH.

function dr_L(u) = wl(du(u));
function dr_cx(u) = dr_L(u) / 2;
function dr_w(u) = min(door_width, dr_L(u) - 2 * (KA + C + 5 + LIN + FW));
function dr_top() = THR + min(door_height, 2 * CH - 25 - THR);
function dr_axes(u) =
    let (cx = dr_cx(u), w = dr_w(u))
    door_style == "french" ? [cx - w / 2 + 1 + LT / 2, cx + w / 2 - 1 - LT / 2]
                           : [cx - w / 2 + 1 + LT / 2];
// top of the opening (inset = 0) or of a leaf (inset = gap) at x
function dr_top_at(u, x, inset) =
    let (w = dr_w(u), cx = dr_cx(u), r = w / 2 - inset, yc = dr_top() - w / 2)
    door_style == "arched" ? yc + sqrt(max(0, r * r - (x - cx) * (x - cx)))
                           : dr_top() - inset;
function knob_y() = min(165, CH - TAB - 14);

module door_open2d(u) {
    w = dr_w(u); top = dr_top();
    translate([dr_cx(u) - w / 2, THR])
        if (door_style == "arched") {
            square([w, top - THR - w / 2]);
            translate([w / 2, top - THR - w / 2]) circle(d = w, $fn = 96);
        } else square([w, top - THR]);
}

module door_tabs2d(u) {
    cx = dr_cx(u); w = dr_w(u);
    for (a = dr_axes(u))
        if (a < cx) rect(cx - w / 2, CH - TAB, a + 3, CH);
        else rect(a - 3, CH - TAB, cx + w / 2, CH);
}

// ------------------------------------------------------------ wall 2D regions
// Wall piece coordinates: x along the wall as seen from outside, y up, z out
// of the wall (z = 0 is the inside face, on the bed).

function k_off(k) = k == "door_upper" ? CH : 0;

module open2d(k, u) {
    if (k == "window") win_open2d(u);
    else if (is_door(k)) translate([0, -k_off(k)]) door_open2d(u);
}

module cut2d(k, u) {
    if (k == "window") offset(delta = LIN) win_open2d(u);
    else if (is_door(k)) translate([0, -k_off(k)]) intersection() {
        offset(delta = LIN) door_open2d(u);
        rect(-1, THR, 1000, 1000);
    }
}

module thr2d(k, u) {
    if (k == "door_lower") let (w = dr_w(u), cx = dr_cx(u))
        rect(cx - w / 2 - LIN, 0, cx + w / 2 + LIN, THR);
}

module tabs2d(k, u) { if (k == "door_lower") door_tabs2d(u); }

module piece2d(k, u) { square([kind_len(k, u), CH]); }

module W2(k, u) { difference() { piece2d(k, u); cut2d(k, u); thr2d(k, u); } }

module lining2d(k, u) {
    intersection() { piece2d(k, u); difference() { cut2d(k, u); open2d(k, u); } }
}

// architrave: frame colour on the inside skin and raised outside
module F2(k, u) {
    if (trim && k != "wall") intersection() {
        piece2d(k, u);
        difference() {
            offset(delta = LIN + FW) open2d(k, u);
            cut2d(k, u); thr2d(k, u);
        }
    }
}

function has_sill() = trim && window_style != "round";

module sill2d(k, u) {
    if (k == "window" && has_sill()) let (w = win_w(u), cx = wl(u) / 2, y0 = win_sill() - LIN)
        rect(cx - w / 2 - LIN - SILL_X, y0 - SILL_H, cx + w / 2 + LIN + SILL_X, y0);
}

function box_top() = win_sill() - LIN - (has_sill() ? SILL_H : 0);
function box_h() = min(FBH, box_top() - 1);
function has_box() = flower_box && box_h() >= 14;
function box_x0(u) = wl(u) / 2 - win_w(u) / 2 - LIN - SILL_X + 3;
function box_x1(u) = wl(u) / 2 + win_w(u) / 2 + LIN + SILL_X - 3;

module box2d(k, u) {
    if (k == "window" && has_box())
        rect(box_x0(u), box_top() - box_h(), box_x1(u), box_top());
}

function sh_w(u) = min(win_w(u) / 2, (wl(u) - win_w(u)) / 2 - LIN - FW - 3);
function has_shutters(u) = shutters && window_style != "round" && sh_w(u) >= 10;

module shutters2d(k, u) {
    if (k == "window" && has_shutters(u)) {
        w = win_w(u); cx = wl(u) / 2; sw = sh_w(u);
        y0 = win_sill() - LIN; y1 = win_sill() + win_hs(u) + LIN;
        rect(cx - w / 2 - LIN - FW - sw, y0, cx - w / 2 - LIN - FW, y1);
        rect(cx + w / 2 + LIN + FW, y0, cx + w / 2 + LIN + FW + sw, y1);
    }
}

module louvres2d(k, u) {
    if (k == "window" && has_shutters(u)) {
        y0 = win_sill() - LIN; y1 = win_sill() + win_hs(u) + LIN;
        for (y = [y0 + 5 : 5 : y1 - 6]) rect(-1, y, 1000, y + 2);
    }
}

module muntins2d(u) {
    w = win_w(u); h = win_h(u); x0 = wl(u) / 2 - w / 2; y0 = win_sill();
    intersection() {
        win_open2d(u);
        union() {
            for (i = [1 : window_panes_x - 1]) translate([x0 + i * w / window_panes_x - 1.2, y0 - 1]) square([2.4, h + 2]);
            for (j = [1 : window_panes_y - 1]) translate([x0 - 1, y0 + j * h / window_panes_y - 1.2]) square([w + 2, 2.4]);
        }
    }
}

// exterior relief goes everywhere the frame, sill, box and shutters are not
module E2(k, u) {
    difference() { W2(k, u); F2(k, u); sill2d(k, u); box2d(k, u); shutters2d(k, u); }
}

// ------------------------------------------------------------ patterns
// Anchored to the house grid, so they continue across joints: wall x = 0 is
// half a module (plus half a gap) along the grid, and an upper course starts
// one course up.

X_ANCHOR = M / 2 + G / 2;
function ph(v, p) = v - p * floor(v / p);

module lattice(L, H, px, py, xoff, yoff, shift = [0, 0]) {
    ox = ph(xoff, px); oy = ph(yoff, py);
    for (i = [-1 : ceil(L / px) + 1], j = [-1 : ceil(H / py) + 1])
        translate([i * px - ox + shift[0] * px, j * py - oy + shift[1] * py]) children();
}

module heart2d(s) {
    r = s / 4;
    translate([0, s * 0.12]) {
        hull() { translate([-r, 0]) circle(r = r, $fn = 20); translate([0, -2.3 * r]) square(0.01, center = true); }
        hull() { translate([r, 0]) circle(r = r, $fn = 20); translate([0, -2.3 * r]) square(0.01, center = true); }
    }
}

module star2d(ro) {
    polygon([for (i = [0 : 9]) let (a = 90 + i * 36, r = i % 2 ? ro * 0.42 : ro) [r * cos(a), r * sin(a)]]);
}

module diamond2d(w, h) { polygon([[0, -h / 2], [w / 2, 0], [0, h / 2], [-w / 2, 0]]); }

// which = 1 or 2 (the two wallpaper colours)
module wallpaper2d(which, L, H, yoff) {
    p = M / pattern_repeats;
    if (wallpaper == "stripes") {
        lattice(L, H, p, 1000, X_ANCHOR, 0)
            if (which == 1) translate([0, -1]) square([p / 2, H + 2]);
            else translate([0.75 * p - 0.6, -1]) square([1.2, H + 2]);
    } else if (wallpaper != "none") {
        lattice(L, H, p, p, X_ANCHOR, yoff, which == 1 ? [0, 0] : [0.5, 0.5])
            if (wallpaper == "polka_dots") circle(d = 0.36 * p, $fn = 20);
            else if (wallpaper == "diamonds") { if (which == 1) diamond2d(0.9 * p, 0.9 * p); else diamond2d(0.3 * p, 0.3 * p); }
            else if (wallpaper == "hearts") heart2d(0.5 * p);
            else if (wallpaper == "stars") star2d(which == 1 ? 0.3 * p : 0.2 * p);
    }
}

module bricks2d(L, H, yoff) {
    bh = texture_size; bl = 2 * bh;
    for (j = [-1 : ceil(H / bh) + 1]) {
        y = j * bh - ph(yoff, bh);
        o = ph(X_ANCHOR + ((floor((yoff + y + 0.5) / bh) % 2) ? bl / 2 : 0), bl);
        for (i = [-1 : ceil(L / bl) + 1]) translate([i * bl - o + 0.6, y + 0.6]) square([bl - 1.2, bh - 1.2]);
    }
}

STONES = [1.6, 2.3, 1.2, 2.0, 1.4, 2.6, 1.8, 1.1, 2.2];
function cum(v, n) = n <= 0 ? 0 : cum(v, n - 1) + v[(n - 1) % len(v)];

module stones2d(L, H, yoff) {
    rh = 2.2 * texture_size;
    for (j = [-1 : ceil(H / rh) + 1]) {
        y = j * rh - ph(yoff, rh);
        row = round((yoff + y) / rh);
        st = ph(row * 3, len(STONES));
        o = ph(X_ANCHOR + row * 0.7 * texture_size, 30 * texture_size);
        for (i = [0 : 60]) let (x0 = cum(STONES, st + i) * texture_size - cum(STONES, st) * texture_size - o,
                                sw = STONES[(st + i) % len(STONES)] * texture_size)
            if (x0 < L + 1 && x0 + sw > -1)
                translate([x0 + 1, y + 1]) offset(r = 1.5, $fn = 12) offset(delta = -1.5) square([sw - 2, rh - 2]);
    }
}

module stucco2d(L, H) {
    for (i = [0 : floor(L / 8)], j = [0 : floor(H / 8)])
        translate([i * 8 + ((j * 5) % 7) * 0.6 + 2, j * 8 + ((i * 3) % 5) * 0.8 + 2]) circle(d = 2.4, $fn = 8);
}

module siding3d(L, H, yoff, z0) {
    sb = 1.6 * texture_size;
    for (j = [-1 : ceil(H / sb) + 1]) let (y = j * sb - ph(yoff, sb))
        hull() {
            translate([-1, y, z0]) cube([L + 2, EPS, REL]);
            translate([-1, y + sb - EPS, z0]) cube([L + 2, EPS, 0.4]);
        }
}

// ------------------------------------------------------------ wall pieces

// $crs is the course of plain and window walls and corner posts (the preview
// sets it per piece); door walls know their own course.
$crs = course;
function crs(k) = k == "door_lower" ? "lower" : k == "door_upper" ? "upper" : $crs;
function wh() = min(wainscot_height, CH - 10);
function outer_skin() = exterior == "brick" || exterior == "stone" || exterior == "stucco";

// inside bands, piece y ranges: [baseboard, wainscot, rail, paper, crown]
function band_base(k) = crs(k) == "lower" && baseboard ? [0, BB] : [0, 0];
function band_wain(k) = crs(k) == "lower" && wainscoting ? [band_base(k)[1], wh() - RAIL] : [0, 0];
function band_rail(k) = crs(k) == "lower" && wainscoting ? [wh() - RAIL, wh()] : [0, 0];
function band_crown(k) = crs(k) == "upper" && crown_moulding ? [CH - CR, CH] : [CH, CH];
function band_paper(k) = [max(band_base(k)[1], band_rail(k)[1]), band_crown(k)[0]];

module band(b) { if (b[1] > b[0]) rect(-1, b[0], 1000, b[1]); }

module IN2(k, u) { difference() { W2(k, u); F2(k, u); } }

module paper2d(k, u) { intersection() { IN2(k, u); band(band_paper(k)); } }

module pattern_in(k, u, which) {
    intersection() {
        paper2d(k, u);
        wallpaper2d(which, kind_len(k, u), CH, crs(k) == "upper" ? CH : 0);
    }
}

// pattern colour 2 never touches colour 1 by construction; subtract anyway
module pattern2_in(k, u) { difference() { pattern_in(k, u, 2); pattern_in(k, u, 1); } }

module wall_holes(k, u) {
    L = kind_len(k, u);
    door = is_door(k);
    cx = dr_cx(u); w = dr_w(u);
    // in-line joints: keys at quarter heights of both ends
    for (x = [-G / 2, L + G / 2], y = [CH / 4, 3 * CH / 4])
        translate([x, y, -EPS]) linear_extrude(KD + EPS) key2d(C);
    // course joints: keys on the top and bottom edges, pegs beside them
    for (e = [0, 1]) {
        blocked = door && (e == 0 || k == "door_lower");
        for (x = edge_key_xs(L))
            if (!(blocked && abs(x - cx) < w / 2 + LIN + KH / 2 + C + 2))
                translate([x, e * CH, -EPS]) linear_extrude(KD + EPS) rotate(90) key2d(C);
        for (x = peg_xs(L))
            if (!(blocked && abs(x - cx) < w / 2 + LIN + PH / 2 + 2))
                translate([x, e ? CH - PD : -EPS, T / 2]) teardrop_y(PH / 2, PD + EPS);
    }
    // door hinges
    if (k == "door_lower") for (a = dr_axes(u)) {
        translate([a, CH - TAB - EPS, T / 2]) teardrop_y(HH / 2, TAB + 2 * EPS);
        translate([a, THR - 3.5, T / 2]) teardrop_y(HH / 2, 3.5 + EPS);
    }
    if (k == "door_upper") for (a = dr_axes(u)) let (y0 = dr_top_at(u, a, 0) - CH - 1)
        translate([a, y0, T / 2]) teardrop_y(HH / 2, CH - y0 + EPS);
}

module wall_part(col, k, u) { color(col) difference() { children(); wall_holes(k, u); } }

function fl_n(u) = max(1, floor((box_x1(u) - box_x0(u) - 4) / 15));
function fl_x(u, i) = box_x0(u) + (i + 0.5) * (box_x1(u) - box_x0(u)) / fl_n(u);
function fl_y() = box_top() - box_h() / 2 + 1;

module petals2d() { for (a = [0 : 72 : 359]) rotate(a) translate([2.6, 0]) circle(r = 2.6, $fn = 16); }

module flowers2d(u, which) {
    for (i = [0 : fl_n(u) - 1]) if (i % 2 == which - 1) translate([fl_x(u, i), fl_y()]) petals2d();
}

// domed blossoms: straight-sided extrude that narrows, printed on the box face
module flowers3d(u, which, z0) {
    for (i = [0 : fl_n(u) - 1]) if (i % 2 == which - 1)
        translate([fl_x(u, i), fl_y(), z0]) linear_extrude(2.5, scale = 0.7) petals2d();
}

module leaves2d(u) {
    n = fl_n(u); sp = (box_x1(u) - box_x0(u)) / n;
    difference() {
        for (i = [0 : n]) translate([box_x0(u) + i * sp, fl_y() - 4])
            for (s = [-1, 1]) rotate(s * 35) scale([1, 0.45]) circle(r = 4.5, $fn = 20);
        flowers2d(u, 1); flowers2d(u, 2);
    }
}

module wall_piece(k, u) {
    L = kind_len(k, u);
    yoff = crs(k) == "upper" ? CH : 0;
    frame_col = is_door(k) ? door_frame_color : window_frame_color;
    top_body = T - (outer_skin() ? S : 0);

    wall_part(wall_color, k, u) {
        slab(S, top_body - S) W2(k, u);
        if (!trim) slab(0, T) thr2d(k, u);
        if (!trim) slab(0, T) tabs2d(k, u);
    }
    // outside
    if (exterior == "brick" || exterior == "stone")
        wall_part(mortar_color, k, u) slab(T - S, S) W2(k, u);
    if (exterior != "plain") wall_part(exterior_color, k, u) {
        if (exterior == "stucco") {
            slab(T - S, S) W2(k, u);
            slab(T, 0.4) intersection() { E2(k, u); stucco2d(L, CH); }
        } else if (exterior == "siding") intersection() {
            slab(T, REL + EPS) E2(k, u);
            siding3d(L, CH, yoff, T);
        } else slab(T, REL) intersection() {
            E2(k, u);
            if (exterior == "brick") bricks2d(L, CH, yoff); else stones2d(L, CH, yoff);
        }
    }
    // inside
    wall_part(wall_inside_color, k, u) slab(0, S) difference() { paper2d(k, u); pattern_in(k, u, 1); pattern2_in(k, u); }
    wall_part(wallpaper_color, k, u) slab(0, S) pattern_in(k, u, 1);
    wall_part(wallpaper_color_2, k, u) slab(0, S) pattern2_in(k, u);
    wall_part(wainscot_color, k, u) slab(0, S) intersection() { IN2(k, u); band(band_wain(k)); }
    wall_part(baseboard_color, k, u) slab(0, S) intersection() { IN2(k, u); union() { band(band_base(k)); band(band_rail(k)); } }
    wall_part(crown_color, k, u) slab(0, S) intersection() { IN2(k, u); band(band_crown(k)); }
    // frames
    wall_part(frame_col, k, u) {
        slab(0, S) F2(k, u);
        slab(T, FR) difference() { F2(k, u); sill2d(k, u); box2d(k, u); }
        slab(0, T + FR) lining2d(k, u);
        if (trim) slab(0, T) tabs2d(k, u);
        if (k == "window") slab(0, MT) muntins2d(u);
    }
    if (k == "window" && glass)
        wall_part(window_pane_color, k, u) slab(0, S) difference() { win_open2d(u); muntins2d(u); }
    if (trim) wall_part(trim_color, k, u) {
        slab(T, SILL_R) sill2d(k, u);
        slab(0, T) thr2d(k, u);
    }
    if (k == "window") {
        wall_part(shutter_color, k, u) {
            slab(T, 1) shutters2d(k, u);
            slab(T + 1, 1) difference() { shutters2d(k, u); louvres2d(k, u); }
        }
        wall_part(flower_box_color, k, u) slab(T, FBD) box2d(k, u);
        if (has_box()) {
            wall_part(flower_color, k, u) flowers3d(u, 1, T + FBD);
            wall_part(flower_color_2, k, u) flowers3d(u, 2, T + FBD);
            wall_part(leaf_color, k, u) slab(T + FBD, 1) intersection() { box2d(k, u); leaves2d(u); }
        }
    }
}

// ------------------------------------------------------------ door leaves
// Built in storey coordinates, then moved to the plate. A leaf prints flat,
// outside face up; its hinge edge is a half-round knuckle round the pin axis.

GAP = 0.6;

function leaf_y0(half) = half == "lower" ? THR + GAP : CH + GAP;
function leaf_x1(u, a) = let (cx = dr_cx(u), w = dr_w(u))
    door_style == "french" ? (a < cx ? cx - GAP / 2 : cx + w / 2 - 1)
                           : cx + w / 2 - 1;
// leaf outline for the hinge at axis a (a left-hand leaf; mirrored for a right-hand one)
module leaf2d(u, half, a) {
    x0 = a - LT / 2; x1 = leaf_x1(u, a);
    intersection() {
        rect(x0, leaf_y0(half), x1, half == "lower" ? CH - TAB - GAP : 2 * CH);
        offset(delta = -GAP) door_open2d(u);
    }
}
function leaf_top_at(u, a) = dr_top_at(u, a, GAP);

module glazing2d(u, half, a) {
    y0 = leaf_y0(half); x0 = a - LT / 2; x1 = leaf_x1(u, a);
    h = (half == "lower" ? CH - TAB - GAP : dr_top()) - y0;
    intersection() {
        offset(delta = -10) leaf2d(u, half, a);
        if (half == "lower") rect(-1, y0 + 0.45 * h, 1000, 1000);
    }
}
module glazing_bars2d(u, half, a) {
    x0 = a - LT / 2; x1 = leaf_x1(u, a);
    intersection() {
        glazing2d(u, half, a);
        union() {
            translate([(x0 + x1) / 2 - 1.2, -1]) square([2.4, 1000]);
            for (y = [0 : 45 : 2 * CH]) translate([-1, y - 1.2]) square([1000, 2.4]);
        }
    }
}
module panels2d(u, half, a) {
    y0 = leaf_y0(half); x0 = a - LT / 2; x1 = leaf_x1(u, a);
    top = half == "lower" ? CH - TAB - GAP : dr_top();
    ph_ = (top - y0 - 10) / door_panels;
    intersection() {
        offset(delta = -10) leaf2d(u, half, a);
        for (i = [0 : door_panels - 1])
            translate([x0 + 12, y0 + 10 + i * ph_]) offset(r = 2, $fn = 12) offset(delta = -2) square([x1 - x0 - 22, ph_ - 10]);
    }
}
function knob_x(u, a) = leaf_x1(u, a) - 6;

module leaf_holes(u, half, a) {
    if (half == "lower") translate([a, CH - TAB - GAP - 6, LT / 2]) teardrop_y(HH / 2, 6 + EPS);
    else {
        translate([a, leaf_y0(half) - EPS, LT / 2]) teardrop_y(HH / 2, 6 + EPS);
        translate([a, leaf_top_at(u, a) - 6, LT / 2]) teardrop_y(HH / 2, 7);
    }
}

module leaf_part(col, u, half, a) { color(col) difference() { children(); leaf_holes(u, half, a); } }

module one_leaf(u, half, a) {
    french = door_style == "french";
    glazed = french;
    knob = half == "lower";
    kx = knob_x(u, a); ky = knob_y();
    leaf_part(door_color, u, half, a) {
        difference() {
            union() {
                slab(0, LT) difference() { leaf2d(u, half, a); rect(-1, -1, a, 1000); }
                intersection() {
                    translate([a, 0, LT / 2]) rotate([-90, 0, 0]) cylinder(r = LT / 2, h = 2 * CH, $fn = 24);
                    slab(0, LT) leaf2d(u, half, a);
                }
            }
            if (glazed) translate([0, 0, -1]) linear_extrude(LT + 2) glazing2d(u, half, a);
            if (knob) translate([kx, ky, -1]) cylinder(r = 4, h = S + 1);
        }
        if (glazed) slab(0, LT) glazing_bars2d(u, half, a);
        if (!french) slab(LT, 1) panels2d(u, half, a);
        // bottom pin of the lower leaf drops into the threshold
        if (half == "lower") translate([a, leaf_y0(half) + EPS, LT / 2]) rotate([90, 0, 0]) cylinder(d = HP, h = GAP + 3, $fn = 8);
    }
    if (glazed) leaf_part(window_pane_color, u, half, a) slab(0, S) difference() { glazing2d(u, half, a); glazing_bars2d(u, half, a); }
    if (knob) leaf_part(door_knob_color, u, half, a) {
        translate([kx, ky, 0]) cylinder(r = 4, h = S);
        translate([kx, ky, LT]) linear_extrude(3.5, scale = 0.5) circle(r = 4, $fn = 24);
    }
}

// Plate: the left-hand leaf at the origin, a french pair's right-hand leaf
// mirrored beside it (same face up).
module door_leaves(u, half) {
    axes = dr_axes(u);
    y0 = leaf_y0(half) - (half == "lower" ? GAP + 3 : 0);
    a = axes[0];
    translate([-(a - LT / 2), -y0, 0]) one_leaf(u, half, a);
    if (len(axes) > 1) {
        wl_ = leaf_x1(u, a) - (a - LT / 2);
        translate([2 * wl_ + 8, 0, 0]) mirror([1, 0, 0]) translate([-(a - LT / 2), -y0, 0]) one_leaf(u, half, a);
    }
}

// ------------------------------------------------------------ corner post
// Stands upright. Outer corner at the origin, arms along +x and +y, outside
// faces at y = 0 and x = 0 (exterior texture on the -y and -x sides), inside
// faces at y = T and x = T. Arms end half a module (less the joint gap) from
// the outer corner, so the walls on either side keep the grid.

CA = M / 2 - G;          // arm length
CJ = M / 2 - G / 2;      // joint centre

module corner_holes() {
    for (z = [CH / 4, 3 * CH / 4]) {
        // x-arm pocket, opening on its inside face y = T
        translate([CJ, T + EPS, z]) rotate([90, 0, 0]) linear_extrude(KD + EPS) key2d(C);
        // y-arm pocket, opening on x = T
        translate([T + EPS, CJ, z]) rotate([90, 0, 90]) mirror([0, 0, 1]) linear_extrude(KD + EPS) key2d(C);
    }
    for (p = [[M / 4 - G / 2, T / 2], [T / 2, M / 4 - G / 2]], z = [-EPS, CH - PD])
        translate([p[0], p[1], z]) cylinder(d = PH, h = PD + EPS, $fn = 24);
}

module corner_part(col) { color(col) difference() { children(); corner_holes(); } }

module corner_plan() { rect(0, 0, CA, T); rect(0, 0, T, CA); }
module corner_skin_in() { rect(T - S, T - S, CA, T); rect(T - S, T, T, CA); }
module corner_skin_out() { rect(0, 0, CA, S); rect(0, S, S, CA); }

function zband(b) = [b[0], b[1] - b[0]];

module corner_piece() {
    k = "wall";
    body = exterior == "plain" ? wall_color : exterior_color;
    mortar = exterior == "brick" || exterior == "stone";
    rows = exterior == "stone" ? 2.2 * texture_size : texture_size;
    yoff = crs(k) == "upper" ? CH : 0;
    corner_part(body) difference() {
        linear_extrude(CH) corner_plan();
        translate([0, 0, -1]) linear_extrude(CH + 2) corner_skin_in();
        if (mortar) for (j = [0 : ceil(CH / rows) + 1]) let (z = j * rows - ph(yoff, rows) - 0.6)
            translate([0, 0, z]) linear_extrude(1.2) corner_skin_out();
    }
    if (mortar) corner_part(mortar_color) intersection() {
        linear_extrude(CH) corner_skin_out();
        for (j = [0 : ceil(CH / rows) + 1]) let (z = j * rows - ph(yoff, rows) - 0.6)
            translate([0, 0, z]) linear_extrude(1.2) corner_skin_out();
    }
    if (exterior == "siding") corner_part(exterior_color) {
        sb = 1.6 * texture_size;
        // boards on both outside faces, thick edge down
        for (j = [-1 : ceil(CH / sb) + 1]) let (z = j * sb - ph(yoff, sb)) intersection() {
            translate([-REL - 1, -REL - 1, 0]) cube([CA + 5, CA + 5, CH]);
            union() {
                hull() {
                    translate([0, -REL, z]) cube([CA, REL, EPS]);
                    translate([0, -0.4, z + sb - EPS]) cube([CA, 0.4, EPS]);
                }
                hull() {
                    translate([-REL, 0, z]) cube([REL, CA, EPS]);
                    translate([-0.4, 0, z + sb - EPS]) cube([0.4, CA, EPS]);
                }
            }
        }
    }
    // inside face: baseboard, wainscot, rail, crown, background
    bands = [[band_base(k), baseboard_color], [band_wain(k), wainscot_color],
             [band_rail(k), baseboard_color], [band_crown(k), crown_color],
             [band_paper(k), wall_inside_color]];
    for (b = bands) if (b[0][1] > b[0][0])
        corner_part(b[1]) translate([0, 0, b[0][0]]) linear_extrude(b[0][1] - b[0][0]) corner_skin_in();
}

// ------------------------------------------------------------ floor and roof
// Printed top up. Tile x = 0 sits half a gap along the grid; walls stand on
// the outer band with their outside face flush with the tile edge, so the
// peg holes are T/2 in from every edge. Keys sit in pockets on the underside.

function sw_in() = max(T + 2, KA + C + 1.5);
// how wide the stairwell opening is on a tile Lx long
function sw_w(Lx) = min(Lx - sw_in(), sw_in() + stair_width + 10) - sw_in();
module stairwell2d(Lx, Ly) {
    if (stairwell) rect(sw_in(), sw_in(), sw_in() + sw_w(Lx), Ly - sw_in());
}

module tile_holes(Lx, Ly, through) {
    // keys on all four edges
    for (x = edge_key_xs(Lx), y = [-G / 2, Ly + G / 2])
        translate([x, y, -EPS]) linear_extrude(KD + EPS) rotate(90) key2d(C);
    for (y = edge_key_xs(Ly), x = [-G / 2, Lx + G / 2])
        translate([x, y, -EPS]) linear_extrude(KD + EPS) key2d(C);
    // pegs under every wall position
    ph_ = through ? FT + 2 * EPS : FT - 1.2 + EPS;
    for (x = peg_xs(Lx), y = [T / 2, Ly - T / 2]) translate([x, y, -EPS]) cylinder(d = PH, h = ph_, $fn = 24);
    for (y = peg_xs(Ly), x = [T / 2, Lx - T / 2]) translate([x, y, -EPS]) cylinder(d = PH, h = ph_, $fn = 24);
    if (through) translate([0, 0, -1]) linear_extrude(FT + 2) stairwell2d(Lx, Ly);
}

module tile_part(col, Lx, Ly, through) { color(col) difference() { children(); tile_holes(Lx, Ly, through); } }

// Patterns in grid coordinates (tile x = 0 is at +G/2).
module planks2d(Lx, Ly) {
    pw = M / planks_per_module;
    for (j = [0 : ceil(Ly / pw) + 1]) let (y = j * pw - G / 2) {
        translate([-1, y - 0.4]) square([Lx + 2, 0.8]);
        o = ((j * 3) % 4) * M / 4;
        for (k = [-1 : ceil(Lx / M) + 1]) translate([k * M + o - G / 2 - 0.4, y]) square([0.8, pw]);
    }
}

module herringbone2d(Lx, Ly) {
    wd = M / planks_per_module;
    n = ceil((Lx + Ly) / wd) + 4;
    for (a = [-n : n], b = [-n : n]) let (x = (a + 2 * b) * wd - G / 2, y = (a - 2 * b) * wd - G / 2)
        if (x > -2 * wd && x < Lx + wd && y > -4 * wd && y < Ly + wd)
            translate([x, y + wd]) square([wd, 2 * wd]);
}

module grout2d(Lx, Ly) {
    s = M / tiles_per_module;
    for (i = [0 : ceil(Lx / s) + 1]) translate([i * s - G / 2 - 0.6, -1]) square([1.2, Ly + 2]);
    for (j = [0 : ceil(Ly / s) + 1]) translate([-1, j * s - G / 2 - 0.6]) square([Lx + 2, 1.2]);
}

module checker2d(Lx, Ly) {
    s = M / tiles_per_module;
    for (i = [0 : ceil(Lx / s)], j = [0 : ceil(Ly / s)]) if ((i + j) % 2)
        translate([i * s - G / 2, j * s - G / 2]) square([s, s]);
}

module floor_accent2d(Lx, Ly) {
    intersection() {
        square([Lx, Ly]);
        if (floor_texture == "wood_planks") planks2d(Lx, Ly);
        else if (floor_texture == "herringbone") herringbone2d(Lx, Ly);
        else if (floor_texture == "tiles") grout2d(Lx, Ly);
        else if (floor_texture == "checkerboard") checker2d(Lx, Ly);
    }
}

function rug_m(Lx, Ly) = T + 12 + 0.12 * min(Lx, Ly);
// The rug needs room for its 6 mm border plus a visible centre; on a tile too
// small for that it is left off rather than drawn with a negative size.
function rug_fits(Lx, Ly) = min(Lx, Ly) - 2 * rug_m(Lx, Ly) >= 20;
module rug2d(Lx, Ly) {
    m = rug_m(Lx, Ly);
    if (!rug_fits(Lx, Ly)) {}
    else if (rug == "rectangle") rect(m, m, Lx - m, Ly - m);
    else if (rug == "oval") translate([Lx / 2, Ly / 2]) scale([(Lx - 2 * m) / 2, (Ly - 2 * m) / 2]) circle(r = 1, $fn = 96);
}
module rug_inner2d(Lx, Ly) { offset(delta = -6) rug2d(Lx, Ly); }

module floor_tile(u, v) {
    Lx = wl(u); Ly = wl(v);
    tile_part(floor_color, Lx, Ly, true) {
        slab(0, FT - S) square([Lx, Ly]);
        slab(FT - S, S) difference() { square([Lx, Ly]); floor_accent2d(Lx, Ly); rug2d(Lx, Ly); }
    }
    tile_part(floor_accent_color, Lx, Ly, true) slab(FT - S, S) difference() { floor_accent2d(Lx, Ly); rug2d(Lx, Ly); }
    tile_part(rug_border_color, Lx, Ly, true) slab(FT - S, S) difference() { rug2d(Lx, Ly); rug_inner2d(Lx, Ly); }
    tile_part(rug_color, Lx, Ly, true) slab(FT - S, S) rug_inner2d(Lx, Ly);
}

// Roof panels: same grid and joints; peg holes are blind from underneath.
module shingles2d(Lx, Ly, accent) {
    s = M / tiles_per_module;
    for (j = [0 : ceil(Ly / (0.7 * s)) + 1], i = [-1 : ceil(Lx / s) + 1]) {
        pick = ((i * 7 + j * 3) % 5) == 0;
        if (pick == accent)
            translate([i * s + (j % 2) * s / 2 - G / 2 + 0.5, j * 0.7 * s - G / 2 + 0.5])
                hull() { translate([0, s * 0.3]) square([s - 1, s * 0.4 - 1]); translate([(s - 1) / 2, s * 0.3]) scale([1, 0.6]) circle(d = s - 1, $fn = 24); }
    }
}

module roof_panel(u, v) {
    Lx = wl(u); Ly = wl(v);
    s = M / tiles_per_module;
    tile_part(roof_color, Lx, Ly, false) {
        slab(0, FT - S) square([Lx, Ly]);
        if (roof_style == "flat") slab(FT - S, S) offset(delta = -8) square([Lx, Ly]);
        if (roof_style == "shingles") slab(FT, 0.8) intersection() { square([Lx, Ly]); difference() { shingles2d(Lx, Ly, false); shingles2d(Lx, Ly, true); } }
        if (roof_style == "tiles") intersection() {
            translate([0, 0, FT]) cube([Lx, Ly, 3]);
            for (i = [0 : ceil(Lx / s) + 1]) translate([i * s - G / 2, -1, FT]) rotate([-90, 0, 0])
                scale([1, 0.55, 1]) cylinder(d = s * 0.8, h = Ly + 2, $fn = 24);
        }
    }
    tile_part(roof_accent_color, Lx, Ly, false) {
        if (roof_style == "flat") slab(FT - S, S) difference() { square([Lx, Ly]); offset(delta = -8) square([Lx, Ly]); }
        else slab(FT - S, S) square([Lx, Ly]);
        if (roof_style == "shingles") slab(FT, 0.8) intersection() { square([Lx, Ly]); shingles2d(Lx, Ly, true); }
    }
}

// ------------------------------------------------------------ stairs
// One storey (two courses and a floor) in N steps, split into two halves
// that key together through both stringers. Profile: x along the run, y up;
// the halves lie on one stringer, z across the width.

ST = 6;                         // stringer thickness
SB = 30;                        // stringer depth below the step line
TT = 4; RT = 3;                 // tread and riser thickness
SH = 2 * CH + FT;               // storey height
SN = 2 * max(3, round(SH / 68));
SR = SH / SN;
STD = min(tread_depth, 290 / (SN / 2));
SX = SN / 2 * STD;              // x of the joint between the halves
STOP = (SN - 1) * STD + RT;     // x of the far face of the top riser
SW = stair_width - 2 * ST;

module stair_outline2d() {
    polygon(concat([[0, 0]],
        [for (i = [1 : SN - 1]) each [[(i - 1) * STD, i * SR], [i * STD, i * SR]]],
        [[(SN - 1) * STD, SN * SR], [STOP, SN * SR], [STOP, 0]]));
}
module treads2d(i0, i1) { for (i = [i0 : i1]) if (i < SN) rect((i - 1) * STD, i * SR - TT, i * STD, i * SR); }
module risers2d(i0, i1) {
    for (i = [i0 : i1]) rect((i - 1) * STD, max(0, (i - 1) * SR - TT), (i - 1) * STD + RT, i < SN ? i * SR - TT : SN * SR);
}
module stringer2d(lower) {
    intersection() {
        stair_outline2d();
        polygon([[-1, -SR / STD - SB], [STOP + 1, (STOP + 1) * SR / STD - SB], [STOP + 1, 3 * SH], [-1, 3 * SH]]);
        if (lower) rect(-1, -1, SX, 3 * SH); else rect(SX, -1, STOP + 1, 3 * SH);
    }
    if (lower) rect(SX - 12, 0, SX, SN / 2 * SR);          // leg down to the floor
}

module stair_holes() {
    translate([SX, SN / 2 * SR - SB / 2, -EPS]) linear_extrude(KD + EPS) key2d(C);
    translate([SX, SN / 2 * SR - SB / 2, stair_width - KD]) linear_extrude(KD + EPS) key2d(C);
}

module stair_half(lower) {
    i0 = lower ? 1 : SN / 2 + 1; i1 = lower ? SN / 2 : SN;
    translate(lower ? [0, 0, 0] : [-SX, -(SN / 2 * SR - SB), 0]) {
        color(stair_riser_color) difference() {
            union() {
                slab(0, ST) stringer2d(lower);
                slab(stair_width - ST, ST) stringer2d(lower);
                slab(ST, SW) difference() {
                    union() {
                        risers2d(i0, i1);
                        // the lower half ends in a full-width back plate down to the floor
                        if (lower) rect(SX - RT, 0, SX, SN / 2 * SR - TT);
                    }
                    treads2d(i0, i1);
                }
            }
            stair_holes();
        }
        color(stair_tread_color) slab(ST, SW) treads2d(i0, i1);
    }
}

// ------------------------------------------------------------ railing
// Flat, one wall thickness; pegs into the floor edge holes, keys at the ends.

RP = 16; RB = PD + 4; RTOP = 12;

module railing_holes(L) {
    for (x = [-G / 2, L + G / 2], y = [0.3, 0.7]) translate([x, y * railing_height, -EPS]) linear_extrude(KD + EPS) key2d(C);
    for (x = peg_xs(L)) translate([x, -EPS, T / 2]) teardrop_y(PH / 2, PD + EPS);
}

module railing(u) {
    L = wl(u); H = railing_height;
    n = max(1, round((L - 2 * RP) / 24)); sp = (L - 2 * RP) / n;
    color(railing_color) difference() {
        slab(0, T) { rect(0, 0, RP, H); rect(L - RP, 0, L, H); rect(0, 0, L, RB); rect(0, H - RTOP, L, H); }
        railing_holes(L);
    }
    color(spindle_color) slab(0, T) for (i = [0 : n - 1]) translate([RP + (i + 0.5) * sp, 0]) {
        rect(-3.5, RB, 3.5, H - RTOP);
        translate([0, (RB + H - RTOP) / 2]) circle(d = 12, $fn = 32);
    }
}

// ------------------------------------------------------------ connectors

function hinge_pin_lengths() =
    let (a = dr_axes(width_units)[0], hdr = 2 * CH - 0.4 - (leaf_top_at(width_units, a) - 6))
    [TAB + 11, hdr];

module oct_rod(d, len) {
    ap = d / 2 * cos(22.5);
    translate([0, 0, ap]) rotate([0, 90, 0]) rotate([0, 0, 22.5]) cylinder(d = d, h = len, $fn = 8);
}

module connectors() {
    n = connector_count;
    color(clip_color)
    if (connector_type == "keys") {
        cols = ceil(sqrt(n));
        for (i = [0 : n - 1]) translate([(i % cols) * (2 * KA + 4) + KA, floor(i / cols) * (KH + 4) + KH / 2, 0])
            linear_extrude(KT) key2d(0);
    } else if (connector_type == "pegs") {
        cols = max(1, floor(280 / (PL + 4)));
        for (i = [0 : n - 1]) translate([(i % cols) * (PL + 4), floor(i / cols) * (PEG + 4), 0]) oct_rod(PEG, PL);
    } else {
        per = door_style == "french" ? 2 : 1;
        lens = hinge_pin_lengths();
        cols = max(1, floor(290 / (max(lens) + 4)));
        rows = ceil(2 * n * per / cols);
        for (i = [0 : 2 * n * per - 1]) translate([floor(i / rows) * (max(lens) + 4), (i % rows) * (HP + 3), 0]) oct_rod(HP, lens[i % 2]);
    }
}

// ------------------------------------------------------------ room preview
// A back corner of a 2 x 2 module room: floor tile, two corner posts, a plain
// lower course and a window upper course on the back, a door wall with its
// leaves swung open on the left. Not a printable plate.

module room_preview() {
    u = 1.5; L = wl(u); Ly = wl(2);
    floor_tile(2, 2);
    for (c = [0, 1]) let ($crs = c ? "upper" : "lower")
        translate([0, Ly, FT + c * CH]) rotate([0, 0, -90]) corner_piece();
    translate([M / 2 + L, Ly - T, FT]) rotate([90, 0, 180]) {
        let ($crs = "lower") wall_piece("wall", u);
        let ($crs = "upper") translate([0, CH, 0]) wall_piece("window", u);
    }
    translate([T, Ly - M / 2, FT]) rotate([90, 0, -90]) {
        wall_piece("door_lower", u);
        translate([0, CH, 0]) wall_piece("door_upper", u);
        for (h = ["lower", "upper"]) for (a = dr_axes(u))
            translate([a, 0, T / 2]) rotate([0, a < dr_cx(u) ? 70 : -70, 0]) translate([-a, 0, -LT / 2])
                if (a < dr_cx(u)) one_leaf(u, h, a);
                else translate([2 * dr_cx(u), 0, 0]) mirror([1, 0, 0]) one_leaf(u, h, 2 * dr_cx(u) - a);
    }
}

// ------------------------------------------------------------ notes
// Say so whenever the piece being made differs from what was asked for.

function r1(x) = round(x * 10) / 10;
WIN_PIECE = preview == "" && piece == "wall_window";
DOOR_PIECE = preview == "" && (piece == "wall_door_lower" || piece == "wall_door_upper"
    || piece == "door_leaf_lower" || piece == "door_leaf_upper"
    || (piece == "connectors" && connector_type == "hinge_pins"));
if (WIN_PIECE) {
    u = width_units;
    // name the limit that actually bound: the wall's length, or the height the window gets
    if (win_w(u) < window_width && win_w(u) == win_wmax(u))
        echo(str("NOTE: window_width reduced to ", r1(win_w(u)), " mm to fit a ", width_units,
                 "-unit wall (it keeps room for the end keys)"));
    else if (win_w(u) < window_width && window_style == "arched")
        echo(str("NOTE: window_width reduced to ", r1(win_w(u)), " mm so the arch fits the window's ",
                 r1(min(window_height, win_room())), " mm height (",
                 window_height <= win_room() ? "raise window_height" : "lower window_sill", " for a wider one)"));
    else if (win_w(u) < window_width && window_style == "round")
        echo(str("NOTE: window_width reduced to ", r1(win_w(u)), " mm: a round window is as tall as it is wide and only ",
                 r1(win_room()), " mm fits between the sill and the top-edge pegs (lower window_sill for a bigger one)"));
    if (window_style != "round" && win_h(u) < window_height)
        echo(str("NOTE: window_height reduced to ", r1(win_h(u)), " mm so the window clears the top-edge pegs and keys"));
    if (win_sill() < window_sill)
        echo(str("NOTE: window_sill lowered to ", r1(win_sill()), " mm to leave a 30 mm tall window in the course"));
    if (shutters && window_style != "round" && !has_shutters(u))
        echo("NOTE: no room for shutters beside this window; left off");
    if (flower_box && !has_box())
        echo("NOTE: no room for a flower box under this window; left off");
}
if (DOOR_PIECE) {
    u = width_units;
    if (width_units < 1)
        echo(str("NOTE: door walls are at least 1 unit long; width_units ", width_units, " made as 1"));
    if (dr_w(u) < door_width)
        echo(str("NOTE: door_width reduced to ", r1(dr_w(u)), " mm to fit a ", du(u), "-unit wall with its frame and end keys"));
    if (dr_top() - THR < door_height)
        echo(str("NOTE: door_height reduced to ", r1(dr_top() - THR), " mm to leave a 25 mm header in two ",
                 course_height, " mm courses"));
}
if (preview == "" && piece == "floor_tile") {
    Lx = wl(width_units); Ly = wl(depth_units);
    if (rug != "none" && !rug_fits(Lx, Ly))
        echo("NOTE: this floor tile is too small for a rug; left off");
    sw = sw_w(Lx);
    if (stairwell && sw < stair_width)
        echo(str("NOTE: the stairwell is only ", r1(sw), " mm wide on this tile; the stairs are ", stair_width,
                 " mm wide (use a wider tile)"));
}

// ------------------------------------------------------------ plate

if (preview == "room") room_preview();
else if (piece == "wall") wall_piece("wall", width_units);
else if (piece == "wall_window") wall_piece("window", width_units);
else if (piece == "wall_door_lower") wall_piece("door_lower", width_units);
else if (piece == "wall_door_upper") wall_piece("door_upper", width_units);
else if (piece == "door_leaf_lower") door_leaves(width_units, "lower");
else if (piece == "door_leaf_upper") door_leaves(width_units, "upper");
else if (piece == "corner_post") corner_piece();
else if (piece == "floor_tile") floor_tile(width_units, depth_units);
else if (piece == "roof_panel") roof_panel(width_units, depth_units);
else if (piece == "stairs_lower") stair_half(true);
else if (piece == "stairs_upper") stair_half(false);
else if (piece == "railing") railing(width_units);
else if (piece == "connectors") connectors();
