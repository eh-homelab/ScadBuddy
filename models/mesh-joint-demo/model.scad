// Generated from scene.json by ScadBuddy; edit the scene, or eject (scene_eject) to edit this file
leaves_x = 70;

module sb_key(d, h, key, grow = 0) {
  r = d / 2 + grow;
  if (key == "hex") cylinder(h = h, r = r / cos(30), $fn = 6);
  else if (key == "flat") intersection() {
    cylinder(h = h, r = r, $fn = 48);
    translate([-r, -r, 0]) cube([2 * r, r + 0.35 * d + grow, h]);
  }
  else cylinder(h = h, r = r, $fn = 48);
}

module o1() {
  color("#F08A24") intersection() {
    import("meshes/o1-p1.3mf");
    translate([0, 0, 32]) multmatrix([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]) translate([-2000, -2000, -4000]) cube(4000);
  }
  color("#3A7D2C") intersection() {
    import("meshes/o1-p2.3mf");
    translate([0, 0, 32]) multmatrix([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]) translate([-2000, -2000, -4000]) cube(4000);
  }
  color("#3A7D2C") translate([0, 0, 32]) multmatrix([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]) sb_key(3.6, 4, "flat");
}

module o2() {
  color("#F08A24") difference() {
    intersection() {
      import("meshes/o1-p1.3mf");
      translate([0, 0, 32]) multmatrix([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]) translate([-2000, -2000, 0]) cube(4000);
    }
    translate([0, 0, 32]) multmatrix([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]) translate([0, 0, -0.01]) sb_key(3.6, 4 + 0.2 + 0.01, "flat", 0.2);
  }
  color("#3A7D2C") difference() {
    intersection() {
      import("meshes/o1-p2.3mf");
      translate([0, 0, 32]) multmatrix([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]) translate([-2000, -2000, 0]) cube(4000);
    }
    translate([0, 0, 32]) multmatrix([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]) translate([0, 0, -0.01]) sb_key(3.6, 4 + 0.2 + 0.01, "flat", 0.2);
  }
}

translate([0, 0, 0]) rotate([0, 0, 0]) scale([1, 1, 1]) o1();
translate([leaves_x, 0, 6]) rotate([0, 90, -180]) scale([1, 1, 1]) o2();
