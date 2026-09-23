/* [Global] */
// Model quality
quality = 64; // [16:16:128]

/* [Shapes] */
// How many copies
copies = 3;
// Wall thickness (mm)
wall = 1.2;
// Label
label = "hello"; // 32
// Shape family
family = "round"; // [round, square, hex]
// Grid size
grid = 2; // [1, 2, 4, 8]
// Add a lid
lid = true;
// Ramp angle
ramp = 30; // [0:5:90]

/* [Appearance] */
// Body colour
body_colour = "#00ff88"; // color
// Label font
label_font = "DejaVu Sans"; // font

/* [Hidden] */
secret = 42;

cube([copies * grid, wall, quality / 16]);
