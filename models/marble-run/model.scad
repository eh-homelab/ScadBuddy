// Stackable marble run — square tiles that stack into a tower. Each tile has an
// open-top channel for a standard marble; the marble drops out of one tile
// through a hole in its floor and lands at the start of the channel of the
// tile underneath. Registration pegs on top locate the next tile up.
//
// Connection standard (the same for every piece, so any two pieces connect):
//   * Four ports sit on the tile's centre lines, a distance q from the centre
//     (q = tile/2 - wall - channel radius): west, north, east, south.
//   * The marble always ENTERS at the west port, at the top.
//   * It EXITS straight down through the floor at one port (which one depends
//     on the piece). Turn the tile underneath so its west port (the start of
//     its channel) sits under that exit hole. The pegs are 4-fold symmetric,
//     so every quarter turn fits.
//   * A channel is covered by the tile stacked on top, so the marble cannot
//     jump out.
//
// Prints without supports as it lies on the plate: channels are open at the
// top, every hole is vertical, and the peg sockets underneath have 45° roofs.
//
// Written to the MakerWorld Parametric Model Maker customizer conventions so
// the same file works unchanged on MakerWorld and in ScadBuddy.
//
// The two colour parameters are the extruder order: piece_color is extruder
// 1, accent_color (the top rim and the pegs) is extruder 2.

/* [Piece] */

// Which piece to make. The marble always enters at the west port; stack the next tile turned so its west port sits under this piece's exit
type = "spiral"; // [straight_drop:Straight drop, zigzag:Zigzag run, spiral:Spiral, funnel_start:Funnel start, finish_cup:Finish cup, cross:Cross junction]

// Footprint of the square tile in mm
tile = 50; // [40:5:80]

// Height of one level in mm (raised automatically if too short for the marble)
height = 30; // [20:5:60]

// Marble diameter in mm
marble_d = 16; // [12:1:25]

// Extra room around the marble in channels and holes, in mm (added to the diameter)
channel_clearance = 2; // [1:0.5:4]

// Channel slope in degrees (reduced automatically if the level is too short)
slope = 6; // [3:1:12]

/* [Stacking] */

// Registration pegs on top and matching sockets underneath
peg = true;

// Gap between a peg and its socket, per side, in mm
peg_clearance = 0.3; // [0.1:0.05:0.6]

/* [Colours] */

// Piece body (extruder 1)
piece_color = "#29B6F6"; // color

// Top rim and pegs (extruder 2)
accent_color = "#FFEE58"; // color

/* [Hidden] */

$fn = 64;

wall = 1.6;        // thinnest wall between a channel and the outside
min_floor = 1.6;   // thinnest floor under a channel
rim_h = 2;         // height of the accent-coloured top rim
edge_r = 3;        // corner radius of the tile
chamfer = 0.8;     // chamfer on the top outer edge
peg_d = 5;
peg_h = 4;
socket_extra = 0.4; // sockets are this much deeper than the pegs are tall
bowl_dip = 3;      // finish cup: how far the bowl sits below the channel end
spiral_steps = 36;

T = tile;
rc = (marble_d + channel_clearance) / 2;            // channel radius
H = max(height, 2 * rc + min_floor + 4);            // level height used
q = T / 2 - wall - rc;                              // port offset from centre
zs = H - 2 * rc;                                    // channel floor at entry

// Corner pegs sit just outside the largest circle any channel uses.
rp = peg_d / 2;
rs = rp + peg_clearance;
pc = (T / 2 - wall + 1.5 + rp) / sqrt(2);
top_pegs = peg && type != "funnel_start";

// Finish cup bowl.
rb = rc + 1;
xb = min(q, T / 2 - wall - rb);

W = [-q, 0]; E = [q, 0]; N = [0, q]; S = [0, -q];

// Length of the marble's main path, and the drop that is available for it.
path_len =
    type == "spiral"     ? q * 1.5 * PI :
    type == "finish_cup" ? q + xb :
    2 * q;
dip = type == "finish_cup" ? bowl_dip : 0;
theta = min(slope, atan(max(0, zs - min_floor - dip) / path_len));
function floor_at(s) = zs - s * tan(theta);

spiral_pts = [for (i = [0:spiral_steps])
    let(a = 180 - 270 * i / spiral_steps) [q * cos(a), q * sin(a)]];

function cum(pts, i) = i == 0 ? 0 : cum(pts, i - 1) + norm(pts[i] - pts[i - 1]);

// ---------------------------------------------------------------- channels

