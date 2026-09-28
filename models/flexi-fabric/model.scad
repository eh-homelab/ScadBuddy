// Flexi Fabric — a print-in-place sheet of interlocking links that drapes,
// folds and squishes like fabric. Printed flat, no supports.
//
// Every link is a closed loop built from straight bars at two or three
// heights ("levels"), joined by vertical posts that stand on the bed. Each
// link overlaps its neighbours in plan: one link's post stands inside the
// neighbour's loop, and where the two loops cross, one bar passes over and
// the other under, with at least `clearance` of air all round. Two loops that
// cross once over and once under are linked like chain links, so no link can
// be lifted out of the sheet, and nothing is ever fused.
//
// Bars at level 0 lie on the bed; bars above bridge between their own posts,
// so the whole sheet prints without supports on 0.2 mm layers with a 0.4 mm
// nozzle.
//
// Colours: each link takes one palette colour (each distinct colour is one
// part and one filament). The colour parameters, in source order, are the
// extruder order: palette_1 .. palette_8 are extruders 1-8, then top_color,
// then overlay_color. Parameters that are not used by the chosen colour mode
// produce no part and take no extruder.
//
// Written to the MakerWorld Parametric Model Maker customizer conventions.

/* [Fabric] */

// Outline of the whole sheet
shape = "rectangle"; // [rectangle:Rectangle, circle:Circle / ellipse, hexagon:Hexagon, heart:Heart, star:Star]

// Sheet width in mm (X). The H2C plate is 325 x 320 mm with one nozzle, 300 x 320 mm using both.
width = 120; // [40:5:300]

// Sheet height in mm (Y)
height = 120; // [40:5:300]

// Link pattern
pattern = "square_links"; // [square_links:Square links, chainmail_rings:Chainmail rings, diamond:Diamond links, hex_scales:Hex scales - 3 levels, triflex_triangles:TriFlex triangles - 3 levels]

// Size of one link across its widest point, mm. Raised automatically if too small for the pattern and clearance.
link_size = 8; // [5:0.5:15]

// Air gap between links, mm (sideways and vertically). 0.3 suits a tuned 0.4 mm nozzle.
clearance = 0.3; // [0.2:0.05:0.6]

/* [Print] */

// Layer height the heights are rounded to, mm
layer_height = 0.2; // [0.12, 0.16, 0.2, 0.24, 0.28]

// Thickness of each bar, in layers
bar_layers = 4; // [3:1:8]

// Widest bar width to use, mm (2-3 lines of a 0.4 mm nozzle). Reduced automatically, never below 0.8, when the links would otherwise be closer than the clearance.
bar_width = 1.2; // [0.8:0.1:2]

/* [Colours] */

// How links are coloured
colour_mode = "rainbow"; // [single:Single colour, checker:Checker - neighbours differ, stripes:Vertical stripes, rows:Horizontal rows, gradient_bands:Gradient bands left to right, rainbow:Rainbow arcs, random_seeded:Random - seeded, overlay_only:Base colour plus overlay]

// How many palette colours the multi-colour modes use
colour_count = 8; // [2:1:8]

// Stripe / row width, in links
stripe_width = 2; // [1:1:10]

// Seed for the random mode
seed = 7; // [0:1:9999]

// Palette colour 1 (extruder 1; the single / base colour)
palette_1 = "#E53935"; // color
// Palette colour 2 (extruder 2)
palette_2 = "#FB8C00"; // color
// Palette colour 3 (extruder 3)
palette_3 = "#FDD835"; // color
// Palette colour 4 (extruder 4)
palette_4 = "#43A047"; // color
// Palette colour 5 (extruder 5)
palette_5 = "#00ACC1"; // color
// Palette colour 6 (extruder 6)
palette_6 = "#1E88E5"; // color
// Palette colour 7 (extruder 7)
palette_7 = "#8E24AA"; // color
// Palette colour 8 (extruder 8)
palette_8 = "#EC407A"; // color

// Two-tone links: the top layers of every bar and post take top_color
two_tone = false;

// Layers of top colour (also the depth of the exact-outline overlay)
top_layers = 2; // [1:1:4]

// Colour of the top layers (extruder after the palette)
top_color = "#FFFFFF"; // color

/* [Overlay] */

// Picture to lay across the sheet: upload an SVG or PNG (or name a file in this model's directory, e.g. sample-overlay.svg). Empty = off.
overlay_file = ""; // file:svg,png

