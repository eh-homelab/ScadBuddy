# Label Outputs Implementation Plan (#7216)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The labels PDF prints names in any script the image's fonts hold, and ScadBuddy (SB) gets
three of Bambuddy's (BB) label features: saved custom sheet grids, PNG output, and a choice of the
fields a spool label prints.

**Architecture:** The PDF stays the one drawing (`filament/label_pdf.py`, ReportLab). It embeds
DejaVu Sans with a fallback over the image's Noto fonts. A PNG is that PDF rasterised by PDFium
(`pypdfium2`). Saved sheets are a new table that only the API reads and writes. Each new option is a
stored setting that the Labels page saves at each change.

**Tech Stack:** Python 3.12, FastAPI, psycopg, ReportLab 5.1, pypdfium2 5.15, Pillow 13; React,
vitest, msw; the agent's TypeScript tools (zod).

**Spec:** issue #7216 (`gh issue view 7216`), the follow-up of #7157. The #7157 plan
`docs/superpowers/plans/2026-11-23-label-templates-pdf.md` and its rulings R-7157-1 to R-7157-10
stay in force unless a ruling below changes one. Spec of the filament area:
`docs/superpowers/specs/2026-11-14-filament-management-design.md` §7 and §11.

## What Bambuddy has

Read in BB's public source at v1.3.1, the commit the #7157 plan cites
([d3be761f](https://github.com/maziggy/bambuddy/tree/d3be761f3bd0b6bac78045dc53599331d64c3672/backend/app)),
read 2026-11-25 with `gh api`. No live BB and no printer was touched.

- Saved sheets (#3296):
  [`routes/label_templates.py`](https://github.com/maziggy/bambuddy/blob/d3be761f3bd0b6bac78045dc53599331d64c3672/backend/app/api/routes/label_templates.py)
  lists, creates, replaces and deletes named sheet layouts. Printing never reads that table: the
  dialog sends the layout with the print request.
  [`schemas/label_template.py`](https://github.com/maziggy/bambuddy/blob/d3be761f3bd0b6bac78045dc53599331d64c3672/backend/app/schemas/label_template.py):
  a name of 1 to 100 characters, NFC, unique when casefolded; the layout is `page`, `label_w_mm`,
  `label_h_mm`, `cols`, `rows`, `top_mm`, `left_mm`, `col_gap_mm`, `row_gap_mm`.
  [`services/label_renderer.py` L506-L561](https://github.com/maziggy/bambuddy/blob/d3be761f3bd0b6bac78045dc53599331d64c3672/backend/app/services/label_renderer.py#L506-L561):
  labels at least 40 x 20 mm, lengths at most 300 mm, 1 to 20 columns, 1 to 40 rows, the grid must
  fit the page with 0.01 mm of slack.
- PNG output:
  [`routes/labels.py`](https://github.com/maziggy/bambuddy/blob/d3be761f3bd0b6bac78045dc53599331d64c3672/backend/app/api/routes/labels.py)
  takes `format` `pdf` or `png` and `dpi` 203, 300 or 600. It rasterises the PDF
  (`pdf_to_pngs`, pypdfium2, under a process-wide lock), answers one PNG as it is and several in a
  stored ZIP (`label-<spool id>.png` for a roll, `sheet-<n>.png` for a sheet), and refuses sheet
  PNGs past 15 000 pages x dpi.
- Field choice (#2981): `fields` from `brand`, `material`, `hex`, `name`, `location`,
  `material_number`, `temps`, `weight`, `note`, `added`, `qr`, `spool_id`
  ([`label_renderer.py` L67-L102](https://github.com/maziggy/bambuddy/blob/d3be761f3bd0b6bac78045dc53599331d64c3672/backend/app/services/label_renderer.py#L67-L102));
  none sent prints the old set. BB's dialog keeps the choice per browser
  (`bambuddy-label-print-options` in `localStorage`,
  [`LabelTemplatePickerModal.tsx` L128](https://github.com/maziggy/bambuddy/blob/d3be761f3bd0b6bac78045dc53599331d64c3672/frontend/src/components/LabelTemplatePickerModal.tsx#L128)).

## Measured facts

- DejaVu Sans is `/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf` and `DejaVuSans-Bold.ttf`, from
  `fonts-dejavu-core`, which the Dockerfile's `base` stage installs through `fonts-dejavu`
  (`Dockerfile` L85). Measured 2026-11-25: `ls` in `openscad/openscad:dev.2026-11-23` (the pinned
  base), and `dpkg -S DejaVuSans.ttf` in a local app image (`scadbuddy:p4-385`).
- The same app image holds `NotoSans-Regular.ttf` and `-Bold.ttf`, and both weights of NotoSans
  Arabic, Armenian, Bengali, Devanagari, Ethiopic, Georgian, Hebrew, Khmer, Lao, Myanmar, Sinhala,
  Tamil and Thai under `/usr/share/fonts/truetype/noto/` (`fonts-noto-core`, `Dockerfile` L88). It
  holds no CJK font. Measured 2026-11-25 with `ls` in `scadbuddy:p4-385`.
- ReportLab 5.1 shapes text only with the optional `uharfbuzz` and reorders right-to-left text only
  with the optional `rlbidi` (`reportlab/pdfbase/ttfonts.py` L67, `reportlab/pdfgen/textobject.py`
  L22, read in the backend's locked 5.1.0). For an embedded TrueType subset it writes a ToUnicode
  CMap (`ttfonts.py` L1330), so PDFium can read the text back.
- pypdfium2 5.15.2 has no dependencies and ships manylinux x86_64 and aarch64 wheels
  ([PyPI JSON](https://pypi.org/pypi/pypdfium2/json), read 2026-11-25). "PDFium is inherently not
  thread-safe" ([README, Known limitations](https://github.com/pypdfium2-team/pypdfium2#incompatibility-with-threading)).

## Rulings

- **R-7216-1 The PDF's text is DejaVu Sans, embedded.** Regular and bold are the two paths above,
  registered with ReportLab once per process and embedded as subsets. Text no longer passes through
  Latin-1, so R-7157-8's `?` goes. The browser print keeps its own fonts. — The issue names it, and
  it is in the image already. — If wrong: the PDF grows by the subsets (tens of kB).
- **R-7216-2 A character DejaVu lacks takes the first Noto font that has it.** The chain is fixed:
  `NotoSans`, then `NotoSansArmenian`, `NotoSansGeorgian`, `NotoSansHebrew`, `NotoSansArabic`,
  `NotoSansDevanagari`, `NotoSansBengali`, `NotoSansTamil`, `NotoSansSinhala`, `NotoSansThai`,
  `NotoSansLao`, `NotoSansKhmer`, `NotoSansMyanmar`, `NotoSansEthiopic`, each in the line's weight.
  A font is loaded only when a character needs it; a missing fallback file is skipped and logged
  once. A character no font holds prints as U+FFFD from DejaVu, never as `?`. — It covers the
  scripts the image holds without loading many fonts for plain Latin text. — If wrong: a name in an
  uncovered script prints as U+FFFD, as it would with no fallback.
- **R-7216-3 No shaping, no right-to-left order, no CJK font in this change.** Arabic prints
  unjoined, Hebrew and Arabic print in stored order, and CJK prints as U+FFFD. Task 2 files a
  follow-up issue for `uharfbuzz` with `rlbidi`, and for a CJK font, which changes the render
  image's font set. — Each one is a new dependency or an image change with a review of its own. —
  If wrong: right-to-left and CJK names stay poor until that issue.
- **R-7216-4 A missing DejaVu file is an error, never Helvetica.** `POST /filament/labels` answers
  503 `label_font_missing` and names the path. — A silent fallback brings back the `?`. — If wrong:
  a machine without `fonts-dejavu-core` cannot make labels until it gets the package.
- **R-7216-5 A PNG is the PDF rasterised by pypdfium2.** The pin is `pypdfium2>=5.15,<6`, locked
  with the image's uv (`uvx uv@0.13.9 lock`). Every PDFium call holds one process-wide
  `threading.Lock`. — One drawing path, so the PNG and the PDF never disagree; BB does the same.
  A second drawing in Pillow would drift. — If wrong: a new binary dependency in the API image.
- **R-7216-6 How a PNG answer looks.** One page is `labels.png` (`image/png`); more pages are
  `labels.zip` (`application/zip`, stored, every entry dated 1980-01-01 so the bytes do not depend on
  the date). A roll's entries are named by the label (`spool-<number>.png`, `L-<n>.png`); a sheet's
  are `sheet-<n>.png`. Each PNG is greyscale (`L`) and carries its dpi (`pHYs`), so label software
  prints it at size. The dpi is 203, 300 or 600, as BB's. — BB's shape, with SB's own label names. —
  If wrong: a person renames files.
- **R-7216-7 A PNG answer holds at most 400 megapixels.** Measured before any page is drawn; past it
  the route answers 422 `png_too_large`, which names the limit and says to use PDF, a lower dpi or
  fewer labels. 100 roll labels of 50 x 25 mm at 600 dpi are 70 megapixels; one Letter page at 600
  dpi is 33.7. — BB caps sheet pages only, but SB's custom one-label size reaches 190 x 190 mm, so a
  roll can be large too. — If wrong: a large request needs two downloads.
- **R-7216-8 Format, dpi and fields are one stored setting, `filament_label_output`.** It is
  stored-only, like `filament_label_template` (R-7157-2): `{format, dpi, fields}`, default
  `{pdf, 300, <R-7216-9 default>}`. The Labels page reads it and saves each change. A request's own
  `format`, `dpi` and `fields` outrank it. — #6219: every option is a remembered choice with a
  control. BB keeps it per browser; SB keeps it for the install, as its template. — If wrong: two
  people on one install share one choice, as they share the template.
- **R-7216-9 The spool label's fields.** `number`, `material` (material and subtype), `colour`
  (colour name), `hex` (`#RRGGBB` of the effective colour), `brand`, `location` (the spool's place),
  `temps` (`190–230 °C`, or one value), `weight` (label weight, `1000 g`), `note` (its first line),
  `added` (the row's `created_at` date in UTC, ISO), `qr`. The default is what labels printed
  before: `number`, `material`, `colour`, `brand`, `qr`. Lines print top to bottom in that order.
  At least one field is chosen (422 otherwise). BB's `material_number` is not taken: SB's spool has
  no such field. Place labels keep their code, name and QR. — BB's set, mapped to SB's columns. — If
  wrong: a field is added later as one more value of the list.
- **R-7216-10 Lines that do not fit are left off from the bottom.** The text block may be the label's
  height less 2 x `PAD_MM`; the last lines go until it fits. The number or code line always stays.
  The browser label applies the same rule (`labelSheet.ts` `fitLines`), from the same sizes. With
  `qr` off the text column starts at the quiet zone and takes the QR's width too. — Nothing is ever
  drawn outside a label, and the browser and the PDF agree. — If wrong: a person chooses fewer
  fields or a taller template.
- **R-7216-11 Saved sheets are a table only the API reads and writes.** `filament_label_sheets`,
  a new migration. A name is 1 to 100 characters after trim and NFC, and unique when casefolded
  (`name_key`, a unique index, so two saves at once cannot both pass). At most 100 saved sheets
  (409 `too_many_label_sheets`, counted under a transaction advisory lock). Bounds: page `a4` or
  `letter`; a label 40 to 190 mm wide and 25 to 190 mm tall (SB's QR floor, R-7157-1, not BB's 20
  mm); margins and gaps 0 to 190 mm; 1 to 20 columns and 1 to 40 rows (BB's); the grid fits the page
  with 0.01 mm of slack (BB's message). — Root CLAUDE.md "Database access": no new client. The
  bounds keep the PDF's QR column whole. — If wrong: a BB sheet with labels under 25 mm tall cannot
  be saved.
- **R-7216-12 A saved sheet is chosen by its id.** `LabelTemplateChoice` gains `template: "saved"`
  with `sheet_id`; the PDF reads the row at each request, so an edit applies to the next print. A
  choice that names a deleted sheet answers 409 `label_sheet_gone`; the Labels page draws at the
  default size and says the saved sheet was deleted. A sheet grid must be saved before it is used:
  there is no one-off unsaved grid (`custom` stays one label a page). — One source for a sheet's
  numbers; BB copies the layout into each request instead, which lets a stored choice go stale. —
  If wrong: a person saves a grid once before a one-off print.
- **R-7216-13 Saved sheets are listed in `GET /filament/label-templates` as `saved`**, in
  `name_key` order, so the page and the agent read every size in one call. The writes are
  `POST /filament/label-sheets`, `PUT /filament/label-sheets/{sheet_id}` (the whole sheet) and
  `DELETE /filament/label-sheets/{sheet_id}`, all in `NOT_NUDGED`: they touch nothing the sync
  pushes. — If wrong: a separate list route is added later.
- **R-7216-14 The agent can save and delete sheets; the labels file stays `NOT_A_TOOL`.**
  `filament_label_sheet_upsert` and `filament_label_sheet_delete` (`write`); `filament_label_templates`
  lists `saved` too. The labels file is still binary, now also PNG or ZIP (R-7157-10). — A person
  can ask the assistant to set up their sheet. — If wrong: two tools to remove.
- **R-7216-15 Not taken from BB:** `starting_position`, `monochrome` and its colour swatch, and the
  `/preview` route. The page itself draws the labels at the chosen size. — Not asked by #7216. — If
  wrong: a follow-up issue each.
- **R-7216-16 One save queue for each stored label setting.** R-7157-9's rule (one `PUT /settings`
  at a time, a newer pick replaces a waiting one, only the newest save's failure shows) moves into a
  hook, `useSettingSaver`, that the template and `filament_label_output` both use. — The same race
  exists for both settings. — If wrong: the hook is one file to change.

- **P1 (pre-flight) Task 1 makes `filament_label_output` whole.** Task 1 stores `{format, dpi,
  fields}`, with `fields` at the R-7216-9 default; Task 3 makes `fields` act. — One shape of the
  setting from its first commit. — If wrong: none.

## Global Constraints

- Only the API holds SQL. Add no database client, no read or write of another service's tables, no
  cross grant (root `CLAUDE.md` "Database access", #5295).
- A migration is a NEW file `backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`
  (slug `[a-z0-9_]`), named when it is written. Never edit a merged one.
- Every label option is a remembered choice with a control (#6219): a stored setting or remembered
  choice, never a hardcoded value.
- Images and files move as blobs and `{name, mediaType}` pointers, never base64 in JSON or text
  (#2479, `.github/scripts/lint-base64-images.sh`). The PNG and ZIP are the answer's body.
- A test's result must not depend on runner speed or the date.
- Each behaviour change has a test seen to fail first.
- Frontend work loads the `frontend-design:frontend-design` skill first.
- Docs and comments are in Simplified Technical English (ASD-STE100). Docs carry no counts.
- Never commit `backend/openapi.json`, `frontend/src/api/schema.d.ts` or
  `agent/src/api/schema.d.ts`.
- Lock with the image's uv: `uvx uv@0.13.9 lock` (Dockerfile L38), never a local uv.
- Backend tests that build the app need Postgres (`backend/CLAUDE.md` "Commands"; locally one
  container per run, `CLAUDE.local.md`). Run Python from `backend/` with `uv run --frozen`.
- Every external fact in code comments and docs carries its link; every measured fact says where and
  how it was measured.

## Review Focus

1. A name that mixes scripts, such as `Пётр 3D ✓ 😀`: it prints with each run in its own font, the
   emoji as U+FFFD, and the ellipsis cut measures every run. Test: Task 2
   `test_a_mixed_script_name_is_cut_by_its_measured_runs`.
2. Two PNG downloads at once: PDFium is not thread-safe, so both pass the one lock and both answer
   good PNGs. Test: Task 1 `test_two_rasterisations_at_once_both_give_pngs`.
3. A stored choice that names a deleted saved sheet: the file answers 409 `label_sheet_gone`, and
   the page draws at the default size with a note. Tests: Task 4
   `test_a_choice_of_a_deleted_sheet_is_a_409`, Task 8 `shows a note when the saved sheet is gone`.
4. A spool that has none of the chosen fields (only `note` chosen, no note): one empty label of the
   right size, not a crash and not a missing page. Test: Task 3
   `test_a_spool_with_no_chosen_value_still_gets_its_page`.
5. A stored `filament_label_template` written before this change (`{template, width_mm,
   height_mm}`, no `sheet_id`) still reads. Test: Task 4 `test_an_old_template_choice_still_reads`.

---

### Task 1: PNG output of the labels (backend)

**Files:**
- Modify: `backend/pyproject.toml` (`pypdfium2>=5.15,<6` in `dependencies`), `backend/uv.lock`
- Create: `backend/scadbuddy/filament/label_output.py`, `backend/scadbuddy/filament/label_png.py`
- Modify: `backend/scadbuddy/filament/label_pdf.py` (factor out the sheet and the page count)
- Modify: `backend/scadbuddy/filament/models.py` (`LabelsPdfRequest` becomes `LabelsRequest`)
- Modify: `backend/scadbuddy/api/filament.py` (`render_labels_pdf` becomes `render_labels`; `NOT_NUDGED`)
- Modify: `backend/scadbuddy/library/settings_store.py`, `backend/scadbuddy/api/settings.py`, `backend/scadbuddy/core/setting_docs.py`
- Modify: `frontend/src/mocks/fixtures.ts` (the setting's default, so the mocks still type)
- Test: `backend/tests/filament/test_label_png.py`, `backend/tests/api/test_filament_label_pdf.py`
- Docs: `backend/CLAUDE.md` (the labels paragraph), `docs/features/spools-and-filament.md` (label templates)

**Interfaces:**
- Produces, `label_output.py`: `OutputFormat = Literal["pdf", "png"]`, `Dpi = Literal[203, 300, 600]`,
  `class LabelOutput(BaseModel)` (`extra="forbid"`; `format: OutputFormat = "pdf"`, `dpi: Dpi = 300`).
- Produces, `label_pdf.py`: `sheet_of(template: LabelTemplate, layout: Layout) -> LabelSheet | None`,
  `page_count(template: LabelTemplate, layout: Layout, count: int) -> int`; `render_pdf` uses both.
- Produces, `label_png.py`: `MAX_PNG_PIXELS: Final = 400_000_000`;
  `png_pixels(template, layout, count: int, dpi: int) -> int`;
  `class PngTooLargeError(ValueError)`;
  `render_pngs(pdf: bytes, dpi: int) -> list[bytes]` (under `_PDFIUM_LOCK: Final = threading.Lock()`);
  `png_names(labels: Sequence[Label], sheet: bool, pages: int) -> list[str]`;
  `zip_pngs(names: Sequence[str], pngs: Sequence[bytes]) -> bytes`.
- Produces, `models.py`: `LabelsRequest` = the old body plus `format: OutputFormat | None = None`,
  `dpi: Dpi | None = None` (`None`: the setting's value).
- Produces, settings: stored-only `filament_label_output: LabelOutput | None` (store and update
  classes), view `filament_label_output: LabelOutput` (default `LabelOutput()`), doc "Label output".

- [ ] **Step 1: Add the dependency.** `pypdfium2>=5.15,<6` in `backend/pyproject.toml`; `cd backend && uvx uv@0.13.9 lock`. Expected: `uv.lock` gains `pypdfium2` 5.15.x and nothing else moves.
- [ ] **Step 2: Write the failing tests** in `backend/tests/filament/test_label_png.py`:

```python
def test_one_roll_label_is_one_png_of_its_size_at_its_dpi() -> None:
    pdf = render_pdf([SPOOL], BY_ID["roll_50x25"], "roll")
    [png] = render_pngs(pdf, 300)
    image = Image.open(io.BytesIO(png))
    assert image.mode == "L"
    assert image.size == (round(50 / 25.4 * 300), round(25 / 25.4 * 300))
    assert image.info["dpi"] == pytest.approx((300, 300), abs=0.5)
    assert min(image.getdata()) == 0  # the QR's black modules are drawn

def test_each_page_is_one_png() -> None:
    assert len(render_pngs(render_pdf([SPOOL, PLACE], BY_ID["box_62x29"], "roll"), 203)) == 2

def test_roll_entries_are_named_by_the_label_and_sheet_entries_by_page() -> None:
    assert png_names([SPOOL, PLACE], sheet=False, pages=2) == ["spool-12.png", "L-7.png"]
    assert png_names([SPOOL] * 31, sheet=True, pages=2) == ["sheet-1.png", "sheet-2.png"]

def test_the_zip_is_stored_and_its_bytes_do_not_depend_on_the_date() -> None:
    one = zip_pngs(["a.png"], [b"x"])
    with zipfile.ZipFile(io.BytesIO(one)) as archive:
        [info] = archive.infolist()
        assert info.compress_type == zipfile.ZIP_STORED
        assert info.date_time == (1980, 1, 1, 0, 0, 0)
    assert zip_pngs(["a.png"], [b"x"]) == one

def test_the_pixels_count_every_page() -> None:
    assert png_pixels(BY_ID["roll_50x25"], "roll", 100, 600) == 100 * round(50 / 25.4 * 600) * round(25 / 25.4 * 600)
    assert png_pixels(BY_ID["avery_5160"], "roll", 31, 300) == 2 * round(215.9 / 25.4 * 300) * round(279.4 / 25.4 * 300)

def test_two_rasterisations_at_once_both_give_pngs() -> None:
    pdf = render_pdf([SPOOL], BY_ID["roll_50x25"], "roll")
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: render_pngs(pdf, 300), range(2)))
    assert all(r[0].startswith(b"\x89PNG") for r in results)
```

In `backend/tests/api/test_filament_label_pdf.py`, using the module's existing spool fixtures:
`test_png_of_one_label_is_labels_png` (`format: "png"`, one spool: 200, `image/png`,
`attachment; filename="labels.png"`); `test_png_of_several_labels_is_a_zip` (two spools: `application/zip`,
`labels.zip`, entries `spool-<n>.png` in request order); `test_the_stored_output_choice_is_used`
(`PUT /settings {"filament_label_output": {"format": "png", "dpi": 203}}`, then a body with no
`format`: a PNG whose `dpi` info is 203); `test_a_png_past_the_pixel_limit_is_a_422` (100 custom
190 x 190 labels at 600 dpi: 422, code `png_too_large`, nothing rendered);
`test_the_output_setting_round_trips` (default `{"format": "pdf", "dpi": 300}`; `null` puts it back).
A `dpi` of 150 is one more `test_a_bad_body_is_a_422` case.

- [ ] **Step 3: Run them to see them fail.** `uv run --frozen pytest tests/filament/test_label_png.py tests/api/test_filament_label_pdf.py -v`. Expected: FAIL (`ImportError` on `label_png`; 422 on `format`).
- [ ] **Step 4: Implement.** `render_pngs`: `pypdfium2.PdfDocument(pdf)`, each page `page.render(scale=dpi / 72).to_pil().convert("L")`, saved `PNG` with `dpi=(dpi, dpi)`; close each page and the document in `finally`, all under `_PDFIUM_LOCK`. `png_pixels`: `page_count` x the page's (or the label's, with no sheet) width and height in px, each `round(mm / 25.4 * dpi)`. `zip_pngs`: `zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))`, `ZIP_STORED`. The route reads `filament_label_output` beside the template, resolves the format and dpi (body, else setting, else default), checks `png_pixels` against `MAX_PNG_PIXELS` before it renders (`ApiError(422, ..., code="png_too_large")`), renders the PDF, and for PNG rasterises in `asyncio.to_thread`. The route's `description` and `responses` name `image/png` and `application/zip`. Rename in `NOT_NUDGED`.
- [ ] **Step 5: Run them to see them pass,** then the whole label suite and the settings tests: `uv run --frozen pytest tests/filament tests/api/test_filament_label_pdf.py tests/api/test_settings.py -n auto`. Expected: PASS. Then `uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy`.
- [ ] **Step 6: Docs.** `backend/CLAUDE.md` labels paragraph: `label_png.py` (PDFium under one lock, R-7216-5 to R-7216-7), the setting `filament_label_output`, the route's PNG and ZIP answers. `docs/features/spools-and-filament.md`: the PNG output and the setting.
- [ ] **Step 7: Commit.** `git add` the files above; `git commit -m "feat(filament): labels as PNG, one file or a ZIP (#7216)"`.

### Task 2: Text in any script the image holds (backend)

**Files:**
- Create: `backend/scadbuddy/filament/label_fonts.py`
- Modify: `backend/scadbuddy/filament/label_pdf.py` (fonts, runs, no `_latin`), `backend/scadbuddy/api/filament.py` (503)
- Test: `backend/tests/filament/test_label_fonts.py`, `backend/tests/filament/test_label_pdf.py`
- Docs: `backend/CLAUDE.md` (drop "Helvetica, Latin-1 only"), `docs/features/spools-and-filament.md`

**Interfaces:**
- Consumes: pypdfium2 (Task 1), for the tests' text reads.
- Produces, `label_fonts.py`: `Weight = Literal["regular", "bold"]`;
  `DEJAVU: Final[dict[Weight, Path]]` (the two R-7216-1 paths);
  `FALLBACK_DIR: Final = Path("/usr/share/fonts/truetype/noto")`;
  `FALLBACKS: Final[tuple[str, ...]]` (the R-7216-2 families, in order);
  `REPLACEMENT: Final = "�"`;
  `class LabelFontMissingError(RuntimeError)` (names the path);
  registered names `SBLabel-<weight>` for DejaVu and `SBLabel-<family>-<weight>` for a fallback;
  `reset_for_tests() -> None` (empties the cache of loaded fonts);
  `runs(text: str, weight: Weight) -> list[tuple[str, str]]` (registered font name, text), adjacent
  characters of one font joined;
  `width_mm(text: str, weight: Weight, size_mm: float) -> float` (the sum of the runs' widths).
- `label_pdf._Line.font` becomes `Weight`; `_width`, `_fit`, `_wrap` and `_big_size` measure through
  `width_mm`; drawing sets each run's font and advances x by its width.

- [ ] **Step 1: Rewrite the test helper.** In `test_label_pdf.py`, replace `_streams` with `_text(pdf: bytes) -> str`, every page's text from `pypdfium2.PdfDocument(pdf)[i].get_textpage().get_text_range()`, joined by newlines. Change the existing assertions from `"(#12)" in text` to `"#12" in text`. Run `uv run --frozen pytest tests/filament/test_label_pdf.py -v`. Expected: PASS still on Helvetica (PDFium reads Type 1 text too), so the helper is proven before the fonts change.
- [ ] **Step 2: Write the failing tests.** In `test_label_fonts.py`:

```python
def test_dejavu_holds_cyrillic_greek_and_latin_in_one_run() -> None:
    assert [font for font, _ in runs("Пётр Ωμέγα Zoë", "regular")] == ["SBLabel-regular"]

def test_a_character_no_font_holds_is_the_replacement_character() -> None:
    assert "".join(text for _, text in runs("A😀", "bold")) == "A�"

@pytest.mark.skipif(not (FALLBACK_DIR / "NotoSansThai-Regular.ttf").exists(), reason="the image's Noto fonts")
def test_thai_takes_the_noto_thai_font() -> None:
    assert runs("ไทย", "regular") == [("SBLabel-NotoSansThai-regular", "ไทย")]

def test_a_missing_dejavu_names_its_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setitem(label_fonts.DEJAVU, "regular", tmp_path / "none.ttf")
    label_fonts.reset_for_tests()
    with pytest.raises(LabelFontMissingError, match="none.ttf"):
        runs("a", "regular")
```

The skip applies only outside the image: CI runs the suite in the `test` image, which has the font.
In `test_label_pdf.py`: `test_a_name_outside_latin_1_prints_as_itself` (colour `Пётр`, brand
`Ζεύς`: `_text` holds both, and no `?`); `test_the_fonts_are_embedded` (the PDF bytes hold
`/FontFile2` and no `/BaseFont /Helvetica`);
`test_a_mixed_script_name_is_cut_by_its_measured_runs` (colour `"Пётр 3D ✓ 😀 " * 6` on
`roll_50x25`: the line ends with `…` and `width_mm(line, "regular", 3.0)` is at most the column).
In `tests/api/test_filament_label_pdf.py`: `test_a_missing_label_font_is_a_503` (monkeypatch
`DEJAVU` to a missing path: 503, code `label_font_missing`).

- [ ] **Step 3: Run them to see them fail.** `uv run --frozen pytest tests/filament/test_label_fonts.py tests/filament/test_label_pdf.py -v`. Expected: FAIL (`ImportError`; `?` in the text).
- [ ] **Step 4: Implement `label_fonts.py`.** `TTFont(name, path)` registered by `pdfmetrics.registerFont` on first use, under a `threading.Lock`, cached in a module dict; a font's coverage is `font.face.charToGlyph`. A fallback file that is missing is skipped, logged once at warning with its path. `reset_for_tests()` empties the cache. Then change `label_pdf.py`: drop `_latin`, `BOLD`/`PLAIN` become weights, and update its module docstring and R-7157-8 comment to R-7216-1 to R-7216-3. The route maps `LabelFontMissingError` to `ApiError(503, ..., code="label_font_missing")`.
- [ ] **Step 5: Run the label tests.** `uv run --frozen pytest tests/filament tests/api/test_filament_label_pdf.py -n auto`. Expected: PASS. DejaVu is wider than Helvetica, so a wrap or cut test can move: change an expected line only after you measure it with `width_mm`, and name each changed expectation in the commit body.
- [ ] **Step 6: File the follow-up issue** (R-7216-3): `gh issue create --title "feat(filament): labels PDF shapes Arabic and Indic text, orders right-to-left text, and prints CJK (#7216 follow-up)"` with the measured ReportLab facts above, the Noto list, and the cost of a CJK font package in the base image. Labels `enhancement`, `api`. Note its number in the plan's R-7216-3 line in the same commit.
- [ ] **Step 7: Docs.** `backend/CLAUDE.md`: `label_fonts.py` and the 503. `docs/features/spools-and-filament.md`: what prints and what does not (R-7216-3).
- [ ] **Step 8: Commit.** `git commit -m "fix(filament): the labels PDF prints names beyond Latin-1 (#7216)"`.

### Task 3: The fields a spool label prints (backend)

**Files:**
- Modify: `backend/scadbuddy/filament/label_output.py` (`LabelField`, `fields`), `label_pdf.py` (`SpoolLabel`, lines, fit, no-QR column), `labels.py` (`pdf_labels` fills the values), `models.py` (`LabelsRequest.fields`), `api/filament.py`, `core/setting_docs.py`
- Modify: `frontend/src/mocks/fixtures.ts` (the setting's default gains `fields`, so the mocks still type)
- Test: `backend/tests/filament/test_label_pdf.py`, `backend/tests/api/test_filament_label_pdf.py`
- Docs: `backend/CLAUDE.md`, `docs/features/spools-and-filament.md`

**Interfaces:**
- Produces, `label_output.py`: `LabelField = Literal["number", "material", "colour", "hex", "brand", "location", "temps", "weight", "note", "added", "qr"]`;
  `ALL_FIELDS: Final[tuple[LabelField, ...]]` (that order, R-7216-9);
  `DEFAULT_FIELDS: Final = ("number", "material", "colour", "brand", "qr")`;
  `LabelOutput.fields: list[LabelField]` (default `list(DEFAULT_FIELDS)`, 1 to `len(ALL_FIELDS)`,
  unique; a validator puts them in `ALL_FIELDS` order).
- Produces, `label_pdf.py`: `SpoolLabel` gains `hex`, `location`, `temps`, `weight`, `note`,
  `added: str | None = None`; `render_pdf(labels, template, layout, fields: Sequence[LabelField] = DEFAULT_FIELDS) -> bytes`.
- Produces, `labels.py`: `temps_text(low: int | None, high: int | None) -> str | None` (`"190–230 °C"`,
  `"220 °C"`, `None`), used by `pdf_labels`.
- Produces, `models.py`: `LabelsRequest.fields: list[LabelField] | None = None`.

- [ ] **Step 1: Write the failing tests.** In `test_label_pdf.py`, with `FULL = SpoolLabel(number=12, material="PLA Basic", colour="Jade White", brand="Bambu Lab", qr="scadbuddy:spool:x", hex="#00AE42", location="Shelf A", temps="190–230 °C", weight="1000 g", note="Dry first", added="2026-11-20")`:

```python
def test_the_default_fields_print_what_labels_printed_before() -> None:
    text = _text(render_pdf([FULL], BY_ID["ams_holder_75x55"], "roll"))
    assert all(s in text for s in ("#12", "PLA Basic", "Jade White", "Bambu Lab"))
    assert not any(s in text for s in ("#00AE42", "Shelf A", "1000 g"))

def test_only_the_chosen_fields_print_in_their_order() -> None:
    text = _text(render_pdf([FULL], BY_ID["ams_holder_75x55"], "roll", ["weight", "number", "hex"]))
    assert text.index("#12") < text.index("#00AE42") < text.index("1000 g")
    assert "PLA Basic" not in text

def test_lines_that_do_not_fit_are_left_off_from_the_bottom() -> None:
    text = _text(render_pdf([FULL], BY_ID["roll_50x25"], "roll", list(ALL_FIELDS)))
    assert "#12" in text and "2026-11-20" not in text

def test_without_the_qr_the_text_starts_at_the_quiet_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    drawn: list[str] = []
    monkeypatch.setattr(label_pdf, "_draw_qr", lambda *a: drawn.append(a[1]))
    render_pdf([FULL], BY_ID["roll_50x25"], "roll", ["number", "material"])
    assert drawn == []  # and the number's x, read with PDFium's char box, is QUIET_MM

def test_a_spool_with_no_chosen_value_still_gets_its_page() -> None:
    bare = SpoolLabel(number=3, material=None, colour=None, brand=None, qr="q")
    assert len(_boxes(render_pdf([bare, bare], BY_ID["roll_50x25"], "roll", ["note"]))) == 2

def test_place_labels_ignore_the_fields() -> None:
    assert "L-7" in _text(render_pdf([PLACE], BY_ID["roll_50x25"], "roll", ["note"]))
```

`test_label_output_puts_fields_in_order_and_refuses_none` (`LabelOutput(fields=["qr", "number"]).fields == ["number", "qr"]`;
`[]` and `["number", "number"]` raise `ValidationError`). `temps_text` cases: `(190, 230)`,
`(None, 220)`, `(None, None)`. In the API test: `test_a_spool_label_prints_the_chosen_fields` (a spool with
note, place, rgba `#00AE42FF`, label weight 1000 and temps 190 to 230; `fields` all: the text holds
`#00AE42`, the place's name, `190–230 °C`, `1000 g` and the note's first line on a 75 x 55 label;
`added` is the row's `created_at` date read back from the API, never today's date), and
`test_the_stored_fields_are_used` (`PUT /settings` with `fields: ["number"]`: the text has no
material).

- [ ] **Step 2: Run them to see them fail.** `uv run --frozen pytest tests/filament/test_label_pdf.py tests/api/test_filament_label_pdf.py -v`. Expected: FAIL (`TypeError` on `hex`; `ImportError` on `ALL_FIELDS`).
- [ ] **Step 3: Implement.** Line sizes in mm: number 9 bold (`leading=1.0`, as now), material 3.2 bold, colour 3.0, hex, brand, location, temps, weight, note and added 2.6, all regular. The first text line after the number keeps the 1 mm gap. Fit: drop the last line while the block is taller than `height - 2 * PAD_MM`; the number line stays. No `qr`: the column starts at `x + QUIET_MM` and is `width - QUIET_MM - PAD_MM` wide. `pdf_labels`: `hex` is `effective.rgba[:7]` upper case, `location` is `names.location`, `weight` is `f"{effective.label_weight_g} g"`, `note` is its first line stripped (`None` when empty), `added` is `created_at.astimezone(UTC).date().isoformat()`.
- [ ] **Step 4: Run them to see them pass.** The same command, then `tests/filament tests/api/test_filament_label_pdf.py -n auto`, ruff and mypy. Expected: PASS.
- [ ] **Step 5: Docs.** `setting_docs.py` "Label output" names the fields. `backend/CLAUDE.md` and `docs/features/spools-and-filament.md`: the fields, the default and R-7216-10.
- [ ] **Step 6: Commit.** `git commit -m "feat(filament): choose the fields a spool label prints (#7216)"`.

### Task 4: Saved sheet grids (backend)

**Files:**
- Create: `backend/scadbuddy/migrations/<UTC stamp>_filament_label_sheets.sql`, `backend/scadbuddy/filament/label_sheets.py`
- Modify: `backend/scadbuddy/filament/label_templates.py` (`"saved"`, `sheet_id`), `filament/component.py` (`label_sheets`), `filament/models.py` (`LabelTemplates.saved`), `api/filament.py` (routes, `NOT_NUDGED`, 409s), `core/setting_docs.py`
- Modify: `frontend/src/mocks/features/filament/index.ts` (`LABEL_TEMPLATES.saved: []`) and `frontend/src/mocks/fixtures.ts` (`sheet_id: null`), so the mocks still type
- Test: `backend/tests/filament/test_label_sheets.py`, `backend/tests/api/test_filament_label_sheets.py`
- Docs: `backend/CLAUDE.md`, `docs/features/spools-and-filament.md`

**Interfaces:**
- Produces, `label_sheets.py`: `MAX_SAVED_SHEETS: Final = 100`, `NAME_MAX: Final = 100`,
  `MAX_COLS: Final = 20`, `MAX_ROWS: Final = 40`, `FIT_SLACK_MM: Final = 0.01`;
  `name_key(name: str) -> str` (NFC, then casefold);
  `class SheetLayout(BaseModel)` (`page: PageSize`, `width_mm`, `height_mm`, `cols`, `rows`,
  `top_mm`, `left_mm`, `col_gap_mm = 0`, `row_gap_mm = 0`; R-7216-11 bounds; a fit validator whose
  message names the grid's width or height and the page's);
  `class LabelSheetIn(BaseModel)` (`name`, trimmed and NFC; `layout: SheetLayout`);
  `class SavedLabelSheet(BaseModel)` (`id: UUID`, `name: str`, `layout: SheetLayout`,
  `created_at`, `updated_at`);
  `class LabelSheetNameTakenError(ValueError)`, `class TooManyLabelSheetsError(ValueError)`,
  `class LabelSheetGoneError(LookupError)`;
  `class LabelSheetStore` (`__init__(pool)`; `list() -> list[SavedLabelSheet]` by `name_key`;
  `get(sheet_id) -> SavedLabelSheet | None`; `create(body) -> SavedLabelSheet`;
  `replace(sheet_id, body) -> SavedLabelSheet | None`; `delete(sheet_id) -> bool`);
  `to_template(sheet: SavedLabelSheet) -> LabelTemplate` (`id="saved"`, `name=sheet.name`);
  `resolve(choice: LabelTemplateChoice, sheets: LabelSheetStore) -> LabelTemplate` (raises
  `LabelSheetGoneError`; for other templates, `choice.resolved()`).
- Produces, `label_templates.py`: `TemplateId` gains `"saved"`; `LabelTemplateChoice.sheet_id: UUID | None = None`,
  required for `saved` and refused for the rest; `resolved()` raises `ValueError` for `saved`.
- Produces, `component.py`: `Filament.label_sheets` (a property, as `insert_weights`).
- Produces, routes: `POST /filament/label-sheets` (201, `SavedLabelSheet`), `PUT /filament/label-sheets/{sheet_id}`,
  `DELETE /filament/label-sheets/{sheet_id}` (204); route names `create_label_sheet`,
  `replace_label_sheet`, `delete_label_sheet`. `GET /filament/label-templates` gains
  `saved: list[SavedLabelSheet]`.

- [ ] **Step 1: Write the migration.** `touch "backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_filament_label_sheets.sql"`. Table `filament_label_sheets`: `id uuid PRIMARY KEY DEFAULT gen_random_uuid()`, `name text NOT NULL`, `name_key text NOT NULL UNIQUE`, `page text NOT NULL CHECK (page IN ('a4', 'letter'))`, `width_mm`, `height_mm`, `top_mm`, `left_mm`, `col_gap_mm`, `row_gap_mm` `double precision NOT NULL`, `cols` and `rows` `integer NOT NULL CHECK (… >= 1)`, `created_at` and `updated_at` `timestamptz NOT NULL DEFAULT now()`. A header comment cites #7216 and R-7216-11.
- [ ] **Step 2: Write the failing tests.** In `test_label_sheets.py` (pure models):

```python
AVERY_5163 = dict(page="letter", width_mm=101.6, height_mm=50.8, cols=2, rows=5, top_mm=12.7, left_mm=4.0, col_gap_mm=4.8)

def test_a_grid_that_fits_its_page_is_a_layout() -> None:
    assert SheetLayout(**AVERY_5163).cols == 2

@pytest.mark.parametrize("change, words", [
    ({"cols": 3}, "wide"), ({"rows": 6}, "tall"), ({"height_mm": 24}, None),
    ({"cols": 21}, None), ({"rows": 41}, None), ({"top_mm": -1}, None), ({"page": "a5"}, None),
])
def test_a_bad_grid_is_refused(change: dict[str, object], words: str | None) -> None:
    with pytest.raises(ValidationError, match=words or ""):
        SheetLayout(**{**AVERY_5163, **change})

def test_names_match_without_case_or_composition() -> None:
    assert name_key("Café Sheet") == name_key("CAFÉ sheet")

def test_an_old_template_choice_still_reads() -> None:
    assert LabelTemplateChoice.model_validate({"template": "custom", "width_mm": 60, "height_mm": 30}).sheet_id is None

def test_a_saved_choice_needs_its_sheet_id_and_only_it_has_one() -> None:
    with pytest.raises(ValidationError):
        LabelTemplateChoice(template="saved")
    with pytest.raises(ValidationError):
        LabelTemplateChoice(template="roll_50x25", sheet_id=uuid4())
```

In `tests/api/test_filament_label_sheets.py` (`client`, Postgres):
`test_a_saved_sheet_is_listed_with_the_templates` (POST 201, then `GET /label-templates`
`saved[0].layout == AVERY_5163` with defaults filled); `test_a_name_taken_in_another_case_is_a_409`
(`"Avery 5163"` then `"avery 5163"`: 409 `label_sheet_name_taken`; also on PUT to another
sheet's name); `test_the_hundred_and_first_sheet_is_a_409` (insert 100 rows straight through the
store, then POST: 409 `too_many_label_sheets`); `test_replace_and_delete` (PUT changes the
layout; DELETE 204, then 404 for both); `test_a_saved_choice_prints_on_the_saved_grid` (`template:
{"template": "saved", "sheet_id": id}`, 11 spools: two Letter pages); `test_an_edit_applies_to_the_next_print`
(PUT `cols: 1`, the same 11 spools: three pages); `test_a_choice_of_a_deleted_sheet_is_a_409`
(the stored setting names a deleted sheet: `POST /labels` with no template answers 409
`label_sheet_gone`); `test_the_sheet_writes_do_not_nudge_the_sync` (the three route names are in
`NOT_NUDGED`, and the existing nudge test's route walk passes).

- [ ] **Step 3: Run them to see them fail.** `uv run --frozen pytest tests/filament/test_label_sheets.py tests/api/test_filament_label_sheets.py -v`. Expected: FAIL (`ImportError`; 404 on the routes).
- [ ] **Step 4: Implement.** The store follows `shopping.py` (a pool of autocommit dict-row connections; callers run it in a thread). `create` runs in one transaction: `pg_advisory_xact_lock(hashtext('scadbuddy:filament:label_sheets'))`, then the count, then the insert; a `UniqueViolation` on `name_key` becomes `LabelSheetNameTakenError`. `replace` sets `updated_at = now()`. The PDF route resolves the choice with `resolve` in its thread and maps `LabelSheetGoneError` to 409 `label_sheet_gone` ("The saved sheet was deleted. Choose another template."). A 404 says "no label sheet <id>".
- [ ] **Step 5: Run them to see them pass,** then `tests/filament tests/api/test_filament_label_pdf.py tests/api/test_filament_label_sheets.py -n auto`, ruff and mypy. Expected: PASS.
- [ ] **Step 6: Docs.** `setting_docs.py` "Label template" adds saved sheets. `backend/CLAUDE.md`: `label_sheets.py`, the table, the routes, `NOT_NUDGED`. `docs/features/spools-and-filament.md`: saved sheets.
- [ ] **Step 7: Commit.** `git commit -m "feat(filament): saved label sheet grids (#7216)"`.

### Task 5: The agent's sheet tools

**Files:**
- Modify: `agent/src/tools/filament.ts` (two tools; the templates tool's description), `agent/src/tools/coverage.ts` (the labels entry's reason)
- Test: `agent/test/filament.test.ts`
- Docs: `agent/CLAUDE.md` only if it lists the filament tools by name (check with `grep -n filament_label agent/CLAUDE.md`)

**Interfaces:**
- Consumes: the Task 4 routes (`pnpm gen:api` writes their types).
- Produces: `filament_label_sheet_upsert` (`risk: 'write'`; input `sheet_id?: uuid`, `name: string`,
  `layout: {page, width_mm, height_mm, cols, rows, top_mm, left_mm, col_gap_mm?, row_gap_mm?}`;
  routes `POST /api/v1/filament/label-sheets` and `PUT /api/v1/filament/label-sheets/{sheet_id}`;
  `summarize`: `Save label sheet <name>`), `filament_label_sheet_delete` (`risk: 'write'`; input
  `sheet_id`; route `DELETE /api/v1/filament/label-sheets/{sheet_id}`).

- [ ] **Step 1: Write the failing tests** in `agent/test/filament.test.ts`, in the style of the existing `filament_label_templates` test: the two names in the risk table (`['filament_label_sheet_upsert', 'write']`, `['filament_label_sheet_delete', 'write']`); upsert with no `sheet_id` POSTs the body and returns the answer; with one, PUTs it; delete sends DELETE and reports the id; the coverage test passes with the new routes covered.
- [ ] **Step 2: Run them to see them fail.** `cd agent && pnpm exec vitest run test/filament.test.ts test/coverage.test.ts`. Expected: FAIL (no such tool; three routes uncovered).
- [ ] **Step 3: Implement the two tools.** The upsert's description says: a saved sheet is a named grid of labels on A4 or Letter, the bounds (R-7216-11), the 409s, "Only what the user says. Writes ScadBuddy only." The templates tool's description adds `saved`. The `NOT_A_TOOL` reason for `POST /api/v1/filament/labels` says the answer is a PDF, a PNG or a ZIP of PNGs (R-7216-14).
- [ ] **Step 4: Run them to see them pass.** The same command, then `pnpm typecheck && pnpm lint`. Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "feat(agent): save and delete label sheets (#7216)"`.

### Task 6: Output format and dpi on the Labels page (frontend)

Load `frontend-design:frontend-design` first.

**Files:**
- Create: `frontend/src/lib/useSettingSaver.ts`, `frontend/src/components/filament/LabelOutputPicker.tsx`
- Modify: `frontend/src/pages/filament/LabelsPage.tsx`, `frontend/src/api/filament.ts` (`labelsPdf` becomes `labelsFile`), `frontend/src/mocks/features/filament/index.ts`, `frontend/src/mocks/features/settings.ts`
- Test: `frontend/src/lib/useSettingSaver.test.ts`, `frontend/src/pages/filament/LabelsPage.test.tsx`
- Docs: `frontend/CLAUDE.md` (the `/filament/labels` text)

**Interfaces:**
- Consumes: `LabelsRequest`, `LabelOutput` (Task 1, through `pnpm gen:api`).
- Produces: `useSettingSaver<K extends keyof SettingsUpdate>(key: K): { save(value: SettingsUpdate[K]): void; error: string | null; savedBeforeRead: boolean }`
  (R-7157-9's queue, R-7216-16); `labelsFile(body: LabelsRequest): Promise<Blob>` (Accept
  `application/pdf, image/png, application/zip`); `fileName(blob: Blob): string` (`labels.png`
  for `image/png`, `labels.zip` for `application/zip`, else `labels.pdf`);
  `LabelOutputPicker({ value, onChange }: { value: LabelOutput; onChange(next: LabelOutput): void })`.

- [ ] **Step 1: Move R-7157-9 into the hook, behind the existing tests.** Extract the save queue from `LabelsPage.tsx` into `useSettingSaver`; the template's save uses it. Run `cd frontend && pnpm exec vitest run src/pages/filament/LabelsPage.test.tsx`. Expected: PASS with no test changed. Then write `useSettingSaver.test.ts` for the queue on its own (a slow PUT, two picks while it waits: two PUTs, the second with the newest value; the first's failure is not shown) and see it pass.
- [ ] **Step 2: Write the failing page tests** in `LabelsPage.test.tsx`: `downloads a PNG when the output is PNG` (choose PNG and 600 dpi: one `PUT /settings` with `filament_label_output: {format: 'png', dpi: 600, fields}`; Download sends `format: 'png', dpi: 600`; the file name asked of `downloadBlob` is `labels.png` for an `image/png` answer); `names a ZIP answer labels.zip`; `shows the stored output choice` (settings with `format: 'png', dpi: 203`: the picker shows them); `hides the dpi for PDF`.
- [ ] **Step 3: Run them to see them fail.** `pnpm exec vitest run src/pages/filament/LabelsPage.test.tsx`. Expected: FAIL.
- [ ] **Step 4: Implement.** The picker is a two-way PDF / PNG choice with a dpi select (203, 300, 600) shown for PNG, beside the template picker, saved through `useSettingSaver('filament_label_output')`. "Download PDF" becomes "Download", whose `title` names the format. `downloadBlob(() => labelsFile(body), () => fileName(blob))`, with the blob kept from the load. The mock `POST /labels` answers a one-byte body of the type the request's format asks for (one label: `image/png`; more: `application/zip`).
- [ ] **Step 5: Run them to see them pass,** then `pnpm typecheck && pnpm lint && pnpm exec vitest run src/pages/filament src/lib`. Expected: PASS.
- [ ] **Step 6: Docs.** `frontend/CLAUDE.md`: the output picker, `useSettingSaver`, `labelsFile`.
- [ ] **Step 7: Commit.** `git commit -m "feat(filament): download labels as PNG from the Labels page (#7216)"`.

### Task 7: Field choice on the Labels page (frontend)

Load `frontend-design:frontend-design` first.

**Files:**
- Create: `frontend/src/components/filament/LabelFieldsPicker.tsx`
- Modify: `frontend/src/components/filament/labelSheet.ts` (`spoolLines`, `fitLines`), `frontend/src/components/filament/Label.tsx` (`fields`), `frontend/src/pages/filament/LabelsPage.tsx`
- Test: `frontend/src/components/filament/labelSheet.test.ts`, `frontend/src/components/filament/Label.test.tsx`, `frontend/src/pages/filament/LabelsPage.test.tsx`
- Docs: `frontend/CLAUDE.md`

**Interfaces:**
- Consumes: `LabelField`, `LabelOutput.fields` (Task 3).
- Produces, `labelSheet.ts`: `ALL_FIELDS: readonly LabelField[]`, `DEFAULT_FIELDS` (both as the
  backend's, R-7216-9); `type LabelLine = { field: LabelField; text: string; sizeMm: number; bold: boolean }`;
  `spoolLines(spool: SpoolDetail, fields: readonly LabelField[]): LabelLine[]` (the backend's texts:
  `#RRGGBB`, `190–230 °C`, `1000 g`, the note's first line, the UTC date of `created_at`);
  `fitLines(lines: LabelLine[], heightMm: number): LabelLine[]` (R-7216-10, the backend's sizes and
  leading); `Label` takes `fields?: readonly LabelField[]` (default `DEFAULT_FIELDS`).
- Produces: `LabelFieldsPicker({ value, onChange })`, a checkbox per field; the last checked box
  is disabled.

- [ ] **Step 1: Write the failing tests.** `labelSheet.test.ts`: `spoolLines` with every field gives the texts in `ALL_FIELDS` order (fixture values as Task 3's `FULL`); `fitLines(lines, 25)` with every field keeps the number and drops `added` and `note` first, and the kept heights add up to at most `25 - 2 * 1.5`; `added` uses UTC (`created_at: '2026-11-20T23:30:00-05:00'` gives `2026-11-21`). `Label.test.tsx`: with `fields: ['number', 'hex']` it shows `#00AE42` and no brand; with no `qr` it draws no QR box. `LabelsPage.test.tsx`: `saves the fields chosen` (uncheck Brand: one PUT with `fields` lacking `brand`; the labels lose the brand line), `keeps one field` (with one left, its box is disabled), `place labels ignore the fields`.
- [ ] **Step 2: Run them to see them fail.** `pnpm exec vitest run src/components/filament src/pages/filament/LabelsPage.test.tsx`. Expected: FAIL.
- [ ] **Step 3: Implement.** The page saves fields through the same `filament_label_output` saver as Task 6 (one value, so a format change and a field change never overwrite each other). The picker groups under "Spool label prints"; place labels show a note that they print their code and name.
- [ ] **Step 4: Run them to see them pass,** then `pnpm typecheck && pnpm lint`. Expected: PASS.
- [ ] **Step 5: Docs.** `frontend/CLAUDE.md`: fields, `spoolLines`, `fitLines`.
- [ ] **Step 6: Commit.** `git commit -m "feat(filament): choose a label's fields on the Labels page (#7216)"`.

### Task 8: Saved sheets on the Labels page, and the parity rows (frontend, docs)

Load `frontend-design:frontend-design` first.

**Files:**
- Create: `frontend/src/components/filament/LabelSheetEditor.tsx`
- Modify: `frontend/src/pages/filament/LabelsPage.tsx` (picker group, `sizeOf`, the gone note), `frontend/src/api/filament.ts` (`createLabelSheet`, `replaceLabelSheet`, `deleteLabelSheet`), `frontend/src/mocks/features/filament/index.ts` (`LABEL_TEMPLATES.saved`, the three handlers)
- Test: `frontend/src/components/filament/LabelSheetEditor.test.tsx`, `frontend/src/pages/filament/LabelsPage.test.tsx`
- Docs: `frontend/CLAUDE.md`, `docs/features/filament-parity.md` (the #7157 gap row's "Not taken" and a new #7216 row), `docs/features/spools-and-filament.md`

**Interfaces:**
- Consumes: Task 4's routes and `LabelTemplates.saved`; `useSettingSaver` (Task 6).
- Produces: `LabelSheetEditor({ sheet, onSaved, onClose }: { sheet: SavedLabelSheet | null; onSaved(sheet: SavedLabelSheet): void; onClose(): void })`,
  a dialog with name, page, label width and height, columns, rows, top and left margins, and column
  and row gaps; it shows the API's 409 and 422 messages as they come.
- `sizeOf(choice, templates)` resolves `{template: 'saved', sheet_id}` from `templates.saved`;
  a missing id gives `DEFAULT_SIZE` and `gone: true`.

- [ ] **Step 1: Write the failing tests.** `LabelSheetEditor.test.tsx`: Save POSTs `{name, layout}` with numbers, not strings; on edit it PUTs to the sheet's id; a 409 `label_sheet_name_taken` shows its message and keeps the form; a 422 shows the API's fit message. `LabelsPage.test.tsx`: `lists saved sheets in the template picker` (a "Saved sheets" group); `prints a saved sheet on its grid` (`[data-sheet-page]` pages of `cols x rows`); `saves a new sheet and chooses it` (one PUT `filament_label_template: {template: 'saved', sheet_id}`); `deletes a saved sheet after a confirm`; `shows a note when the saved sheet is gone` (stored choice names an id not in `saved`: default size, the note, Download still sends the stored choice and shows the 409's message).
- [ ] **Step 2: Run them to see them fail.** `pnpm exec vitest run src/components/filament/LabelSheetEditor.test.tsx src/pages/filament/LabelsPage.test.tsx`. Expected: FAIL.
- [ ] **Step 3: Implement.** "New sheet…" and, for a chosen saved sheet, "Edit sheet…" and "Delete" sit beside the template picker. The editor's number fields take mm with one decimal step and send numbers; the server's message is the only validation text (R-7216-11 lives in one place). After a save or a delete, the page reads `GET /label-templates` again.
- [ ] **Step 4: Run them to see them pass,** then `pnpm typecheck && pnpm lint && pnpm exec vitest run src/pages/filament src/components/filament`. Expected: PASS.
- [ ] **Step 5: Docs.** `frontend/CLAUDE.md`: saved sheets on the page. `docs/features/filament-parity.md`: the #7157 gap row no longer lists saved sheets, PNG and field choice as not taken; a new row for #7216 says MATCH with file and test references (lines at this change), and lists R-7216-3 and R-7216-15 as not taken. `docs/features/spools-and-filament.md`: the page's saved sheets.
- [ ] **Step 6: Commit.** `git commit -m "feat(filament): saved label sheets on the Labels page (#7216)"`.

## Order and review

Tasks 1 to 4 are the backend, in order (Task 2's tests read text with Task 1's pypdfium2; Task 3
and Task 4 change `LabelsRequest` and the route after Task 1). Task 5 needs Task 4. Tasks 6 to 8
need their backend task and run in order (Task 7 and Task 8 use Task 6's saver). The PR body links
`Fixes #7216` and names the follow-up issue from Task 2.
