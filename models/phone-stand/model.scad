// Phone / tablet stand — the device leans back against a backrest with its
// bottom edge in a slot behind a front lip. Three styles: a solid wedge, a
// lightweight A-frame with windows cut through the backrest, and a
// constant-thickness folded plate. Optional cable pass-through (a slot in
// the floor under the charging port, and a channel along the underside to
// the back) and optional text inlaid flush in the front face of the lip.
//
// Prints upright on its base, the way it stands on the desk, without
// supports: the backrest leans back by at most 40 degrees from vertical, the
// windows have 50-degree pointed tops, and the cable channel and the folded
// floor are short bridges. The base is lengthened automatically so the
// device's centre of mass always sits well inside the footprint.
//
// Written to the MakerWorld Parametric Model Maker customizer conventions so
// the same file works unchanged on MakerWorld and in ScadBuddy.
//
// The colour parameters are the extruder order: stand_color is extruder 1,
// front_color extruder 2, text_color extruder 3. Colours that are equal merge
// into one part and one filament.

/* [Device] */

// Device thickness including its case, in mm
device_thickness = 11; // [4:0.5:30]

// Height of the device as it stands in the stand, in mm (the long side for portrait); only used to keep it from tipping
device_height = 160; // [60:5:350]

// Extra room in the slot, in mm
clearance = 1; // [0.4:0.1:3]

/* [Stand] */

// Style of the stand
style = "solid"; // [solid:Solid wedge, cutout:Cut-out - lightweight A-frame, folded:Folded plate]

// Viewing angle: backrest tilt from the desk, in degrees (90 would be upright)
angle = 65; // [50:1:80]

// Width of the stand, in mm (a tablet in landscape wants 150+)
width = 80; // [40:5:250]

// Length of the backrest along its slope, from the slot floor, in mm
backrest_length = 90; // [30:5:250]

// Height of the lip in front of the device, above the slot floor, in mm
lip_height = 10; // [3:1:40]

// Height of the slot floor above the desk, in mm (room under the device for a charging plug)
floor_height = 14; // [4:1:40]

// Wall / plate thickness, in mm
thickness = 5; // [3:0.5:10]

/* [Cable] */

// Cable pass-through: a slot in the floor under the charging port and a channel along the underside to the back
cable_slot = true;

// Width of the cable slot and channel, in mm (fits the plug, not just the cable)
cable_width = 12; // [6:1:24]

// Height of the channel along the underside, in mm (clamped to 2 mm below the slot floor)
cable_channel_height = 8; // [4:1:20]

/* [Text] */

// Text inlaid in the front face of the lip (leave empty for none)
text = ""; // 30

// Typeface
font = "DejaVu Sans:style=Bold"; // font

// Text size in mm; shrinks automatically to fit the front face
text_size = 10; // [4:1:40]

/* [Colors] */

// Stand colour (extruder 1)
stand_color = "#546E7A"; // color

// Front lip colour (extruder 2; set it to the stand colour for one piece)
front_color = "#FF7043"; // color

// Text colour (extruder 3)
text_color = "#FFFFFF"; // color

/* [Hidden] */

$fn = 48;

a = angle;
t = thickness;
fz = max(floor_height, t + 1);           // slot floor height
u = [cos(a), sin(a)];                     // up the backrest
n = [sin(a), -cos(a)];                    // from backrest front face into it

// Slot: the device's back lies on the backrest front face, its bottom edge on
// the floor, so the floor between lip and backrest is thickness / sin(angle).
slot_w = device_thickness / sin(a) + clearance;
xb0 = t + slot_w;                         // backrest front face at the floor
P0 = [xb0, fz];
T1 = P0 + backrest_length * u;            // backrest top, front
T2 = T1 + t * n;                          // backrest top, back
B2 = P0 + t * n;                          // backrest bottom, back
lip_top = fz + lip_height;

// Folded plate: the rear leg leaves the back of the backrest two thirds of
// the way up and drops to the desk at 65 degrees (a 25-degree overhang).
leg_a = 65;
leg_top = P0 + 0.65 * backrest_length * u + (t / 2) * n;
leg_foot = [leg_top[0] + (leg_top[1] - t / 2) / tan(leg_a), t / 2];
x_leg = leg_foot[0];

// Device centre of mass (x): its back-bottom corner is at P0, it leans at the
// angle. The base runs at least 15 mm behind it (and behind the backrest).
dev_com_x = xb0 + device_height / 2 * cos(a) - device_thickness / 2 * sin(a);
x_struct = style == "folded" ? x_leg + t / 2 + 10 : T2[0];
x_rear = max(x_struct, dev_com_x + 15);
echo(STAND = [x_rear, dev_com_x, xb0, T1[1], slot_w, fz]);