// File type: auto picks by extension; an SVG outline, or a PNG cut at a brightness threshold
overlay_type = "auto"; // [auto:Auto - by file extension, svg:SVG outline, png_threshold:Image threshold - PNG]

// How the picture is applied
overlay_detail = "links"; // [links:Recolour whole links - link-sized pixels, inlay:Exact outline in the top layers]

// Overlay colour (last extruder)
overlay_color = "#212121"; // color

// Picture width as a percentage of the sheet width
overlay_scale = 80; // [10:5:300]

// Move the picture right, mm
overlay_x = 0; // [-150:1:150]

// Move the picture up, mm
overlay_y = 0; // [-150:1:150]

// Rotate the picture, degrees
overlay_rotation = 0; // [-180:5:180]

// Images: pixels darker than this brightness (percent) form the picture
image_threshold = 50; // [1:1:99]

// Swap picture and background
overlay_invert = false;

/* [Hidden] */

// Size of the marker dot used to decide which links an overlay covers.
DOT = 0.02;
// Growth of a link's footprint when used as an overlay mask; must stay far
// below half the clearance so a mask never reaches a neighbour of its class.
MASK_GROW = 0.04;

PALETTE = [palette_1, palette_2, palette_3, palette_4,
           palette_5, palette_6, palette_7, palette_8];

// ===========================================================================
// Geometry helpers
// ===========================================================================

function unit(v) = v / norm(v);
function lnormal(d) = [-d[1], d[0]];                 // inward for CCW outlines
function rot2(p, a) = [p[0] * cos(a) - p[1] * sin(a), p[0] * sin(a) + p[1] * cos(a)];
function dir(a) = [cos(a), sin(a)];

// Mitred inward offset of a CCW outline by w.
function inner_pts(V, w) = let (n = len(V))
    [for (i = [0 : n - 1])
        let (p = V[(i - 1 + n) % n], c = V[i], q = V[(i + 1) % n],
             n1 = lnormal(unit(c - p)), n2 = lnormal(unit(q - c)))
        c + (n1 + n2) * (w / (1 + n1 * n2))];

// Drop consecutive duplicate points (a post corner collapses to a triangle).
function dedupe(P) = let (n = len(P))
    [for (i = [0 : n - 1]) if (norm(P[i] - P[(i + 1) % n]) > 1e-6) P[i]];

// Outline nodes for a convex polygon whose corners are all posts:
// [point, kind] where kind is the level (0, 1, 2) of the segment that starts
// at the point, or -1 for a post segment. Each corner gets a post running
// `pl` along both edges.
function poly_nodes(C, levels, pl) = let (n = len(C))
    [for (k = [0 : n - 1])
        let (a = C[k], b = C[(k + 1) % n], d = unit(b - a))
        each [[a, -1], [a + d * pl, levels[k]], [b - d * pl, -1]]];

// Level of the nearest bar before / after segment i (skipping posts).
function bar_before(K, i, s = 1) = let (n = len(K), k = K[(i - s + 4 * n) % n])
    k >= 0 ? k : bar_before(K, i, s + 1);
function bar_after(K, i, s = 1) = let (n = len(K), k = K[(i + s) % n])
    k >= 0 ? k : bar_after(K, i, s + 1);

// A link as a list of convex prisms [polygon, z0, z1]: one per outline
// segment, the band between the outline and its inward offset. Bars sit at
// their level; posts run from the bed to the top of the higher bar they join.
function ring_pieces(nodes, w, hb, vg) =
    let (V = [for (n = nodes) n[0]], K = [for (n = nodes) n[1]],
         U = inner_pts(V, w), N = len(V), step = hb + vg)
    [for (i = [0 : N - 1])
        let (j = (i + 1) % N, k = K[i],
             poly = dedupe([V[i], V[j], U[j], U[i]]),
             top = k >= 0 ? k : max(bar_before(K, i), bar_after(K, i)))
        [poly, k >= 0 ? k * step : 0, top * step + hb]];

// ===========================================================================
// Pattern registry
//
// A pattern is one function returning a spec (see S_* below) for a link size
// L and bar width w. To add one: write a `<name>_spec(L, w)` block like the
// ones below, add one line to pattern_spec(), and one entry to the `pattern`
// dropdown. Everything else (lattice filling, colours, overlay, parts) is
// generic.
// ===========================================================================

