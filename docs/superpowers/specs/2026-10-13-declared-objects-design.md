# Declared objects and print by object (#956)

The rest of #907. #955 shipped the `print_sequence` setting (a template default and a
per-print override); #1862 put a sequence picker in the print dialog's Advanced steps.
What "by object" still lacks is objects. Every plate ScadBuddy writes is one assembly
of colour parts, so Bambu Studio sees one object and "by object" prints it exactly as
"by layer" would.

## 1. How a template declares its objects

The same shape as plates (base spec §6.4), so an author who knows one knows the other:

```scad
/* [Hidden] */
$object = 0;                          // 0 = every object; ScadBuddy sets 1..N
echo(objects = count);                // the object count, may follow the parameters
function is_object(n) = $object == 0 || $object == n;

for (i = [1 : count])
  if (is_object(i)) translate([(i - 1) * pitch, 0, 0]) part();
```

- `echo(objects = N)` exactly, as a top-level statement. Absent, 0 or 1 means one
  object and nothing changes. At most `MAX_OBJECTS` (16) objects.
- ScadBuddy renders object *k* with `-D $object=k`, splits it by colour and solidifies
  it as it does a plate. Its colour parts are the object's parts.
  Each object is a render of its own, so a layout of P plates and N objects is P × N
  renders; the pipeline's `render_solids` budget scales with that count. P × N is at
  most `MAX_RENDERS` (16), the bound a job had before objects (#3250).
- **An object is drawn where it is**, not at the origin. Unlike plates, objects share
  a plate, and the spacing between them is what decides whether the toolhead clears
  them (§4). The `$object = 0` render, which the preview shows, is every object at once.
- With plates too, both are set: object *k* of plate *p* is the render with
  `$plate = p` and `$object = k`. An object that draws nothing on a plate is not on
  that plate. One that draws nothing on any plate is an error, as an empty plate is.
- A colour is still one extruder across the whole file, numbered from the
  everything-at-once render (§7).

Rejected:

- **A `$object` with no count**, discovering objects by rendering 1, 2, … until one is
  empty. That costs an extra render on every template, and a gap stops it short.
- **Splitting the mesh into connected components.** That needs no declaration, but it
  cannot tell two touching parts that are one object from two objects, and a letter's
  dot would become an object of its own.
- **A manifest field in `template.json`.** The count often follows the parameters
  (how many bins, how many tags), and only the template knows it.

A pipeline template (`ctx.render` / `ctx.pack`) already has objects: each placed copy
of a Part is one. It declares nothing extra.

## 2. The 3MF: one object per declared object

`PlateParts` carries each part's object (`objects`, 1-based, default all 1). The
writer emits one assembly per object on each plate, each its own `<item>` in the build
and its own `<object>` in `model_settings.config`, with the plate's offset. A template
with no declaration writes exactly what it does today. A packed plate (Arrange,
`ctx.pack`) gives each placed copy its own object.

A plate of several objects is named as a plate: its `plater_name` is the model's name,
with ` (plate N)` when the file has several plates. A plate of one object leaves it
empty, as the file always did. Bambu Studio reads the key as the plate's name
([`bbs_3mf.cpp` `PLATER_NAME_ATTR`](https://github.com/bambulab/BambuStudio/blob/47a13eeb1d9af65ef2371e361c856e21d4f6ca72/src/libslic3r/Format/bbs_3mf.cpp#L4720-L4721)).
It draws the name on the plate, shows it in the object list, and adds it to an exported
G-code file's name
([`Plater.cpp`](https://github.com/bambulab/BambuStudio/blob/47a13eeb1d9af65ef2371e361c856e21d4f6ca72/src/slic3r/GUI/Plater.cpp#L16581-L16588)).
That is read from the source, not yet seen by opening a file in Studio (#259).

## 3. The print dialog

The sequence picker (#1862) stays an Advanced step. It now says how many objects the
plate has. With one object, "by object" changes nothing, and the picker says so. The
check carries `print_sequence` (#2706), so a by-object print is judged as it will run.

## 4. The clearance pre-check: a warning, never a refusal

Bambu Studio refuses to slice a by-object plate whose objects the toolhead or the
gantry would hit. ScadBuddy says so before the print, as a warning in the check
(`sequence-clearance`), and never refuses. A sliced file is never refused either
(#2671), and Bambuddy's slicer still has the last word.

The values come from Bambu Studio's machine profiles, through
`scripts/generate_plate_profiles.py`, per printer model:

| H2C ([BambuStudio `47a13eeb`, H2C 0.4 nozzle profile](https://github.com/bambulab/BambuStudio/blob/47a13eeb1d9af65ef2371e361c856e21d4f6ca72/resources/profiles/BBL/machine/Bambu%20Lab%20H2C%200.4%20nozzle.json#L37-L40)) | |
|---|---|
| `extruder_clearance_radius` | 49 mm |
| `extruder_clearance_height_to_rod` | 47.4 mm |
| `extruder_clearance_height_to_lid` | 201 mm |

With `print_sequence = by object` and more than one object on a plate, read from the
3MF that will print (its build items, which is what the writer of §2 makes):

- **Horizontal:** two objects whose footprints are closer than the radius warn. The
  footprint is the object's bounding box on the bed, a superset of Bambu's convex hull,
  so the check can warn where Bambu Studio would not. It never stays quiet where Bambu
  Studio would refuse.
- **Height:** more than one object taller than `height_to_rod` warns, because only the
  last object printed may pass under the gantry. Any object taller than
  `height_to_lid` warns.

A printer whose model has no profile is not checked.
