# Pixel Art Mosaic

![Pixel art mosaic rendered with the default parameters](thumbnail.png)

A flat pixel-art plaque, fridge magnet or keychain. Each pixel is a square tile
in its own colour, on a backing plate with an optional raised frame. The art is
one of five built-in patterns or your own PNG, cut into up to eight brightness
bands, one colour each.

Inspired by Printables' "Articulated Pixel Heart Keychain" by Frikarte3D
(2,387 downloads), Tinker.Fun's pixel-art picture-frame inserts and Nerhak's
four-colour pixel-art fridge magnets. The file follows the Parametric Model
Maker customizer conventions, so it works unchanged on MakerWorld and in
ScadBuddy.

## How a picture becomes pixels

`surface()` reads the PNG as a heightfield whose height is the brightness
(0–100, `grey × 100 / 255`). The picture is stretched to `columns` × `rows`
mosaic pixels. A thin probe stands on each pixel centre and checks which band
that centre's brightness falls in. The probe is then grown back into a full
square pixel in that band's colour. With `bands = N`, the bands are split at
`100·k/N`; the darkest band is `color_1`. **A PNG whose size is exactly
`columns` × `rows` is reproduced pixel for pixel.** A larger picture is
resampled, so draw pixel art at its native size.

`sample-cat.png` (16 × 16) uses five greys, drawn to map onto colours 1–4 plus
the background with `bands = 5` and `png_background = lightest`.
`sample-sunset.png` (24 × 16) is a smooth picture for trying 8 bands and
resampling.

## Parameters

### Picture