S_LEVELS   = 0;   // number of bar levels (2 or 3)
S_GAP      = 1;   // smallest sideways gap between neighbouring links, mm
S_A        = 2;   // lattice vector A, mm
S_B        = 3;   // lattice vector B, mm
S_VARIANTS = 4;   // [[offset, nodes], ...]: link shapes per lattice cell
S_NEIGH    = 5;   // per variant: [[di, dj, variant], ...] links it interlocks with
S_CLASS    = 6;   // [K, ci, cj, cv]: class = (ci*i + cj*j + cv*v) mod K; classes
                  // never overlap in plan, so neighbours always differ

function pattern_spec(p, L, w) =
    p == "square_links"      ? square_spec(L, w)
  : p == "chainmail_rings"   ? rings_spec(L, w)
  : p == "diamond"           ? diamond_spec(L, w)
  : p == "hex_scales"        ? hex_spec(L, w)
  : p == "triflex_triangles" ? triflex_spec(L, w)
  : square_spec(L, w);

FOUR_NEIGH = [[[1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0]]];
SIX_NEIGH  = [[[1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0], [-1, 1, 0], [1, -1, 0]]];

// --- square_links ----------------------------------------------------------
// Square loops on a checkerboard; diagonal neighbours overlap at the corners.
// Sides along X lie on the bed, sides along Y bridge over them. The corner
// post stands inside the diagonal neighbour's loop. With half-size s the
// pitch p = (4s - 2w)/3 makes the post's slack in the neighbour's loop equal
// the gap to the next link along the row: both are 2(s - 2w)/3.
function square_spec(L, w) =
    let (s = L / 2, p = (4 * s - 2 * w) / 3)
    [2, 2 * (s - 2 * w) / 3, [p, p], [p, -p],
     [[[0, 0], poly_nodes([[s, -s], [s, s], [-s, s], [-s, -s]], [1, 0, 1, 0], w)]],
     FOUR_NEIGH, [2, 1, 1, 0]];

// --- diamond ---------------------------------------------------------------
// The square weave turned 45 degrees: diamonds on a plain row/column grid,
// linked left/right/up/down, so the sheet folds along its diagonals.
function diamond_spec(L, w) =
    let (s = L / 2, h = s / sqrt(2), p = sqrt(2) * (4 * h - 2 * w) / 3)
    [2, 2 * (h - 2 * w) / 3, [p, 0], [0, p],
     [[[0, 0], poly_nodes([[s, 0], [0, s], [-s, 0], [0, -s]], [1, 0, 1, 0], w)]],
     FOUR_NEIGH, [2, 1, 1, 0]];

// --- chainmail_rings -------------------------------------------------------
// Round rings on a checkerboard, posts at 45 degrees. Top and bottom arcs on
// the bed, left and right arcs bridged. Pitch and gap are linear fits of the
// balanced optimum measured with the prototype (see README).
RINGS_SEG = 6;    // arc segments per quarter
function rings_spec(L, w) =
    let (r = L / 2, pl = 0.5 * w, pd = pl / r * 180 / PI,
         p = RINGS_FIT[0] * L + RINGS_FIT[1] * w,
         nodes = [for (q = [0 : 3])
             let (b = 45 + 90 * q, kind = (q % 2 == 0) ? 0 : 1)
             each concat([[r * dir(b - pd), -1], [r * dir(b), -1]],
                 [for (t = [0 : RINGS_SEG - 1])
                     [r * dir(b + pd + t * (90 - 2 * pd) / RINGS_SEG), kind]])])
    [2, RINGS_FIT[2] * L + RINGS_FIT[3] * w, [p, p], [p, -p],
     [[[0, 0], nodes]], FOUR_NEIGH, [2, 1, 1, 0]];

// --- hex_scales ------------------------------------------------------------
// Hexagons on a triangular lattice; each vertex post stands in the next
// hexagon's loop. Six neighbours need three bar levels: opposite-leaning
// edges e0/e2/e4 take levels 0/1/2 and e1/e3/e5 take 2/0/1, which makes every
// crossing pair differ and every neighbour pair cross once over, once under.
function hex_spec(L, w) =
    let (R = L / 2, d = HEX_FIT[0] * L + HEX_FIT[1] * w)
    [3, HEX_FIT[2] * L + HEX_FIT[3] * w, [d, 0], [d / 2, d * sqrt(3) / 2],
     [[[0, 0], poly_nodes([for (k = [0 : 5]) R * dir(60 * k)], [0, 2, 1, 0, 2, 1], 0.6 * w)]],
     SIX_NEIGH, [3, 1, -1, 0]];

