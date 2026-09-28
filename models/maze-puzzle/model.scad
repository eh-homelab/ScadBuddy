// Ball maze — a tray with a maze generated from a seed, a round start marker
// and a star finish marker inlaid in the floor. Tip the tray to roll a ball
// bearing (or a BB, or a marble) from the circle to the star.
//
// The maze is a perfect maze (every cell reachable, exactly one route between
// any two cells) made by a randomised depth-first search run inside OpenSCAD.
// The same seed always gives the same maze; change the seed for a new one.
//
// In ball_lid mode a flat lid with a snap skirt prints alongside, upside down,
// so the ball stays in. Print the lid in a clear filament to see the maze.
// When the tray and lid do not fit one bed together, the lid goes on a second
// plate: the model echoes `plates = 2`, and draws only plate $plate when
// ScadBuddy sets it (0, the default, draws both, side by side).
//
// Written to the MakerWorld Parametric Model Maker customizer conventions so
// the same file works unchanged on MakerWorld and in ScadBuddy.
//
// Colour parameters are the extruder order: floor_color is extruder 1,
// wall_color 2, marker_color 3, lid_color 4 (ball_lid mode only).

/* [Maze] */

// Number of cells across
cells_x = 8; // [4:1:15]

// Number of cells front to back
cells_y = 8; // [4:1:15]

// Maze number: each seed gives a different maze, the same seed the same maze
seed = 42; // [0:1:9999]

// Grid pitch in mm (widened automatically if the ball would not fit)
cell_size = 10; // [6:1:16]

// Outline: square, or round (the grid clipped inside a disc)
shape = "square"; // [square:Square, round:Round]

// Height of the maze walls above the floor in mm (raised automatically with the snap-on lid so the ball clears it)
wall_height = 6; // [3:1:12]

// Thickness of the maze walls in mm
wall_thickness = 1.6; // [1.2:0.2:3]

/* [Play] */

// Open tray, or a snap-on lid that keeps the ball in
mode = "open_tray"; // [open_tray:Open tray, ball_lid:Ball with snap-on lid]

// Ball diameter in mm; corridors are widened to fit it
ball_d = 6; // [4:0.5:12]

// Inlay a circle at the start and a star at the finish
markers = true;

/* [Colours] */

// Floor (extruder 1)
floor_color = "#80DEEA"; // color

// Maze walls and border (extruder 2)
wall_color = "#006064"; // color

// Start and finish markers (extruder 3)
marker_color = "#FFCA28"; // color

// Lid, ball_lid mode only (extruder 4)
lid_color = "#FFFFFF"; // color

/* [Hidden] */

$fn = 48;

// ScadBuddy's plate convention: 0 draws every plate, N only plate N.
$plate = 0;

floor_t = 2;          // floor thickness
border_extra = 1.2;   // outer border is this much thicker than a maze wall
ball_clear = 1.0;     // corridor width beyond the ball
lid_head = 0.5;       // headroom over the ball under the lid
inlay = 0.6;          // marker inlay depth
lid_t = 1.6;          // lid plate thickness
skirt_t = 1.6;        // lid skirt wall thickness
lid_fit = 0.25;       // gap between the tray border and the skirt
groove_d = 0.5;       // snap groove depth in the tray border
bead_d = 0.4;         // snap bead on the skirt (0.15 mm interference)
snap_hh = 0.6;        // half height of the groove and the bead
part_gap = 8;         // gap between tray and lid on the plate
disc_fn = 96;

// ------------------------------------------------------------ dimensions

W = cells_x;
H = cells_y;
t = wall_thickness;
min_pitch = ball_d + ball_clear + t;
p = max(cell_size, min_pitch);                       // effective pitch
c = p - t;                                           // corridor width
wh = mode == "ball_lid" ? max(wall_height, ball_d + lid_head) : wall_height;
B = t / 2 + border_extra;                            // grid line to outside
rc = B + t / 2;                                      // outer corner radius
cx = W * p / 2;
cy = H * p / 2;
R_in = min(W, H) * p / 2 + p / 4;                    // round: cell limit
R_out = R_in + B;                                    // round: outside
z_top = floor_t + wh;
skirt_h = min(4, wh - 0.4);

