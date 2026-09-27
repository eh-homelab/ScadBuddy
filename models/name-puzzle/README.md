# Kids' Name Puzzle

![Name puzzle rendered with the default parameters](thumbnail.png)

A child's name as a puzzle: a tray with a pocket shaped like each letter, and
a thick letter piece for each pocket. The letters are thicker than the pockets
are deep, so they stand proud of the tray and small fingers can pick them out.
The tray is a rounded rectangle, a cloud, or a train with a locomotive pulling
one wagon per letter.

Everything prints flat on one plate, no supports: the tray, with the letter
pieces in a row in front of it, each letter below its own pocket. Each pocket
is the letter's outline grown by `clearance` on every side. The counters of
A, B, O, R and friends stay in the tray as islands that poke up through the
letter, which makes the fit unambiguous. Letter corners are rounded 0.8 mm so
no piece has a sharp point.

Inspired by the name and letter puzzles on MakerWorld's Parametric Model
Maker, and Printables'
[Letters Puzzle](https://www.printables.com/model/523629) (248 downloads) and
[Alphabet Puzzle](https://www.printables.com/model/643522) (180 downloads);
written to the Parametric Model Maker customizer conventions, so the same file
works unchanged on MakerWorld and in ScadBuddy.

With the defaults (`MIA`, 40 mm letters) the tray is 124.4 × 57.3 × 8 mm and
the whole plate is 124.4 × 106.2 mm.

**Safety:** the letter pieces, and especially the dots of a lowercase `i` or
`j` (which print as separate little pieces), are a choking hazard for children
under 3. Supervise play; prefer capitals for young children.

## Parameters

### Text

| Parameter | Default | What it does |
|---|---|---|
| `name` | `MIA` | Up to 10 characters. Spaces are skipped (no piece, no pocket); on the rectangle and cloud trays they add a gap of 0.35 × `letter_size` between words. |
| `font` | `DejaVu Sans:style=Bold` | Typeface (`// font`). `DejaVu Sans:style=Bold` and `Lobster Two:style=Bold` are measured (see below). Any other face works, but each letter is shrunk into a generic cell. |
| `letter_size` | `40` | Height of a capital letter in mm (25–70). |

### Puzzle

| Parameter | Default | What it does |
|---|---|---|
| `tray_thickness` | `4` | Solid floor under the pockets. The tray is `tray_thickness + pocket_depth` tall. |
| `pocket_depth` | `4` | Depth of the letter pockets. Capped at `letter_thickness - 1` so every letter stands at least 1 mm proud. |
| `letter_thickness` | `7` | Thickness of the letter pieces. With the defaults they stand 3 mm proud of the tray. |
| `clearance` | `0.4` | Gap between a letter and its pocket wall, per side. |
| `knobs` | `false` | Adds a 6 mm tall grab knob on top of each letter, on the thickest part of the stroke nearest the middle (so it lands on the ring of an O, not in its hole). |
| `tray_shape` | `rounded_rect` | `rounded_rect`, `cloud` (bumps along the top, bottom and ends), or `train` (a locomotive at the left, then one wagon per letter on wheels, joined by a coupling bar; the cab has a shallow window recess). |
| `tray_margin` | `8` | Border around the pockets; on the train, around each wagon's pocket. |

### Colors

| Parameter | Default | What it does |
|---|---|---|
| `tray_color` | `#FFE08A` | Tray (butter yellow). |
| `letter_color_1` | `#E53935` | Letters 1, 5, 9 (red). |
| `letter_color_2` | `#1E88E5` | Letters 2, 6, 10 (blue). |
| `letter_color_3` | `#43A047` | Letters 3, 7 (green). |
| `letter_color_4` | `#8E24AA` | Letters 4, 8 (purple). |

## Colours and extruders

**The order of the `color` parameters in the source is the extruder order** —
the first one is extruder 1:

| Parameter | Part | Extruder |
|---|---|---|
| `tray_color` | tray | 1 |
| `letter_color_1` | letters 1, 5, 9 | 2 |
| `letter_color_2` | letters 2, 6, 10 | 3 |
| `letter_color_3` | letters 3, 7 | 4 |
| `letter_color_4` | letters 4, 8 | 5 |

Letters are counted with spaces skipped. A name with fewer than four letters
has fewer parts (`MIA` prints in four colours). Setting two colours equal
merges them into one part and one filament. Every piece is a single colour, so
a single-extruder printer can print each colour as its own plate.

## Glyph table

OpenSCAD cannot measure text without the experimental `textmetrics` feature,
so the pocket layout reads a table in `model.scad`: for every `A-Z`, `a-z` and
`0-9` in both measured faces, the glyph's bounding box and the centre of the
largest circle that fits inside it (the knob position), measured at size 100
from the fonts in the ScadBuddy image. Pockets are therefore exactly as wide
as their letters. Characters outside the table (accents, punctuation) and
other faces are shrunk (never enlarged) into a generic 0.9 × 1.1
`letter_size` cell; their knob may be missing if the cell centre falls in a
counter.

The puzzle grows with the name: ten 25 mm letters on the train are 432 mm
long. When the tray is longer than 250 mm, the render echoes a `NOTE:` saying
so; shorten the name or the letter size to fit a 256 mm bed.

## Variations

- `tray_shape`: `rounded_rect`, `cloud`, `train`.
- `knobs`: on or off.
- `font`: `DejaVu Sans:style=Bold` (chunky, easiest for small hands) or
  `Lobster Two:style=Bold` (script; each letter is still its own piece).

## Needs a test print

- `clearance` 0.4 mm per side is a starting point for PLA on a 0.4 mm nozzle;
  a letter should drop in and lift out without sticking. Tighten it for a
  snugger fit.
- Lobster Two has thin hairline joins (its `A` has the narrowest main stroke,
  about 0.15 × `letter_size`); check the pieces are sturdy enough at small
  sizes, or use DejaVu Sans Bold.

## Verifying

```bash
./verify.sh
```

Renders ten cases in `scadbuddy-verify:local` (building it from
`openscad/openscad:dev` with the font packages when it is missing): the
defaults, a Lobster Two cloud, a train with knobs, a two-word name, the
largest letter with the pocket-depth cap, the smallest size with an accented
character and 0.2 mm clearance, an unmeasured face, a ten-letter train, an
empty name and an all-space name. For each it checks the colour parts, that
`Default` has no triangles, the tray outline against the dimensions the glyph
table implies, the z range of every closed per-colour part (rendered the way
ScadBuddy builds its parts), that the letter row lies clear in front of the
tray, and two volume facts: the plate's union equals the sum of the parts (no
two pieces overlap), and with the hidden `assembled = true` — letters placed
in their pockets — it still does (every letter fits its pocket without
touching the tray). Output lands in `.verify/`.