// --- triflex_triangles -----------------------------------------------------
// Up and down triangles on a honeycomb: every up triangle's corner post
// stands in the corner of the down triangle it points at, and vice versa.
// Edges take levels 0/1/2 so every corner joins two different levels.
function triflex_spec(L, w) =
    let (R = L / 2, d = TRI_FIT[0] * L + TRI_FIT[1] * w + TRI_PITCH_C,
         up = poly_nodes([R * dir(90), R * dir(210), R * dir(330)], [0, 1, 2], 1.8 * w),
         down = [for (n = up) [-n[0], n[1]]])
    [3, TRI_FIT[2] * L + TRI_FIT[3] * w,
     [d * sqrt(3) / 2, 1.5 * d], [-d * sqrt(3) / 2, 1.5 * d],
     [[[0, 0], up], [[0, d], down]],
     [[[0, 0, 1], [-1, 0, 1], [0, -1, 1]], [[0, 0, 0], [1, 0, 0], [0, 1, 0]]],
     [2, 0, 0, 1]];

// Linear fits [pitch_L, pitch_w, gap_L, gap_w] for the patterns without a
// closed form (measured, see README).
RINGS_FIT = [0.58635, -0.59944, 0.16984, -1.19855 - 0.03];
HEX_FIT   = [0.74897, -0.85296, 0.21617, -1.27275 - 0.03];
TRI_FIT   = [0.616, -1.12, 0.1953, -2.0 - 0.03];
TRI_PITCH_C = -0.064;

// ===========================================================================
// Resolve the design: bar width, link size, heights
// ===========================================================================

W_MIN = 0.8;      // two lines of a 0.4 mm nozzle

// Widest bar in [0.8, bar_width] (0.1 steps) whose links keep `clearance`.
function fit_w(p, L, w) =
    (w <= W_MIN + 1e-9 || pattern_spec(p, L, w)[S_GAP] >= clearance) ? w
    : fit_w(p, L, max(W_MIN, round((w - 0.1) * 10) / 10));
// Smallest link size >= L (0.5 steps) that works at the minimum bar width.
function fit_L(p, L) =
    (L >= 40 || pattern_spec(p, L, W_MIN)[S_GAP] >= clearance) ? L : fit_L(p, L + 0.5);

// Keep at least four links across the narrower side of the sheet.
LINK = fit_L(pattern, min(link_size, min(width, height) / 4));
BAR_W = fit_w(pattern, LINK, max(W_MIN, bar_width));
SPEC = pattern_spec(pattern, LINK, BAR_W);

HB = bar_layers * layer_height;                                   // bar thickness
VG = max(2, ceil(clearance / layer_height - 1e-6) + 1) * layer_height; // air between levels
LEVELS = SPEC[S_LEVELS];
TOTAL_H = (LEVELS - 1) * (HB + VG) + HB;
TOP_T = min(top_layers, bar_layers - 1) * layer_height;

VARIANTS = SPEC[S_VARIANTS];
PIECES = [for (v = VARIANTS) ring_pieces(v[1], BAR_W, HB, VG)];
OUTLINES = [for (v = VARIANTS) [for (n = v[1]) n[0]]];

// ===========================================================================
// Sheet outline and the lattice cells inside it
// ===========================================================================

function fit_box(P, W, H) =
    let (xs = [for (p = P) p[0]], ys = [for (p = P) p[1]],
         x0 = min(xs), x1 = max(xs), y0 = min(ys), y1 = max(ys))
    [for (p = P) [(p[0] - (x0 + x1) / 2) * W / (x1 - x0), (p[1] - (y0 + y1) / 2) * H / (y1 - y0)]];

function heart_pts(n = 72) = [for (i = [0 : n - 1]) let (t = 360 * i / n)
    [16 * pow(sin(t), 3), 13 * cos(t) - 5 * cos(2 * t) - 2 * cos(3 * t) - cos(4 * t)]];
function star_pts() = [for (k = [0 : 9]) (k % 2 == 0 ? 1 : 0.48) * dir(90 + 36 * k)];