| Parameter | Default | What it does |
|---|---|---|
| `image_file` | empty | Your PNG (upload widget, or a bare file name in this directory). Empty uses the built-in pattern. Only a bare name is read; anything with a folder or a leading dot is refused with a `NOTE:` and the pattern is used. |
| `image_type` | `png_bands` | `png_bands`: 2–8 brightness bands. `png_threshold`: two colours split at `image_threshold`. (`image_threshold`, the value's name before #318, still works.) |
| `bands` | `4` | Number of brightness bands (colours). |
| `image_threshold` | `50` | Split point (0–100) for the threshold mode. |
| `invert` | `false` | Reverse the band order, so the lightest band becomes `color_1`. |
| `columns` / `rows` | `16` / `16` | Mosaic size in pixels (4–64). Match your PNG. |
| `png_background` | `none` | Make the lightest or darkest band the background, so `background_mode` applies to it. |

### Pattern

| Parameter | Default | What it does |
|---|---|---|
| `pattern` | `mushroom` | Heart (13 × 12), star (13 × 13), smiley (12 × 12), mushroom (14 × 13), blocky green face (8 × 8). Each is drawn as rows of digits in `PATTERNS`: `.` is background and `1`–`8` pick `color_1`–`color_8`. |

### Pixels

| Parameter | Default | What it does |
|---|---|---|
| `pixel_size` | `5` | Pixel pitch in mm. Shrunk (with a `NOTE:`) if the mosaic would not fit the 300 × 320 mm plate. |
| `pixel_height` | `1.2` | Height of the coloured pixel layer. The only layers that change colour. |
| `pixel_gap` | `0` | Groove between pixels, showing the backing colour. 0 gives one flat, seamless surface. |
| `background_mode` | `fill` | `fill`: background pixels in `background_color`. `empty`: nothing there, the backing shows. `cut`: the plate follows the outline of the art (sticker/magnet shape). |

### Backing and frame

| Parameter | Default | What it does |
|---|---|---|
| `base_thickness` | `2` | Backing thickness. Raised automatically (with a `NOTE:`) to fit magnet pockets plus 0.6 mm. |
| `frame_width` | `3` | Frame around the mosaic; 0 = none. Around the art outline when `cut`. |
| `frame_raise` | `0.6` | How far the frame stands above the pixels. |
| `corner_radius` | `2` | Rounds the rectangular plate. |

### Mount

| Parameter | Default | What it does |
|---|---|---|
| `mount` | `hanger` | `none`, `hanger` (tab at the top centre), `keyring` (loop at the top-left corner) or `magnet` (pockets in the back). The tab is joined to the middle of the backing, so it holds on even when the art is cut away. |
| `hole_diameter` | `5` | Hole in the tab. |
| `magnet_diameter` / `magnet_thickness` | `10` / `3` | Magnet size; the pocket is `+ magnet_clearance` wide and 0.2 mm deeper. |
| `magnet_count` | `1` | 1 in the middle, 2 side by side, or 4 in a square. Each pocket sits in a pad of backing at least 1.6 mm wide all round, so it holds even where the art is cut away. Each pocket needs its diameter plus 3.2 mm of its share of the mosaic (half the width, and half the height for 4); when they would run into each other, it drops to 2, then 1, with a `NOTE:`. A single magnet bigger than the plaque keeps its pad, past the plaque's edge, with a `NOTE:`. |
| `magnet_clearance` | `0.2` | Extra pocket diameter. |

## Colours and extruders

The order of the colour parameters is the extruder order. A colour the art does
not use produces no part and takes no extruder. Equal colour values merge into
one part.

| Parameter | Part | Extruder |
|---|---|---|
| `base_color` | backing, frame, tab | 1 |
| `background_color` | background pixels (`fill`) | 2 |
| `color_1` … `color_8` | pattern digits 1–8 / brightness bands, darkest first | 3–10 |

Defaults: `color_1` black, `color_2` red, `color_3` white, `color_4` beige,
`color_5` yellow, `color_6` green, `color_7` blue, `color_8` pink.

## Print settings

- Prints flat and face up with no supports. Magnet pockets open onto the bed,
  and their roofs are short bridges.
- 0.2 mm layers. With the defaults, only the top 1.2 mm (6 layers) change
  colour. A 0.8 mm `pixel_height` halves the purge.
- Fridge magnets: glue the magnets into the pockets. Keep magnets away from
  small children.
- Small parts are a choking hazard for under-3s.

## Verifying

```bash
./verify.sh
```

Renders 30 cases:

- the defaults and every built-in pattern
- the sample PNGs with 3, 5 and 8 bands, threshold mode, inverted, and the
  lightest or darkest band as background
- a resampled picture with grooves
- every mount, a mosaic too big for the plate
- magnets that do not fit as asked: 4 pockets on a 16 mm mosaic (down to 1),
  2 × 25 mm on 40 mm (down to 1), 4 × 20 mm on a 120 × 40 mm strip (down
  to 2), and one 20 mm magnet on a 16 mm plaque (pad past the edge)
- a missing file and refused names

Each 3MF is checked for:

- no geometry on `Default`, and exactly the expected colour parts
- every colour rendered closed on its own (ScadBuddy's per-colour wrapper) adds
  up to the whole, so the bands never overlap
- each colour's volume is its pixel count × `pixel_size²` × `pixel_height`.
  The counts come from the pattern strings in `model.scad`, or from the PNG
  itself, decoded with Python's standard library. So every pixel landed in the
  band its brightness puts it in.
- sits on z = 0, has the expected height and bounding box, and fits the plate
- every magnet pocket is a real pocket: straight up from the bed at its
  centre, the first material is its roof, and there is backing all round
  its rim. Each pocket gets a 1.6 mm pad of backing, so this holds even
  when the art is cut away (the heart's empty corners)
- the pocket count drops (with its `NOTE:`) exactly when the pockets would
  not fit as asked, the pockets left keep a wall between them, and a pad
  only reaches past the plaque's outline when one magnet is bigger than the
  plaque, with its `NOTE:`
- refused names never reach `surface()`
- the legacy `image_threshold` value renders the same parts as `png_threshold`

Output lands in `.verify/`.
