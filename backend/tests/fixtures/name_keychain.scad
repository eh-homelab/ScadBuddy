/* [Text] */
// Text on the tag
name = "Reagan"; // 20
// Text colour
text_colour = "#1f6feb"; // color
// Text font
text_font = "DejaVu Sans:style=Bold"; // font
// Text size (mm)
text_size = 10; // [4:0.5:20]
// Text depth (mm)
text_depth = 2; // [1:0.5:4]

/* [Base] */
// Base colour
base_colour = "#ff6ac1"; // color
// Base length (mm)
base_length = 60;
// Base width (mm)
base_width = 25;
// Base thickness (mm)
base_thickness = 4;

/* [Hidden] */
$fn = 48;

color(base_colour)
  cube([base_length, base_width, base_thickness], center = true);

color(text_colour)
  translate([0, 0, base_thickness / 2])
    linear_extrude(text_depth)
      text(name, size = text_size, font = text_font, halign = "center", valign = "center");