function shape_poly(s, W, H) =
      s == "circle"  ? [for (i = [0 : 89]) [W / 2 * cos(4 * i), H / 2 * sin(4 * i)]]
    : s == "hexagon" ? fit_box([for (k = [0 : 5]) dir(60 * k)], W, H)
    : s == "heart"   ? fit_box(heart_pts(), W, H)
    : s == "star"    ? fit_box(star_pts(), W, H)
    : [[-W / 2, -H / 2], [W / 2, -H / 2], [W / 2, H / 2], [-W / 2, H / 2]];

SHAPE = shape_poly(shape, width, height);

function inside(p, P) = let (n = len(P))
    len([for (i = [0 : n - 1])
        let (a = P[i], b = P[(i + 1) % n])
        if (((a[1] > p[1]) != (b[1] > p[1]))
            && (p[0] < a[0] + (p[1] - a[1]) * (b[0] - a[0]) / (b[1] - a[1]))) 1]) % 2 == 1;

// Probe points per variant: the corners of the outline's bounding box. All
// four inside the sheet outline keeps the link inside it.
function bbox2(P) = let (xs = [for (p = P) p[0]], ys = [for (p = P) p[1]])
    [[min(xs), min(ys)], [max(xs), max(ys)]];
PROBES = [for (o = OUTLINES) let (b = bbox2(o))
    [b[0], [b[1][0], b[0][1]], b[1], [b[0][0], b[1][1]]]];

A = SPEC[S_A];
B = SPEC[S_B];
// Lattice index range covering the sheet: solve the corners in (A, B) space.
DET = A[0] * B[1] - A[1] * B[0];
function to_ij(p) = [(p[0] * B[1] - p[1] * B[0]) / DET, (A[0] * p[1] - A[1] * p[0]) / DET];
CORNER_IJ = [for (sx = [-1, 1], sy = [-1, 1]) to_ij([sx * width / 2, sy * height / 2])];
I0 = floor(min([for (c = CORNER_IJ) c[0]])) - 1;
I1 = ceil(max([for (c = CORNER_IJ) c[0]])) + 1;
J0 = floor(min([for (c = CORNER_IJ) c[1]])) - 1;
J1 = ceil(max([for (c = CORNER_IJ) c[1]])) + 1;

function centre(i, j, v) = i * A + j * B + VARIANTS[v][0];
function fits(i, j, v, poly) = let (c = centre(i, j, v))
    len([for (q = PROBES[v]) if (!inside(c + q, poly)) 1]) == 0;

// G[v][i - I0][j - J0]: the link is in the sheet.
function fit_grid(poly) = [for (v = [0 : len(VARIANTS) - 1])
    [for (i = [I0 : I1]) [for (j = [J0 : J1]) fits(i, j, v, poly)]]];
function at(G, i, j, v) = (i < I0 || i > I1 || j < J0 || j > J1) ? false : G[v][i - I0][j - J0];
// Drop links with no neighbour in the sheet (they would fall out).
function keep(G) = [for (v = [0 : len(VARIANTS) - 1])
    [for (i = [I0 : I1]) [for (j = [J0 : J1])
        G[v][i - I0][j - J0]
        && len([for (n = SPEC[S_NEIGH][v]) if (at(G, i + n[0], j + n[1], n[2])) 1]) > 0]]];

CLS = SPEC[S_CLASS];
function cls_of(i, j, v) = ((CLS[1] * i + CLS[2] * j + CLS[3] * v) % CLS[0] + CLS[0]) % CLS[0];

// Cell: [x, y, i, j, variant, class]
function cells_in(poly) = let (G = keep(keep(fit_grid(poly))))
    [for (v = [0 : len(VARIANTS) - 1], i = [I0 : I1], j = [J0 : J1])
        if (G[v][i - I0][j - J0]) let (c = centre(i, j, v)) [c[0], c[1], i, j, v, cls_of(i, j, v)]];

// A star or heart too small for even two links falls back to the rectangle,
// so every setting renders something.
CELLS_SHAPE = cells_in(SHAPE);
CELLS0 = len(CELLS_SHAPE) > 1 ? CELLS_SHAPE : cells_in(shape_poly("rectangle", width, height));

// ===========================================================================
// Colour modes
//
// link_colour_index(mode, cell) returns a palette index 0..7 for one link.
// To add a mode: add one line here and one entry to the `colour_mode`
// dropdown. A cell is [x, y, i, j, variant, class, index].
// ===========================================================================

NCOL = colour_count;
XS = [for (c = CELLS0) c[0]];
YS = [for (c = CELLS0) c[1]];
X0 = min(XS); X1 = max(XS); Y0 = min(YS); Y1 = max(YS);

