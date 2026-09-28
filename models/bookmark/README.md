# Bookmark

![Bookmark rendered with the default parameters: a blue tassel bookmark with a yellow rim, a flower-and-diamond lattice cut through it, an inlaid pink heart, white inlaid text, and two beads beside it](thumbnail.png)

A flat bookmark that prints face up with no supports, in as many colours as
you like. Pick an outline, then optionally use a picture as a **mask** — the
outline itself, a stencil cut through the strip, a flush colour inlay, or
raised / recessed relief — add a second picture as a colour **overlay**, text
along or across the strip, a coloured rim, two-tone top layers, colour stripes
and beads for a tassel cord.

Inspired by the most downloaded bookmarks on Printables — Dada3Dcraft's
[Cat paw Bookmark](https://www.printables.com/model/674534) (27,044
downloads) and Bamingo Design's
[Minimal Linework Bookmarks](https://www.printables.com/model/1395937)
(12,996 downloads; outline art as the bookmark) — and, for the parametric
strip, Thomas Mergan's
[Bookmark (customizable)](https://www.printables.com/model/88273) (2,694
downloads). No geometry was copied; everything is generated from the
parameters and the sample pictures shipped here.

Written to the MakerWorld Parametric Model Maker customizer conventions, so the
same file works unchanged on MakerWorld and in ScadBuddy. The picture handling
(`*_file`, `*_type`, `image_threshold`, scale / x / y / rotation / invert, the
missing-file behaviour) is the shared ScadBuddy file-parameter convention
(`// file:svg,png`, #204), so any template using it behaves the same.

**Safety:** the beads are small parts — a choking hazard for children under 3.

## Shapes

| `shape` | Outline |
|---|---|
| `classic` | Rounded rectangle, `width` x `length`. |
| `tassel` | Classic with a cord hole near the top (default). |
| `pointed` | The bottom end comes to a rounded point `end_length` long. |
| `ribbon` | Swallowtail: a V notch `end_length` deep in the bottom end. |
| `corner` | Triangular pocket that slips over the corner of a page (below). |
| `rounded_tab` | The top end is a full semicircle (flattened to a half-ellipse when the bookmark is wider than it is long). |
| `scalloped` | Every edge is a row of round scallops. |
| `star_top` / `heart_top` | A star or heart `topper_size` wide on top of the strip, in `topper_color`. |
| `custom_mask` | The outline comes from `mask_file` (same as `mask_mode = outline`). |

`hole_on_any_shape` adds the cord hole to every shape except `corner`. All
outlines are centred on the origin, length along Y; the extents are exact
except where a rounded tip gives up a little (pointed / ribbon ends, star
points).

### Page corner

A right-angled triangle, `corner_size` along each short edge, made of a back
plate on the bed, a front plate on top, and `page_gap` of air between them.
The two short edges are solid walls `wall_width` wide joining the plates; the
long edge is open, so a page corner slides in until it meets the walls. The
front plate carries every decoration, and a cutout goes through the front
plate only.

The front plate's first layer is a **bridge**: its lines run parallel to the
long edge and are anchored on both walls (up to about `corner_size * 1.4`
long at the open edge). `page_gap` 0.6 mm leaves room for a 0.1 mm page plus
some sag; raise it if the first bridge layer droops onto the back plate.

## Parameters

### Shape

| Parameter | Default | What it does |
|---|---|---|
| `shape` | `tassel` | Outline, see above. |
| `length` | `150` | Top to bottom, mm (60-250). |
| `width` | `40` | Strip width, mm (20-80). |
| `thickness` | `2` | Thickness, mm (1.2-4). |
| `corner_radius` | `4` | Corner rounding; also rounds the pointed and ribbon ends. |
| `hole_diameter` | `5` | Cord hole, mm. |
| `hole_from_top` | `9` | Cord hole centre below the top end, mm. |
| `hole_on_any_shape` | `false` | Cord hole in the other shapes too. |
| `end_length` | `18` | Pointed: length of the point. Ribbon: depth of the notch. |
| `scallop_size` | `8` | Scallop diameter, mm. |
| `topper_size` | `56` | Width of the star / heart on top, mm (at most 2.5 x `width`, and no taller than 60 % of the length; a NOTE says when it is reduced). |

### Corner clip

| Parameter | Default | What it does |
|---|---|---|
| `corner_size` | `55` | Length of the two joined edges, mm. |
| `page_gap` | `0.6` | Air gap the page slides into, mm (0.3-1.5). |
| `plate_thickness` | `1` | Front and back plates, mm. Total height `2 * plate_thickness + page_gap`. |
| `wall_width` | `4` | Width of the two joined edges, mm. |

### Mask

| Parameter | Default | What it does |
|---|---|---|
| `mask_file` | `sample-lattice.svg` | Upload an SVG or PNG (`// file:svg,png`), or a file name in this model's directory. Empty = off. |
| `mask_type` | `auto` | `auto` reads a name ending `.png` through `surface()` and anything else with `import()`; `svg` / `png_threshold` force one. `png_threshold` keeps the pixels darker than `image_threshold`. The old value `image_threshold` still works (see below). |
| `mask_mode` | `cutout` | `outline`, `cutout`, `inlay`, `emboss`, `deboss` — see below. |
| `mask_fit` | `fit` | `fit`: largest size that fits the area, aspect kept. `fill`: covers the area, aspect kept, cropped. `stretch`: exactly the area. |
| `mask_scale` | `100` | Picture size as % of the fitted size. |
| `mask_x`, `mask_y` | `0` | Move the picture, mm. |
| `mask_rotation` | `0` | Rotate the picture after fitting, degrees. |
| `mask_invert` | `false` | Swap picture and background within the mask area. |
| `mask_repeat` | `1` | Tile the picture this many times along the strip (each tile fitted to its share). Not used for outlines. |
| `image_threshold` | `50` | Brightness cut-off (%) for images; shared by the mask and the overlay. |
| `min_feature` | `1.2` | Narrowest bridge left between cutouts and narrowest part of an outline, mm (three 0.4 mm lines). |
| `edge_margin` | `3` | Solid margin a cutout keeps from the edge, mm; raised to the rim width + `min_feature` (and to `wall_width` on the page corner). |
| `outline_border` | `1.5` | Outline mode: grow the picture by this much so thin lines become printable. |
| `tie_bars` | `auto` | Cutout: bars tying loose pieces to the rest. `auto` = centre cross when inverted, none otherwise. |
| `tie_pitch` | `15` | Spacing of the `grid` tie bars, mm. |

**`mask_type` / `overlay_type` value renamed (#318).** The PNG choice's value is
`png_threshold`; it was `image_threshold`, the same spelling as the numeric `image_threshold` parameter. The
model still reads the old value as `png_threshold`, so saved presets and past outputs
that hold it render exactly as before, with no migration step. The customizer does
not offer the old value, so a preset that holds it shows no matching dropdown
choice, and saving that preset again is refused (422, not one of the options) until
the PNG choice is re-picked.

The **mask area** is the strip inside the cutout margin, below the cord hole
and below a star / heart topper (on the page corner: the triangle's square
inside the margin). The picture is fitted into it, then scaled, rotated and
moved.

### Overlay

A second picture, always in its own colour, independent of the mask — so a
bookmark can have a cutout mask *and* a colour overlay.

| Parameter | Default | What it does |
|---|---|---|
| `overlay_file` | `sample-overlay.svg` | Upload an SVG or PNG (`// file:svg,png`), or a file name in this model's directory. Empty = off. |
| `overlay_type` | `auto` | As `mask_type`. |
| `overlay_style` | `inlay` | `inlay` (flush, `inlay_depth` deep) or `raised` (`relief` high). |
| `overlay_scale` | `55` | Picture width as % of the bookmark width (`corner_size` on the corner). |
| `overlay_x`, `overlay_y` | `0`, `46` | Position from the centre of the strip, mm (from the right-angle corner on the page corner). A NOTE warns when the centre is off the bookmark, e.g. the default 46 on a bookmark under 92 mm long. |
| `overlay_rotation` | `0` | Degrees. |
| `overlay_invert` | `false` | Swap picture and background. |

### Text

| Parameter | Default | What it does |
|---|---|---|
| `label` | `Keep reading` | Text, up to 40 characters. Empty = none. |
| `font` | `Lobster Two:style=Bold` | Typeface; ScadBuddy fills this dropdown from the fonts in the image (`// font`). |
| `text_direction` | `vertical` | `vertical` runs along the strip reading bottom to top; `horizontal` runs across it. On the page corner the text runs parallel to the long edge. |
| `text_style` | `inlay` | Flush inlay or raised. |
| `text_size` | `11` | Letter height, mm. |
| `auto_fit` | `true` | Shrink (never enlarge) the text to fit inside the rim and clear of the cord hole. |
| `text_y` | `-12` | Move the text along the strip, mm. The text keeps clear of the ends and the cord hole; a `text_y` that would put it off the strip is pulled back with a NOTE. |

### Layers

| Parameter | Default | What it does |
|---|---|---|
| `inlay_depth` | `0.6` | Depth of flush inlays (text, overlay, mask inlay); at most thickness - 0.4. |
| `relief` | `0.6` | Height of raised parts and depth of the deboss. |
| `two_tone` | `false` | The top `top_thickness` of the face takes `top_color`; a deboss then shows the base colour. |
| `top_thickness` | `0.4` | Two-tone thickness, mm. |
| `border` | `true` | A rim around every edge in `border_color`, full height. |
| `border_width` | `2` | Rim width, mm. |
| `base_style` | `solid` | `stripes`: `stripe_count` equal bands across the strip, full height. |
| `stripe_count` | `4` | 2-6 stripe colours. |

### Beads

| Parameter | Default | What it does |
|---|---|---|
| `bead_count` | `2` | Beads for the tassel cord, printed beside the bookmark (only when there is a cord hole). |
| `bead_size` | `12` | Bead diameter, mm; height is 0.8 x that, 45° chamfers top and bottom, hole upright. |
| `bead_hole` | `4` | Bead hole, mm. |

## Mask modes

- **`outline`** — the picture *is* the bookmark: fitted into `width` x
  `length` less `outline_border`, grown by `outline_border`, then opened by
  `min_feature` so nothing thinner survives. The rim follows every edge,
  including holes in the picture. The picture must be one connected shape; a
  picture of separate pieces prints as separate pieces.
- **`cutout`** — the picture is cut through the strip (through the front plate
  on the page corner), keeping `edge_margin` from the edge and `min_feature`
  clear of the cord hole, the text, the overlay and a topper. Every hole is
  shrunk by `min_feature / 2`, so **any bridge between two holes is at least
  `min_feature` wide**; slivers too thin to print are dropped, and a hole that
  shrinking would split at a narrow neck is re-joined rather than left with a
  hair-thin wall across it. With `mask_invert` the picture is kept and the rest
  of the mask area is cut away (a silhouette in a window).
- **`inlay`** — the picture fills a pocket `inlay_depth` deep in `mask_color`,
  flush with the face.
- **`emboss`** / **`deboss`** — raised `relief` in `mask_color`, or recessed
  `relief` deep.

**Islands.** A cutout whose picture has a hole — a ring, the dots in the
sample leaf — leaves a loose disc, and an inverted cutout leaves every
separate piece of the picture loose. Tie bars fix that: `centre` runs one bar
along and one across the mask area through the picture's centre (any single
connected picture crosses both), `grid` runs bars every `tie_pitch` mm (pieces
smaller than the pitch in both directions can still be missed). Bars are
exactly `min_feature` wide. `verify.sh` renders every cutout case as one solid
and fails if it is not one connected body.

## Pictures

**Getting a picture in.** `mask_file` and `overlay_file` are file parameters
(`// file:svg,png`, [#204](https://github.com/eh-homelab/ScadBuddy/issues/204)):
in ScadBuddy's customizer each is a drop zone for an SVG or PNG. ScadBuddy
stores the upload beside `model.scad` under a generated bare name
(`_scadbuddy_solid_asset_<hex>.svg` / `.png`) and passes that name in, and
`mask_type` / `overlay_type` `auto` pick the reader from its extension, so a
PNG works without touching the type. OpenSCAD itself sees an ordinary string,
so on the command line, in the OpenSCAD GUI or on MakerWorld, put the file
next to `model.scad` and type its **bare name** (as `verify.sh` does with the
samples).

**Only bare names are read.** A value containing `/` or `\`, or starting with
`.` (e.g. `../model.scad`, `/etc/hostname`, `sub/x.svg`, `.hidden.svg`) is
refused. A `..` elsewhere in a bare name (`cat..v2.svg`) has no directory to
climb out of and is read normally. For a refused name the model logs `NOTE: mask_file "..." ignored ...` and never calls
`import()` / `surface()` on it, so a parameter cannot read files outside the
model directory.

**Missing files.** A bare name that does not exist does not break the render:
an SVG logs `ERROR: Can't open file ...`, an image logs `WARNING: The file ...
couldn't be opened`, OpenSCAD still exits 0, and the bookmark renders without
the picture. A missing `custom_mask` outline falls back to the classic rounded
rectangle.

**Centring.** A picture is placed by the centre of its page (SVG) or canvas
(PNG), and scaled by the size of the art itself, so art that sits off-centre
on its page lands off-centre on the bookmark. Crop the page tight around the
art. PNGs are read at one pixel per unit before scaling, so a small PNG gives
stair-stepped edges; SVG is smoother.

Samples shipped with the model (all drawn for it):

| File | What | Used for |
|---|---|---|
| `sample-lattice.svg` | staggered flowers and diamonds, 30 x 119 | cutout (the default), emboss, deboss, inlay |
| `sample-cat.svg` | sitting cat silhouette with a thin tail | outline, inverted cutout |
| `sample-leaf.png` | 64 x 160 px leaf with three round holes and a stem | every mode as a PNG; the holes make islands |
| `sample-rings.svg` | three rings | the island test |
| `sample-overlay.svg` | a heart | the overlay (the default) |

## Colours and extruders

**The order of the `color` parameters in the source is the extruder order.**
Each distinct colour is one part and one filament; a parameter the chosen
options do not use produces no part and takes no extruder, and later ones move
up. Two parameters with the same value merge into one part.

| Parameter | Part | Used when |
|---|---|---|
| `base_color` | the body (first stripe) | always |
| `stripe_color_2` … `stripe_color_6` | stripes 2-6 | `base_style = stripes` |
| `topper_color` | the star / heart on top | `star_top`, `heart_top` |
| `border_color` | rim | `border` |
| `top_color` | two-tone top layers | `two_tone` |
| `text_color` | text | `label` not empty |
| `mask_color` | mask inlay / emboss | `mask_mode` `inlay`, `emboss` |
| `overlay_color` | overlay | `overlay_file` |
| `bead_color`, `bead_color_2` | beads, alternating | a cord hole and `bead_count` > 0 |

The defaults use six (blue body, yellow rim, white text, pink heart, orange and
red beads); everything on at once is 14, well inside a 28-colour AMS setup.
Parts never overlap: inlays sit in pockets cut from the body, raised parts sit
on top, and the decorations are clipped in priority order — text, then
overlay, then mask — so the later never covers the earlier.

## Printing

0.4 mm nozzle, 0.2 mm layers, face up, no supports. The default 2 mm strip
with a 0.6 mm inlay is ten layers, the top three shared with the inlays.
`min_feature` 1.2 is three lines of a 0.4 mm nozzle; do not go below 0.8.
Render time (Manifold, this machine, wall clock including container start) is
0.3-1 s for most settings and 2.5 s for an inverted cutout with a grid of
tie bars; ScadBuddy renders once more per colour for the closed parts.

## Verifying

```bash
./verify.sh                      # 43 cases
ONLY='corner|missing' ./verify.sh    # a subset by name regex
```

It renders the defaults, every shape, every mask mode with both an SVG and a
PNG, fit / fill / stretch, repeat, invert with centre and grid tie bars, a
cutout together with a raised PNG overlay, missing mask / outline / overlay
files, refused names (`../model.scad`, `/etc/hostname`, `sub/x.svg`,
`.hidden.svg`), upload-style names (`_scadbuddy_solid_asset_<hex>.svg` /
`.png`, copied in for the run and removed after), the PNG reader forced with
`png_threshold` and with the pre-#318 value `image_threshold` (which must
render the same parts, of the same volume), and the largest bookmark. For each it checks:

- no uncoloured geometry, and exactly the expected colour parts;
- on z=0, exactly as tall as the layers imply, inside and filling its extent,
  beads beside it;
- **one piece:** the bookmark rendered as a single solid (`debug="solid"`) is
  one connected body; a negative control (rings, no tie bars) must come out as
  several;
- **min feature:** the top face (`debug="face"`) has no two different edges
  (outer edge, cord hole, each cutout) closer than `min_feature`, and opening
  it by `min_feature / 2` (`debug="thin"`) removes only facet crumbs; a
  negative control built for 0.8 mm must measure under 1.2;
- **colour parts do not overlap:** each colour rendered closed on its own
  through a `color()`-filtering wrapper (like ScadBuddy's closed-part
  renderer), and the parts' volumes add up to the whole;
- **page corner:** the gap is at least a 0.1 mm page + 0.2 mm clearance, empty
  from the corner out to the open long edge, nothing beyond the long edge, both
  short edges solid walls;
- missing files are logged and the render completes (a missing outline gives
  the classic `width` x `length`); refused names are never opened and are
  logged with a NOTE.

Last run: `OK: all cases passed` (43 cases, 521 checks).

## Upload widget (#204)

The upload widget needs a ScadBuddy with file parameters (#204, shipped by
PR #231). On an older ScadBuddy both fields are plain text boxes: type a bare
file name that sits in this model's directory, such as `sample-cat.svg`.

With the widget, both
file parameters are annotated `// file:svg,png`, the model accepts the
generated upload names (`verify.sh` renders a case with two of them), and the
`auto` type means a PNG upload needs no other change. What this template
still would like from it:

- **The other samples are not selectable in the app.** A file parameter takes
  the default, empty, or an upload, so `sample-cat.svg`, `sample-leaf.png` and
  `sample-rings.svg` can only be used by downloading and re-uploading them. A
  way to pick a file shipped in the template's directory would expose them.
- **Placement is by page / canvas centre** (see *Centring*); a preview that
  shows the page outline, or a hint to crop an SVG tight, would make
  `mask_x` / `mask_y` less of a guess.
- PNG uploads are scaled to 256 px on the long side, which suits this model:
  the surface() read stays well under a second.