cw = min(cable_width, width - 2 * t);
ch = min(cable_channel_height, fz - 2);

// ---- side profile: x from the front face (0) back, y up ----------------------

module solid_profile() {
    polygon([[0, 0], [0, lip_top], [t, lip_top], [t, fz], P0, T1, T2, [T2[0], 0]]);
    square([x_rear, t]);
}

module profile() {
    if (style == "folded") {
        square([t, lip_top]);                                   // front plate
        if (fz <= 2 * t) square([xb0 + t, fz]);                 // low floor: solid
        else {
            translate([0, fz - t]) square([xb0 + t, t]);        // floor, bridged
            translate([xb0, 0]) square([t, fz]);                // rear plate
        }
        polygon([P0, T1, T2, B2]);                              // backrest
        hull() {                                                // rear leg
            translate(leg_top) circle(d = t);
            translate(leg_foot) circle(d = t);
        }
        translate([x_leg - t / 2, 0]) square([x_rear - x_leg + t / 2, t]);  // tail
    } else if (style == "cutout") {
        difference() {
            solid_profile();
            offset(delta = -t) polygon([[xb0, 0], P0, T1, T2, [T2[0], 0]]);
        }
    } else {
        solid_profile();
    }
}

// ---- 3D, in the model frame: x back, y across, z up -------------------------

module body3d() {
    rotate([90, 0, 0]) linear_extrude(width, center = true) profile();
}

// Windows through the backrest (cut-out style): pointed tops, 50 degrees.
win_z0 = max(lip_top, fz + 0.2 * backrest_length * sin(a)) + 2;
win_z1 = fz + 0.85 * backrest_length * sin(a);
win_rail = max(8, width * 0.15);
win_hw = min(width / 2 - win_rail, (win_z1 - win_z0) / 1.2);
module windows() {
    if (style == "cutout" && win_hw > 4 && win_z1 - win_z0 > 8)
        translate([t + 1, 0, 0]) rotate([90, 0, 90]) linear_extrude(x_rear)
            polygon([[-win_hw, win_z0], [win_hw, win_z0], [win_hw, win_z1 - 1.2 * win_hw],
                     [0, win_z1], [-win_hw, win_z1 - 1.2 * win_hw]]);
}

module cable_cut() {
    if (cable_slot) {
        // Through the floor, under the charging port, down to the desk.
        translate([t + 0.8, -cw / 2, -1]) cube([xb0 - t - 0.8, cw, fz + 2]);
        // Channel along the underside to the back.
        translate([t + 0.8, -cw / 2, -1]) cube([x_rear, cw, ch + 1]);
    }
}

module stand3d() {
    difference() { body3d(); windows(); cable_cut(); }
}

// ---- text on the front face of the lip ---------------------------------------

has_text = len(text) > 0;
text_depth = min(1, t - 1.5);
tmargin = 1.5;
text_w = width - 2 * tmargin - 2;
text_h = lip_top - 2 * tmargin;

// Shrink-only fit (see models/name-sign): resize() scales to the bounding box
// of its children, and a hair-thin bar of the target length makes that box at
// least the target, so text already smaller is left alone.
module fit_x(w) {
    projection(cut = true) translate([0, 0, -0.5])
        resize([w, 0, 0], auto = [false, true, false])
            union() {
                linear_extrude(1) children();
                translate([-w / 2, 0, 10]) cube([w, 0.01, 0.01]);
            }
}
module fit_y(h) {
    projection(cut = true) translate([0, 0, -0.5])
        resize([0, h, 0], auto = [true, false, false])
            union() {
                linear_extrude(1) children();
                translate([0, -h / 2, 10]) cube([0.01, h, 0.01]);
            }
}

module text_2d() {
    fit_y(text_h) fit_x(text_w)
        text(text, size = text_size, font = font, halign = "center", valign = "center");
}

// The front face is the plane x = 0, seen from -x: text right is -y, up is +z.
module text3d() {
    translate([-0.01, 0, lip_top / 2]) rotate([90, 0, -90]) mirror([0, 0, 1])
        linear_extrude(text_depth + 0.01) text_2d();
}

// ---- colour regions -----------------------------------------------------------

module front_region() { translate([-1, -width, -1]) cube([t + 1, 2 * width, lip_top + 2]); }

module front_part() {
    difference() {
        intersection() { stand3d(); front_region(); }
        if (has_text) text3d();
    }
}
module rest_part() { difference() { stand3d(); front_region(); } }
module text_part() { intersection() { stand3d(); text3d(); } }

// Plate: the front faces -Y (towards the viewer), width runs along X.
rotate([0, 0, 90]) translate([-x_rear / 2, 0, 0]) {
    color(stand_color) rest_part();
    color(front_color) front_part();
    if (has_text && text_h > 3) color(text_color) text_part();
}