if (p > cell_size)
    echo(str("NOTE: cell_size widened to ", p, " mm so a ", ball_d,
             " mm ball fits the corridors"));
if (wh > wall_height)
    echo(str("NOTE: wall_height raised to ", wh, " mm so the ball clears the lid"));

// ------------------------------------------------------------ maze

// Round mode keeps a cell when its whole corridor square is inside the disc,
// at least half a wall thickness in from the edge.
function far(i, n, ctr) = max(abs(i * p + t / 2 - ctr), abs((i + 1) * p - t / 2 - ctr));
function inside(x, y) = shape != "round"
    || norm([far(x, W, cx), far(y, H, cy)]) <= R_in - t / 2;
A = [for (k = [0 : W * H - 1]) inside(k % W, floor(k / W)) ? 1 : 0];
active = [for (k = [0 : W * H - 1]) if (A[k] == 1) k];
start = active[0];
finish = active[len(active) - 1];

// Park-Miller minimal standard generator: exact in doubles, and the same on
// every OpenSCAD build (rands() is not guaranteed to be).
M = 2147483647;
function lcg(s) = (s * 16807) % M;
function mix(s, n) = n == 0 ? s : mix(lcg(s), n - 1);
s0 = mix((seed * 7919 + 104729) % (M - 1) + 1, 4);

function set(l, k, v) = [for (i = [0 : len(l) - 1]) i == k ? v : l[i]];
function pop(l) = len(l) <= 1 ? [] : [for (i = [0 : len(l) - 2]) l[i]];

function unvisited(k, vis) = let(x = k % W, y = floor(k / W)) [
    if (x + 1 < W && A[k + 1] == 1 && vis[k + 1] == 0) k + 1,
    if (x > 0     && A[k - 1] == 1 && vis[k - 1] == 0) k - 1,
    if (y + 1 < H && A[k + W] == 1 && vis[k + W] == 0) k + W,
    if (y > 0     && A[k - W] == 1 && vis[k - W] == 0) k - W
];

// E[k] = 1: open between k and k+1.  N[k] = 1: open between k and k+W.
function carve(E, N, a, b) =
      b == a + 1 ? [set(E, a, 1), N]
    : b == a - 1 ? [set(E, b, 1), N]
    : b == a + W ? [E, set(N, a, 1)]
    :              [E, set(N, b, 1)];

// Recursive backtracker with an explicit stack. Every call is a tail call, so
// OpenSCAD runs it as a loop: 2 x cells iterations, no recursion depth.
function dfs(stack, vis, E, N, s) =
    len(stack) == 0 ? [E, N]
    : let(k = stack[len(stack) - 1], nb = unvisited(k, vis))
      len(nb) == 0 ? dfs(pop(stack), vis, E, N, s)
    : let(s2 = lcg(s), n = nb[floor(s2 / M * len(nb))], en = carve(E, N, k, n))
      dfs(concat(stack, [n]), set(vis, n, 1), en[0], en[1], s2);

zeros = [for (k = [0 : W * H - 1]) 0];
maze = dfs([start], set(zeros, start, 1), zeros, zeros, s0);
E = maze[0];
N = maze[1];

// verify.sh reads this line to check the maze is perfect.
echo("MAZE", W, H, A, E, N, start, finish);

// ------------------------------------------------------------ 2D

function cell_origin(k) = [(k % W) * p + t / 2, floor(k / W) * p + t / 2];
function cell_centre(k) = cell_origin(k) + [c / 2, c / 2];

module corridors_2d() {
    for (k = active) translate(cell_origin(k)) square(c);
    // Openings: a wall-thick bridge overlapping both cells by a hair.
    for (k = active) {
        if (E[k] == 1) translate(cell_origin(k) + [c - 0.01, 0]) square([t + 0.02, c]);
        if (N[k] == 1) translate(cell_origin(k) + [0, c - 0.01]) square([c, t + 0.02]);
    }
}

// Outline of the tray. Square: corner arcs centred on the corner cells'
// inner corners, so the border is never thinner than B + t/2.
module outline_2d() {
    if (shape == "round")
        translate([cx, cy]) circle(r = R_out, $fn = disc_fn);
    else
        hull() for (x = [t / 2, W * p - t / 2], y = [t / 2, H * p - t / 2])
            translate([x, y]) circle(r = rc, $fn = 64);
}

