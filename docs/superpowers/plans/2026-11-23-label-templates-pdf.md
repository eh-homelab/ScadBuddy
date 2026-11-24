# Label templates and a labels PDF (#7157)

Gap from the §11 parity sweep (#6270, `docs/features/filament-parity.md`; spec
`docs/superpowers/specs/2026-11-14-filament-management-design.md` §7 and §11). Before this
plan ScadBuddy (SB) printed one fixed 50 x 25 mm label from the browser, for spools (#6264)
and for printer, AMS and slot places (#6955).

## What Bambuddy has

Read in Bambuddy's (BB) public source at v1.3.1
([d3be761f](https://github.com/maziggy/bambuddy/tree/d3be761f3bd0b6bac78045dc53599331d64c3672/backend/app)),
never a live BB. SB's recorded spec of 1.2.6b1
(`backend/tests/fixtures/bambuddy-openapi-1.2.6b1.json`) lists `POST /api/v1/inventory/labels`
with no body schema.

- [`routes/labels.py`](https://github.com/maziggy/bambuddy/blob/d3be761f3bd0b6bac78045dc53599331d64c3672/backend/app/api/routes/labels.py):
  body `{spool_ids, template, layout?, monochrome, fields?, starting_position, format, dpi}`,
  at most 500 labels; the answer is the PDF (`application/pdf`), or PNGs.
- [`services/label_renderer.py`](https://github.com/maziggy/bambuddy/blob/d3be761f3bd0b6bac78045dc53599331d64c3672/backend/app/services/label_renderer.py):
  ReportLab and `qrcode`; templates `ams_holder_74x33`, `ams_holder_75x55`, `box_40x30`,
  `box_62x29` (one label a page), `avery_5160` (Letter, 66.675 x 25.4 mm, 3 x 10) and
  `avery_l7160` (A4, 63.5 x 38.1 mm, 3 x 7), and a custom sheet grid (#3296).

## Rulings

- **R-7157-1 Templates.** SB offers its own `roll_50x25` (the default, what it printed
  before) and BB's six built-in templates, with BB's sizes and sheet grids, plus `custom`:
  one label a page, 40 to 190 mm wide and 25 to 190 mm tall (190 mm: one label still
  fits the packed sheet, R-7157-7). The floor is the label's QR
  column: the 19.8 mm code with 2.6 mm of quiet zone on each side is 25 mm, and the text
  column needs 15 mm more. A label taller than 25 mm centres the same content; a wider one
  gives the text column the room. BB's saved custom sheet grids are not taken: the two
  Avery sheets cover sheet paper. The list is the API's (`GET /filament/label-templates`),
  so the page and the PDF never disagree on a size.
- **R-7157-2 The choice is a setting.** The stored-only setting `filament_label_template`
  (`{template, width_mm, height_mm}`; the sizes only for `custom`) holds the template; the
  Labels page reads it and saves each change, for spool and place labels alike. The roll or
  sheet layout stays a per-browser choice (`scadbuddy.labels.layout`, #6264). A sheet
  template prints on its own grid, so the layout choice does not apply to it.
- **R-7157-3 The PDF library is ReportLab 5.1.** Nothing in `backend/pyproject.toml` writes a
  PDF: Pillow only saves raster pages. ReportLab writes the pages, the text and a vector QR
  code (`reportlab.graphics.barcode.qr`) in one package, and its dependencies, Pillow and
  charset-normalizer, are locked already
  ([PyPI](https://pypi.org/project/reportlab/5.1.0/), `requires_dist`, read 2026-11-23).
  fpdf2 2.9.0 would add fontTools ([PyPI](https://pypi.org/project/fpdf2/2.9.0/)) and still
  need a QR library. BB uses ReportLab for the same PDF. `types-reportlab` is a dev
  dependency for mypy. Both pinned in `uv.lock` with the image's uv (0.13.8).
- **R-7157-4 The route.** `POST /api/v1/filament/labels` with `{spool_ids, location_ids,
  template?, layout}` answers the PDF as an attachment, `labels.pdf`. At most 100 labels
  (`MAX_LABELS`, as the page). It reads SB's own rows only and calls no Bambuddy. A label
  carries what the browser label carries: a spool's number, material and subtype, colour
  name and brand with the QR `scadbuddy:spool:<id>`; a place's code and name with the QR
  `<base>/x/L-<n>`. No template given: the stored setting's. An unknown id is a 404 that
  names it. It writes nothing, so it does not nudge the sync (`NOT_NUDGED`).
- **R-7157-5 A place label's address.** The base is `label_base_url` (the setting
  `filament_label_base_url`, else `public_url`'s https origin), never the request's `Host`:
  a printed label outlives the session, and the header is the client's. With none, a 409
  `no_label_base` that says to set the label address. A payload past 84 bytes is a 422, as
  the page refuses it.
- **R-7157-6 The browser print stays.** It is instant, needs no round trip and goes through
  the printer dialog. The PDF is for exact page sizes, for label software and for keeping
  or sending a set. Both draw from the same template list and the same content. The PDF is
  the answer's body and goes through `downloadBlob` (`frontend/src/lib/embed.ts`), so it
  saves inside Bambuddy's frame too; it is never put in JSON.
- **R-7157-7 A packed sheet in the PDF** (a one-label template with the `sheet` layout) is US
  Letter with 10 mm margins and 2 mm gaps: 190 x 259.4 mm of labels, which fits A4 too at
  actual size.
- **R-7157-8 Text in the PDF** is Helvetica, ReportLab's built-in font, so nothing is read
  from the image's font folders. A character outside its Latin-1 set prints as `?`. A long
  line is cut with an ellipsis, as the browser cuts it; a place name takes at most two lines;
  a word wider than its line is broken there and runs onto the next, and only the last line
  is cut.
- **R-7157-9 Only the newest template choice is stored.** The page sends one `PUT /settings`
  at a time. A pick made while one is in flight waits, and a newer pick replaces it, so the
  last PUT holds the newest choice and the page shows it; only that save's failure is shown.
  A choice saved before the stored setting is read outranks the stored one. Opening the
  custom fields saves nothing, so the stored setting still shows under them when it arrives,
  and what the person typed stays: the page never shows a template the person neither chose
  nor has stored. A custom size field left for the template picker saves nothing: the pick
  that follows decides.
- **R-7157-10 The agent reads the templates; the PDF has no tool.** `filament_label_templates`
  (`read`, `agent/src/tools/filament.ts`) lists the templates and the custom bounds, so the
  assistant can say which sizes exist; the choice itself is the setting, which it reads with
  `get_settings`. `POST /filament/labels` is a `NOT_A_TOOL` entry
  (`agent/src/tools/coverage.ts`): its answer is a PDF the assistant cannot read as text, the
  download tools' pattern embeds base64 (never, #2479) or links a GET, and this is a POST.

## Steps

1. Backend: `filament/label_templates.py` (the list, the setting's model, the size of a
   choice), `filament/label_pdf.py` (the drawing), the route in `api/filament.py`, the
   setting in `library/settings_store.py`, `api/settings.py` and `core/setting_docs.py`.
   Tests `tests/filament/test_label_pdf.py`, `tests/api/test_filament_label_pdf.py`.
2. Frontend: `labelSheet.ts` takes a size; `Label` and `LocationLabel` draw at it; the
   Labels page has a template picker (custom width and height), saves the setting, prints a
   sheet template on its grid, and has "Download PDF". Mocks and vitest tests.
3. Docs: `docs/features/spools-and-filament.md`, `docs/features/filament-parity.md` (the
   Labels row and the recorded gap row to MATCH).