// A round-bottomed column: the marble's cross-section at p with its floor at
// z, open all the way up through the top.
module column(p, z) {
    translate([p[0], p[1], z + rc]) {
        sphere(r = rc);
        cylinder(r = rc, h = H + 1 - (z + rc));
    }
}

// Open-top channel along a polyline, starting at floor z0 and falling at the
// channel slope.
module run(pts, z0) {
    for (i = [0:len(pts) - 2])
        hull() {
            column(pts[i], z0 - cum(pts, i) * tan(theta));
            column(pts[i + 1], z0 - cum(pts, i + 1) * tan(theta));
        }
}

// Vertical exit hole from floor z down through the bottom of the tile.
module shaft(p, z) {
    translate([p[0], p[1], -1]) cylinder(r = rc, h = z + rc + 1);
}

// Straight drop: a pointed-top window in the west wall shows the marble fall.
module window() {
    w = marble_d / 2;
    z0 = 4;
    z1 = H - rim_h - 3;
    if (z1 - w / 2 > z0)
        translate([-T / 2 - 1, 0, 0]) rotate([90, 0, 90])
            linear_extrude(height = T / 2 - q + 1)
                polygon([[-w / 2, z0], [w / 2, z0], [w / 2, z1 - w / 2],
                         [0, z1], [-w / 2, z1 - w / 2]]);
}

// Funnel start: a wide funnel that drains to the west port.
funnel_r = T / 2 - wall;
funnel_run = funnel_r + q - rc;
funnel_z = max(H * 0.25, H - funnel_run * tan(35));

module funnel() {
    hull() {
        translate([0, 0, H]) cylinder(r = funnel_r, h = 1);
        translate([W[0], W[1], funnel_z]) cylinder(r = rc, h = 0.01);
    }
    shaft(W, funnel_z - rc);
}

// Finish cup: channel into a bowl at the east side, with a notch in the east
// wall so the marble can be seen and picked out.
module finish() {
    ze = floor_at(q + xb);
    zb = ze - bowl_dip;
    run([W, [xb, 0]], zs);
    translate([xb, 0, zb + rb]) {
        sphere(r = rb);
        cylinder(r = rb, h = H);
    }
    translate([xb, -rc, zb + marble_d * 0.4])
        cube([T, 2 * rc, H]);
}

module cuts() {
    if (type == "straight_drop") {
        shaft(W, H);
        window();
    } else if (type == "zigzag") {
        run([W, E], zs);
        shaft(E, floor_at(2 * q));
    } else if (type == "spiral") {
        run(spiral_pts, zs);
        shaft(S, floor_at(path_len));
        // On small tiles the central post would be a sliver; take it out.
        if (q - rc < 2)
            translate([0, 0, floor_at(path_len)])
                cylinder(r = max(0, q - rc) + 0.5, h = H);
    } else if (type == "cross") {
        run([W, E], zs);
        run([N, [0, 0]], zs);
        run([S, [0, 0]], zs);
        shaft(E, floor_at(2 * q));
    } else if (type == "funnel_start") {
        funnel();
    } else if (type == "finish_cup") {
        finish();
    }
    if (peg) sockets();
}

// ---------------------------------------------------------------- stacking

module corners() {
    for (x = [-1, 1], y = [-1, 1]) translate([x * pc, y * pc, 0]) children();
}

module sockets() {
    corners() translate([0, 0, -0.01]) {
        cylinder(r = rs, h = peg_h + socket_extra + 0.01);
        translate([0, 0, peg_h + socket_extra]) cylinder(r1 = rs, r2 = 0, h = rs);
    }
}

module pegs() {
    corners() translate([0, 0, H]) {
        cylinder(r = rp, h = peg_h - 0.8);
        translate([0, 0, peg_h - 0.8]) cylinder(r1 = rp, r2 = rp - 0.8, h = 0.8);
    }
}

// ---------------------------------------------------------------- body

module outline_2d(inset = 0) {
    offset(r = edge_r - inset) square(T - 2 * edge_r, center = true);
}

module body() {
    linear_extrude(height = H - rim_h) outline_2d();
}

module rim() {
    translate([0, 0, H - rim_h]) {
        linear_extrude(height = rim_h - chamfer) outline_2d();
        hull() {
            translate([0, 0, rim_h - chamfer - 0.01])
                linear_extrude(height = 0.01) outline_2d();
            translate([0, 0, rim_h - 0.01])
                linear_extrude(height = 0.01) outline_2d(chamfer);
        }
    }
}

color(piece_color) difference() { body(); cuts(); }

color(accent_color) {
    difference() { rim(); cuts(); }
    if (top_pegs) pegs();
}
