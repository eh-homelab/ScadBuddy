// A colour drawn by one rotate_extrude that touches the axis: OpenSCAD keeps it
// as a PolySet whose axis triangles are degenerate, and its 3MF export fails (#952).
color("#E53935") rotate_extrude($fn = 24) polygon([[0, 0], [3, 0], [3, 10], [0, 10]]);
color("#1E88E5") translate([10, 0, 0]) cube(5);