// Column and row spacing of the lattice (smallest non-zero X / Y offsets
// between cells), so stripes are counted in links for every pattern.
STEPS = [for (i = [-2 : 2], j = [-2 : 2], v = [0 : len(VARIANTS) - 1])
    i * A + j * B + VARIANTS[v][0] - VARIANTS[0][0]];
COL_STEP = min([for (s = STEPS) if (abs(s[0]) > 1e-6) abs(s[0])]);
ROW_STEP = min([for (s = STEPS) if (abs(s[1]) > 1e-6) abs(s[1])]);

// Rainbow: concentric arcs about a point below the sheet, red outermost.
RB_C = [(X0 + X1) / 2, Y0 - 0.25 * (Y1 - Y0)];
RB_D = [for (c = CELLS0) norm([c[0], c[1]] - RB_C)];
RB_MIN = min(RB_D); RB_MAX = max(RB_D);

RAND = rands(0, NCOL, max(1, len(CELLS0)), seed);

function band(t, n) = min(n - 1, max(0, floor(t * n)));

function link_colour_index(mode, c) =
    mode == "checker"        ? c[5]
  : mode == "stripes"        ? floor((c[0] - X0) / (stripe_width * COL_STEP) + 1e-6) % NCOL
  : mode == "rows"           ? floor((c[1] - Y0) / (stripe_width * ROW_STEP) + 1e-6) % NCOL
  : mode == "gradient_bands" ? band((c[0] - X0) / max(1e-6, X1 - X0 + COL_STEP), NCOL)
  : mode == "rainbow"        ? NCOL - 1 - band((norm([c[0], c[1]] - RB_C) - RB_MIN)
                                               / max(1e-6, RB_MAX - RB_MIN + 1e-3), NCOL)
  : mode == "random_seeded"  ? min(NCOL - 1, floor(RAND[c[6]]))
  : 0;   // single, overlay_only

// Cell: [x, y, i, j, variant, class, index, colour]
CELLS = [for (k = [0 : len(CELLS0) - 1])
    let (c = concat(CELLS0[k], [k])) concat(c, [link_colour_index(colour_mode, c)])];

// ===========================================================================
// Overlay mask
// ===========================================================================

// overlay_file reaches import() and surface() verbatim, and OpenSCAD reads any
// path it is given: only a bare file name in the model's own directory is
// accepted. A path, a leading dot or a backslash turns the overlay off.
function safe_file(f) =
    f != "" && len(search("/", f)) == 0 && len(search("\\", f)) == 0 && f[0] != ".";

OVERLAY_REFUSED = overlay_file != "" && !safe_file(overlay_file);
if (OVERLAY_REFUSED)
    echo(str("NOTE: overlay_file \"", overlay_file,
             "\" is not a bare file name in the model directory; overlay off"));
OVERLAY_ON = safe_file(overlay_file);

function ends_with(f, suffix) =
    len(f) >= len(suffix)
    && [for (i = [0 : len(suffix) - 1]) f[len(f) - len(suffix) + i]] == [for (c = suffix) c];
function lower(s) = s == "" ? "" : chr([for (c = s) let(o = ord(c)) o >= 65 && o <= 90 ? o + 32 : o]);
// "auto" reads the extension (any case), so an uploaded PNG needs no second setting.
// "png_threshold" was "image_threshold" until #318 renamed it (it shared a name
// with the numeric image_threshold parameter); saved presets and past outputs
// still hold the old value, so both force the image reader.
OVERLAY_IS_IMAGE = overlay_type == "png_threshold" || overlay_type == "image_threshold"
    || (overlay_type == "auto" && ends_with(lower(overlay_file), ".png"));
PIXEL = OVERLAY_ON && overlay_detail == "links";
FINE = OVERLAY_ON && overlay_detail == "inlay";

// The picture in 2D. An SVG is imported as-is; an image goes through
// surface(), whose height is the pixel brightness (0-100), and the pixels
// darker than image_threshold are kept. A missing file only warns and gives
// an empty mask, so the sheet still renders without the picture.
module overlay_source() {
    if (OVERLAY_IS_IMAGE)
        difference() {
            projection() surface(file = overlay_file, center = true);
            projection(cut = true)
                translate([0, 0, -image_threshold]) surface(file = overlay_file, center = true);
        }
    else
        import(overlay_file, center = true);
}