module star_2d(r) {
    offset(r = r * 0.12) offset(delta = -r * 0.12)
        polygon([for (i = [0 : 9]) let(a = 90 + i * 36, rr = i % 2 == 0 ? r : r * 0.45)
                 [rr * cos(a), rr * sin(a)]]);
}

module markers_2d() {
    translate(cell_centre(start)) circle(d = c * 0.7);
    translate(cell_centre(finish)) star_2d(c * 0.45);
}

// ------------------------------------------------------------ snap band

module slice(z) { translate([0, 0, z - 0.005]) linear_extrude(0.01) children(); }

// The solid between the prism of the outline (grown by `grow`) and a V-shaped
// waist `d` deep, centred at zc and 2*hh tall: subtract it from the tray
// border for a groove, add it inside the skirt for a bead.
module vband(zc, hh, d, grow) {
    difference() {
        translate([0, 0, zc - hh]) linear_extrude(2 * hh) offset(r = grow) children();
        hull() { slice(zc - hh - 0.005) children(); slice(zc) offset(delta = -d) children(); }
        hull() { slice(zc) offset(delta = -d) children(); slice(zc + hh + 0.005) children(); }
    }
}

// ------------------------------------------------------------ parts

if (on_plate(1)) {
    color(floor_color)
        difference() {
            linear_extrude(floor_t) outline_2d();
            if (markers) translate([0, 0, floor_t - inlay]) linear_extrude(inlay + 1) markers_2d();
        }

    if (markers)
        color(marker_color)
            translate([0, 0, floor_t - inlay]) linear_extrude(inlay) markers_2d();

    color(wall_color)
        difference() {
            translate([0, 0, floor_t]) linear_extrude(wh)
                difference() { outline_2d(); corridors_2d(); }
            if (mode == "ball_lid")
                vband(z_top - skirt_h / 2, snap_hh, groove_d, 1) outline_2d();
        }
}

// The lid prints upside down beside the tray (plate on the bed, skirt up):
// to the right if the pair fits the bed's 300 mm width with both nozzles,
// else behind it within the 320 mm depth, else on a plate of its own.
BED = [300, 320];
lid_grow = lid_fit + skirt_t;
o_min = shape == "round" ? [cx - R_out, cy - R_out] : [-B, -B];
o_max = shape == "round" ? [cx + R_out, cy + R_out] : [W * p + B, H * p + B];
span = o_max - o_min;
pair = [for (i = [0, 1]) 2 * span[i] + part_gap + 2 * lid_grow];
beside = [span[0] + lid_grow + part_gap, 0];
lid_at = pair[0] <= BED[0] && span[1] + 2 * lid_grow <= BED[1] ? beside
       : pair[1] <= BED[1] && span[0] + 2 * lid_grow <= BED[0] ? [0, span[1] + lid_grow + part_gap]
       : undef;
lid_mode = mode == "ball_lid";
lid_plate = lid_mode && lid_at == undef ? 2 : 1;
lid_xy = lid_at == undef ? beside : lid_at;

// ScadBuddy reads this line and renders each plate with $plate = 1 .. plates.
echo(plates = lid_plate);
function on_plate(n) = $plate == 0 || $plate == n;

if (lid_plate == 2)
    echo(str("NOTE: the tray and lid (", pair[0], " mm side by side) do not fit one bed ",
             "together; the lid is on plate 2"));

// On a plate of its own the lid sits where the tray would; drawn with
// everything ($plate = 0) it stays beside the tray, past the edge of the bed.
if (lid_mode && on_plate(lid_plate))
    color(lid_color)
        translate($plate == 2 ? [0, 0, 0] : [lid_xy[0], lid_xy[1], 0]) {
            linear_extrude(lid_t) offset(r = lid_fit + skirt_t) outline_2d();
            translate([0, 0, lid_t - 0.01]) linear_extrude(skirt_h + 0.01)
                difference() {
                    offset(r = lid_fit + skirt_t) outline_2d();
                    offset(r = lid_fit) outline_2d();
                }
            vband(lid_t + skirt_h / 2, snap_hh, bead_d, 0.01) offset(r = lid_fit) outline_2d();
        }