module overlay_2d() {
    translate([overlay_x, overlay_y]) rotate(overlay_rotation)
        resize([width * overlay_scale / 100, 0], auto = true) overlay_source();
}

module mask_2d() {
    if (overlay_invert)
        difference() { square([3 * width, 3 * height], center = true); overlay_2d(); }
    else
        overlay_2d();
}

// Whole-link mode. OpenSCAD cannot ask "is this point in the picture?", so it
// is answered with geometry: a tiny dot at every link centre is intersected
// with the picture, any dot that is hit is restored to a full dot, and the
// Minkowski sum of those dots with the link's footprint is the union of the
// footprints of exactly the links that were hit. Links of one class never
// overlap in plan, so per class the mask covers whole links and nothing else.
module dots(k, v) {
    for (c = CELLS) if (c[5] == k && c[4] == v) translate([c[0], c[1]]) square(DOT, center = true);
}
module pixel_mask(k, v) {
    translate([0, 0, -1]) linear_extrude(TOTAL_H + 2)
        minkowski() {
            union() {
                intersection() {
                    dots(k, v);
                    offset(delta = DOT) intersection() { dots(k, v); mask_2d(); }
                }
                // Keeps the sum non-empty when nothing is hit; far off the sheet.
                translate([1e4, 1e4]) square(DOT, center = true);
            }
            offset(delta = MASK_GROW) polygon(OUTLINES[v]);
        }
}

module fine_mask() {
    translate([0, 0, -1]) linear_extrude(TOTAL_H + 2) mask_2d();
}

// ===========================================================================
// Links and parts
// ===========================================================================

// part: 0 = whole link, 1 = below the top layers, 2 = the top layers.
module piece(pc, part) {
    z0 = pc[1]; z1 = pc[2]; zs = max(z0, z1 - TOP_T);
    lo = part == 2 ? zs : z0;
    hi = part == 1 ? zs : z1;
    if (hi - lo > 1e-6) translate([0, 0, lo]) linear_extrude(hi - lo) polygon(pc[0]);
}

module link(v, part) {
    for (pc = PIECES[v]) piece(pc, part);
}

// Links matching a colour / class / variant (-1 = any).
module links(part, col = -1, k = -1, v = -1) {
    for (c = CELLS)
        if ((col < 0 || c[7] == col) && (k < 0 || c[5] == k) && (v < 0 || c[4] == v))
            translate([c[0], c[1], 0]) link(c[4], part);
}

CLASS_VARIANTS = [for (k = [0 : CLS[0] - 1], v = [0 : len(VARIANTS) - 1])
    if (len([for (c = CELLS) if (c[5] == k && c[4] == v) 1]) > 0) [k, v]];

BASE = two_tone ? 1 : 0;

module colour_part(col) {
    if (PIXEL)
        for (kv = CLASS_VARIANTS)
            difference() { links(BASE, col, kv[0], kv[1]); pixel_mask(kv[0], kv[1]); }
    else if (FINE && !two_tone) {
        links(1, col);
        difference() { links(2, col); fine_mask(); }
    } else
        links(BASE, col);
}

module top_part() {
    if (PIXEL)
        for (kv = CLASS_VARIANTS)
            difference() { links(2, -1, kv[0], kv[1]); pixel_mask(kv[0], kv[1]); }
    else if (FINE)
        difference() { links(2); fine_mask(); }
    else
        links(2);
}

module overlay_part() {
    if (PIXEL)
        for (kv = CLASS_VARIANTS)
            intersection() { links(0, -1, kv[0], kv[1]); pixel_mask(kv[0], kv[1]); }
    else if (FINE)
        intersection() { links(2); fine_mask(); }
}

echo(FLEXI = [pattern, len(CELLS), LINK, BAR_W, SPEC[S_GAP], LEVELS, TOTAL_H, HB, VG,
              norm(A), CLS[0], len(VARIANTS)]);
if (LINK > link_size) echo(str("WARNING: link_size raised to ", LINK, " mm so links keep ", clearance, " mm clearance"));
if (BAR_W < bar_width) echo(str("NOTE: bar width reduced to ", BAR_W, " mm to keep ", clearance, " mm clearance"));

for (col = [0 : 7])
    if (len([for (c = CELLS) if (c[7] == col) 1]) > 0)
        color(PALETTE[col]) colour_part(col);

if (two_tone) color(top_color) top_part();

if (OVERLAY_ON) color(overlay_color) overlay_part();
