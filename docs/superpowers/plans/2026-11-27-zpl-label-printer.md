# ZPL Labels to a Network Zebra Implementation Plan (#7170)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Labels page sends spool, place and printer labels to a Zebra GK420t on the LAN as
raw ZPL on port 9100, direct thermal or thermal transfer, with no browser print dialog.

**Architecture:** The label layer (#7216) stays the one layout: the page asks
`POST /api/v1/filament/labels` for 203 dpi PNGs, then posts those bytes to a new Go route in
`tools/go/core` (area `label-printer`). Core reads the printer's address from two stored
settings that only a person sets, turns each image into a 1-bit `^GFA` graphic, and writes one
ZPL job to the socket. Python adds only settings and one template.

**Tech Stack:** Go 1.x (`tools/go/core`, `image/png`, `archive/zip`, `net`), Python 3.12 and
FastAPI (settings only), React, vitest, msw.

**Spec:** `docs/superpowers/specs/2026-11-27-zpl-label-printer-design.md` (Decisions R1 to R8,
and "The ZPL job"). Read it first.

## Global Constraints

- New routes are Go, in `tools/go/core` (owner, #7667). Python changes here are edits to its
  settings and templates only. Only the API and core hold SQL (root `CLAUDE.md` "Database
  access"). Core runs no untrusted code.
- A request never names a host or a port. The address is the stored settings
  `filament_label_printer_host` and `filament_label_printer_port` (default `9100`), and only a
  person's own session changes or resets them (403 `person_only`).
- Media and darkness are remembered choices with a control (#6219):
  `filament_label_printer_media` (`direct` | `transfer`, default `direct`) and
  `filament_label_printer_darkness` (0 to 30, default `10`).
- Images move as bytes in a body, never base64 in JSON (#2479,
  `.github/scripts/lint-base64-images.sh`). The `^GFA` hex on the printer socket is the
  printer's wire format.
- Limits, exact: body at most 8 MiB (`8 << 20`); at most 100 labels; image width at most 832
  dots; `^GF` bytes per image at most 99999; dpi 203 (a `pHYs` of 7990 to 8000 px/m, or none).
- Problem codes, exact: `label_printer_unset` (409), `label_printer_unreachable` (502),
  `label_not_png` (422), `label_too_large` (422), `label_wrong_dpi` (422), `too_many_labels`
  (422); a body past the bound is 413.
- Timeouts, defaults: dial 5 s, write 30 s, both fields of the sender.
- Tests use a fake listener on `127.0.0.1:0` or `net.Pipe`; never a real printer. A test's
  result must not depend on runner speed or the date; no `time.Sleep`.
- Each behaviour change has a test seen to fail first.
- Every frontend task loads the `frontend-design:frontend-design` skill first.
- Docs and comments are in Simplified Technical English. Docs carry no counts. Every external
  fact has its link; every measured fact says how it was measured.
- Never commit `backend/openapi.json` or either `schema.d.ts`. Lock with `uvx uv@<Dockerfile
  version>` if a lock moves (none should).
- Locally run only lint and build (ruff, mypy, `go vet`, eslint, typecheck) plus the tests a
  task names; CI runs the rest. Backend tests that need Postgres use a container of their own
  (`CLAUDE.local.md`).

## Review Focus

1. A sheet template (Avery) or the `sheet` layout is chosen: its PNG is a Letter or A4 page,
   wider than 832 dots. The page disables "Print to Zebra" and says why; a direct call gets
   422 `label_too_large`, and nothing reaches the socket. Tests: Task 2
   `TestAnImageWiderThanTheHeadIsRefused`, Task 5 `disables Print to Zebra for a sheet`.
2. The printer is off, or its port refuses: 502 `label_printer_unreachable` within the dial
   timeout, and the message names the host. Test: Task 3
   `TestARefusedPortIsUnreachable`.
3. The printer accepts the connection and never reads (a full buffer, a jam): the write
   deadline ends the send with 502, and the handler returns. Test: Task 3
   `TestAPrinterThatNeverReadsTimesOut` (`net.Pipe`, write timeout 1 ms).
4. Two clicks at once: each connection carries one whole job, never a mix. Test: Task 3
   `TestTwoSendsNeverInterleave`.
5. A ZIP with a non-PNG entry, a directory, or an entry that inflates past 8 MiB: 422
   `label_not_png` or `label_too_large`, and the reader stops at the bound. Test: Task 2
   `TestABadZipIsRefused`.

---

### Task 1: The printer settings and the 2 x 1 in template (backend, Python)

**Files:**
- Create: `backend/scadbuddy/filament/label_printer.py`
- Modify: `backend/scadbuddy/filament/label_templates.py` (`TemplateId`, `TEMPLATES`)
- Modify: `backend/scadbuddy/library/settings_store.py` (stored-only fields beside
  `filament_label_output`, in the stored class and in `SettingsPatch`)
- Modify: `backend/scadbuddy/api/settings.py` (view fields; a person-only set and its check,
  beside `FLOW_ACCESS`)
- Modify: `backend/scadbuddy/core/setting_docs.py` (four entries)
- Modify: `frontend/src/mocks/fixtures.ts`, `frontend/src/mocks/features/settings.ts` (the
  defaults, so the mocks still type)
- Test: `backend/tests/filament/test_label_printer.py`,
  `backend/tests/api/test_label_printer_settings.py`,
  `backend/tests/api/test_filament_label_pdf.py` (one case)
- Docs: `backend/CLAUDE.md` (the labels paragraph)

**Interfaces:**
- Produces, `label_printer.py`: `MediaType = Literal["direct", "transfer"]`;
  `DEFAULT_PORT: Final = 9100`; `DEFAULT_DARKNESS: Final = 10`;
  `def printer_host(value: str) -> str` (strip; a DNS name of labels `[A-Za-z0-9-]`, 1 to 63
  each, no leading or trailing `-`, at most 253 in all, or an `ipaddress.ip_address` literal;
  else `ValueError`). IPv6 is written bare, with no brackets.
- Produces, settings (stored only, no environment seed):
  `filament_label_printer_host: str | None` (validated by `printer_host`),
  `filament_label_printer_port: int | None` (`ge=1, le=65535`),
  `filament_label_printer_media: MediaType | None`,
  `filament_label_printer_darkness: int | None` (`ge=0, le=30`). View defaults: `None`,
  `9100`, `"direct"`, `10`. `null` in a PUT puts back the default.
- Produces, `api/settings.py`: `LABEL_PRINTER = frozenset({"filament_label_printer_host",
  "filament_label_printer_port"})` and `_label_printer_by_a_person(patch)`: 403 `PERSON_ONLY`,
  detail `"Only a person changes the label printer's address: " + names`.
- Produces, template: `roll_2x1in`, name `"Roll label 2 x 1 in"`, `width_mm=50.8`,
  `height_mm=25.4`, no sheet.
- Go (Task 3) reads the four rows by these names; the frontend reads the view fields.

- [ ] **Step 1: Write the failing tests.** `test_label_printer.py`:
  `test_a_host_is_a_name_or_an_ip` (`"zebra.lan"`, `" 192.168.1.40 "` gives `"192.168.1.40"`,
  `"fe80::1"`); `test_a_host_with_more_is_refused` (parametrized: `""`, `"http://zebra"`,
  `"zebra:9100"`, `"zebra/x"`, `"a@zebra"`, `"-zebra"`, `"zebra lan"`, `"[fe80::1]"`, a 254-
  character name; each `ValueError`). `test_label_printer_settings.py` (as
  `test_flow_answerers_setting.py`, with its `AGENT` headers):
  `test_the_printer_settings_default` (host `None`, port 9100, media `direct`, darkness 10);
  `test_a_person_sets_the_printer` (PUT host `"zebra.lan"`, port 6101, media `transfer`,
  darkness 22; GET gives them; `null` gives the defaults back);
  `test_an_agent_cannot_move_the_printer` (parametrized host and port bodies with `AGENT`: 403,
  type ends `person_only`, the stored host is unchanged);
  `test_an_agent_may_set_media_and_darkness` (200 with `AGENT`);
  `test_bad_printer_values_are_a_422` (port 0, port 65536, darkness 31, media `"ribbon"`,
  host `"http://zebra"`). In `test_filament_label_pdf.py`:
  `test_the_2x1_template_is_one_png_of_406_by_203` (`template: {template: "roll_2x1in"}`,
  `format: "png"`, `dpi: 203`, one spool: `Image.size == (406, 203)`).
- [ ] **Step 2: Run them to see them fail.** `cd backend && uv run --frozen pytest
  tests/filament/test_label_printer.py tests/api/test_label_printer_settings.py
  tests/api/test_filament_label_pdf.py -k "printer or 2x1" -v`. Expected: FAIL (`ImportError`;
  422 on the unknown setting and template).
- [ ] **Step 3: Implement** the interfaces above. Call `_label_printer_by_a_person(patch)`
  where `_flow_access_by_a_person(patch)` is called. Doc entries: "Label printer host" (the
  Zebra's LAN name or IP address; only a person's session changes it; unset hides "Print to
  Zebra"), "Label printer port" (raw ZPL port, 9100 by default), "Label print mode" (direct
  thermal, or thermal transfer with a ribbon for labels that see heat; the Labels page saves
  the last choice), "Label darkness" (0 to 30, the printer's `~SD`; 10 is the GK420t's own
  default; the Labels page saves the last choice).
- [ ] **Step 4: Run them to see them pass,** then `uv run --frozen pytest
  tests/test_setting_docs.py tests/filament -n auto` and `uv run --frozen ruff check . &&
  uv run --frozen ruff format --check . && uv run --frozen mypy`. Expected: PASS.
- [ ] **Step 5: Docs.** `backend/CLAUDE.md` labels paragraph: the template `roll_2x1in`, the
  four settings, that host and port are person-only, and that Go's `internal/labelprinter`
  reads them.
- [ ] **Step 6: Commit.** `git commit -m "feat(filament): label printer settings and a 2 x 1 in template (#7170)"`.

### Task 2: Images to ZPL (Go, pure)

**Files:**
- Create: `tools/go/core/internal/labelprinter/zpl.go`, `tools/go/core/internal/labelprinter/body.go`
- Test: `tools/go/core/internal/labelprinter/zpl_test.go`, `tools/go/core/internal/labelprinter/body_test.go`

**Interfaces:**
- Produces, `zpl.go`:
  `type Media string` with `const Direct Media = "direct"`, `Transfer Media = "transfer"`;
  `type Job struct { Media Media; Darkness int }`;
  `const MaxWidth = 832`, `const MaxGraphicBytes = 99999`;
  `func Encode(images []image.Image, job Job) []byte` (the job of the spec's "The ZPL job":
  `~SD%02d` once, then one `^XA…^XZ` per image; upper-case hex, no line breaks);
  `func graphic(img image.Image) (row int, data []byte)` (1 bit a dot, MSB first; black when
  the grey composed on white is below 128; unused low bits 0).
- Produces, `body.go`:
  `const MaxBody = 8 << 20`, `const MaxLabels = 100`;
  `func Images(body []byte) ([]image.Image, error)`: a PNG (magic `\x89PNG\r\n\x1a\n`) is one
  image; a ZIP (magic `PK\x03\x04`) is its `.png` entries in the archive's order. For each
  image it reads `png.DecodeConfig` first and refuses before it decodes. The total of the
  entries' bytes read is bounded by `MaxBody` (`io.LimitReader`), never by the header's sizes.
  The `pHYs` chunk, when present, must be 7990 to 8000 px/m on both axes, unit metre.
  Errors are `httpx.WithReason`-style problems with the spec's codes (Global Constraints),
  but with the field `code` (as `unknowntrays`), so the page's `problemCode` reads them.

- [ ] **Step 1: Write the failing tests.** `zpl_test.go`:
  `TestEncodeGivesTheExactJob`: a 10 x 2 image, row 0 black at x 0 and x 9, row 1 white;
  `Encode([]image.Image{img}, Job{Direct, 10})` equals
  `"~SD10^XA^MTD^PW10^LL2^LH0,0^FO0,0^GFA,4,4,2,8040" + "0000" + "^FS^XZ"`.
  `TestTransferAndDarknessAreWritten` (`Job{Transfer, 5}`: starts `~SD05^XA^MTT`).
  `TestEachImageIsItsOwnFormat` (two images: two `^XA`, two `^XZ`, one `~SD`).
  `TestGreyAndAlphaAreComposedOnWhite` (grey 127 is black, 128 white; an RGBA pixel black at
  alpha 0 is white).
  `body_test.go` (images built with `image/png` in the test; a `pHYs` chunk written by a small
  test helper `withPHYs(png []byte, pxPerMetre uint32) []byte`):
  `TestOnePngIsOneImage`; `TestAZipKeepsItsOrder` (entries `spool-2.png`, `spool-1.png` give
  that order); `TestTheLabelLayersDpiIsTaken` (7992 px/m passes; no `pHYs` passes);
  `TestAnotherDpiIsRefused` (11811 px/m, 300 dpi: `label_wrong_dpi`);
  `TestAnImageWiderThanTheHeadIsRefused` (833 x 10: `label_too_large`; 832 x 10 passes);
  `TestTooManyGraphicBytesAreRefused` (800 x 1001: 100 x 1001 bytes > 99999: `label_too_large`);
  `TestNotAPngIsRefused` (a JPEG's magic, empty bytes: `label_not_png`);
  `TestABadZipIsRefused` (an entry `a.txt`; a directory entry; a deflated entry of 9 MiB of
  zeros: `label_not_png`, `label_not_png`, `label_too_large`);
  `TestMoreThanAHundredLabelsAreRefused` (101 entries: `too_many_labels`).
- [ ] **Step 2: Run them to see them fail.** `cd tools/go/core && go test ./internal/labelprinter/`.
  Expected: FAIL (undefined: `Encode`, `Images`).
- [ ] **Step 3: Implement** the interfaces above. `pHYs` is read by a walk of the PNG chunks
  before `IDAT` (length, type, data, CRC); `image/png` does not expose it.
- [ ] **Step 4: Run them to see them pass.** `go vet ./... && go test -race ./internal/labelprinter/`.
  Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "feat(core): label images to ZPL graphics (#7170)"`.

### Task 3: The send route (Go)

**Files:**
- Create: `tools/go/core/internal/labelprinter/labelprinter.go`, `tools/go/core/internal/labelprinter/send.go`
- Modify: `tools/go/core/main.go` (`publicPackages` gains `labelprinter.Public`)
- Test: `tools/go/core/internal/labelprinter/send_test.go`, `tools/go/core/label_printer_test.go`
- Docs: `tools/go/core/README.md` (an area row and a short section)

**Interfaces:**
- Consumes: Task 2 `Images`, `Encode`, `Job`, `MaxBody`; Task 1's setting names.
- Produces, `send.go`:
  `type Sender struct { Dial func(ctx context.Context, network, addr string) (net.Conn, error); DialTimeout, WriteTimeout time.Duration; mu sync.Mutex }`;
  `func NewSender() *Sender` (a `net.Dialer`, 5 s, 30 s);
  `func (s *Sender) Send(ctx context.Context, addr string, job []byte) error`: holds `mu`,
  dials with `DialTimeout`, sets the write deadline to now + `WriteTimeout`, writes all,
  closes. It never reads. Any error is `ErrUnreachable` wrapped with the cause.
- Produces, `labelprinter.go`:
  `const Area = "label-printer"`, `const Path = "/api/v1/filament/label-printer/jobs"`;
  `func Public(pool *pgxpool.Pool, _ *seal.Keys) map[string][]auth.Public` (one route,
  `POST ` + `Path`, class `api`, `crud.Serve(handle, MaxBody)`);
  `type Sent struct { Labels int `json:"labels"`; Bytes int `json:"bytes"` }` (the 200 answer);
  `func printer(ctx context.Context, pool *pgxpool.Pool) (addr string, job Job, err error)`:
  one `SELECT name, value FROM settings WHERE name = ANY($1)` of the four names. A missing or
  `null` host, or one that fails the same rules as Python's `printer_host`, is 409
  `label_printer_unset`. The port defaults to 9100; media and darkness to the stored values,
  then `direct` and 10. `net.JoinHostPort` builds `addr`.
- The route: query `media` (`direct` | `transfer`) and `darkness` (0 to 30) outrank the stored
  values; another value is a 422 through `httpx.Invalid`. It reads the settings first (so an
  unset printer is 409 before the body is decoded), then `Images`, then `Encode`, then
  `Send`. A send error is 502 `label_printer_unreachable`, detail `"the label printer at
  <host>:<port> did not take the job"`. After a send it logs with `slog.InfoContext`:
  `"label printer job"`, `host`, `port`, `labels`, `bytes`, `caller` (`auth.CallerOf(ctx).Kind`).
  No image, no hex.

- [ ] **Step 1: Write the failing tests.** `send_test.go` (a helper `listen(t)` starts
  `net.Listen("tcp", "127.0.0.1:0")` and a goroutine that accepts and `io.ReadAll`s each
  connection into a channel):
  `TestSendWritesTheWholeJob` (the channel gets the exact bytes);
  `TestARefusedPortIsUnreachable` (listen, take the address, close the listener, send:
  `errors.Is(err, ErrUnreachable)`);
  `TestAPrinterThatNeverReadsTimesOut` (`Dial` returns one end of `net.Pipe()`, the other end
  is never read; `WriteTimeout` 1 ms; `ErrUnreachable`);
  `TestTwoSendsNeverInterleave` (two goroutines send a 1 MiB job of `A`s and one of `B`s;
  each received connection is all one letter and whole).
  `label_printer_test.go` (through `newAPI(t, "label-printer")`; it skips without Postgres as
  the other router tests do):
  `TestTheJobReachesTheStoredPrinter` (store host `127.0.0.1` and the fake listener's port
  with `a.sql`; POST a 2 x 1 in PNG; 200 `{"labels":1,"bytes":N}`; the listener got a job
  that starts `~SD10^XA^MTD^PW406^LL203`);
  `TestTheQueryOutranksTheStoredChoice` (stored `transfer`, query `media=direct&darkness=3`:
  `~SD03^XA^MTD`);
  `TestNoPrinterIsA409` (no host row: 409, code `label_printer_unset`; the body is not read);
  `TestABodyPastTheBoundIs413`;
  `TestAnUnreachablePrinterIsA502` (code `label_printer_unreachable`);
  `TestTheAreaOffGoesToPython` (`newAPI(t, "")`: the request reaches the upstream).
- [ ] **Step 2: Run them to see them fail.** `go test ./internal/labelprinter/`, then
  `go test -run 'TestTheJob|TestTheQuery|TestNoPrinter|TestABodyPast|TestAnUnreachable|TestTheAreaOff' .`
  with `SCADBUDDY_TEST_DATABASE_URL` set.
  Expected: FAIL (undefined: `Sender`, `Public`; unknown area).
- [ ] **Step 3: Implement** the interfaces above, and add `labelprinter.Public` to
  `publicPackages`. No `case` in `newRouter` is needed: the `default` case mounts a public area.
- [ ] **Step 4: Run them to see them pass.** `go vet ./... && go test -race ./...` in
  `tools/go/core`. Expected: PASS.
- [ ] **Step 5: Docs.** `tools/go/core/README.md`: an area row `label-printer` (the route, the
  body, the answer, the codes), and a short section: the address is the stored settings, which
  only a person sets; core never reads from the socket; one job at a time per process; the
  timeouts.
- [ ] **Step 6: Commit.** `git commit -m "feat(core): send label images to a network Zebra as ZPL (#7170)"`.

### Task 4: The client, the mocks and the Settings rows (frontend)

Load the `frontend-design:frontend-design` skill first.

**Files:**
- Create: `frontend/src/api/labelPrinter.ts`, `frontend/src/mocks/features/filament/labelPrinter.ts`
- Modify: `frontend/src/mocks/features/filament/index.ts` (add the handlers)
- Modify: `frontend/src/pages/settings/sections/filament.tsx` (a hand-laid `LabelPrinterRows`,
  as `PresetMirrorRow`)
- Test: `frontend/src/api/labelPrinter.test.ts`, `frontend/src/pages/settings/sections/filament.test.tsx`

**Interfaces:**
- Consumes: Task 3's route and codes; Task 1's view fields (from the generated `Settings`).
- Produces, `labelPrinter.ts` (hand-typed: Python's OpenAPI has no Go route, as
  `api/unknownTrays.ts`):
  `export type LabelMedia = 'direct' | 'transfer'`;
  `export interface LabelPrinterSent { labels: number; bytes: number }`;
  `export async function printLabelsToZebra(file: Blob, choice: { media: LabelMedia; darkness: number }): Promise<LabelPrinterSent>`
  (`requestRaw` with the blob as the body, `Content-Type` the blob's type, the query
  `media` and `darkness`);
  `export const labelPrinterMissing = (cause: unknown): boolean` (a 404 with no `code`: the
  area is off).
- Produces, mocks: `POST /api/v1/filament/label-printer/jobs` answers 409
  `label_printer_unset` when the mock settings' host is null, 422 `label_not_png` when the
  body does not start with the PNG or ZIP magic, else 200 `{labels, bytes}` (labels 1 for a
  PNG, the entry count for a ZIP), and records the last request for tests
  (`lastLabelPrinterJob()`).

- [ ] **Step 1: Write the failing tests.** `labelPrinter.test.ts`: `sends the bytes and the
  choice` (the mock gets the blob's bytes, `media=transfer&darkness=22`); `a 409 throws an
  ApiError with code label_printer_unset`; `a bare 404 is labelPrinterMissing`.
  `filament.test.tsx`: `saves the label printer host and port` (types `zebra.lan` and `9100`,
  Save sends both); `shows the API's message for a bad host`.
- [ ] **Step 2: Run them to see them fail.** `cd frontend && pnpm gen:api && pnpm exec vitest
  run src/api/labelPrinter.test.ts src/pages/settings/sections/filament.test.tsx`. Expected: FAIL.
- [ ] **Step 3: Implement** the interfaces above. The Settings rows read their label and help
  with `docOf` (Task 1's docs); the port is a number field.
- [ ] **Step 4: Run them to see them pass,** then `pnpm typecheck && pnpm lint`. Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "feat(filament): label printer client, mocks and settings rows (#7170)"`.

### Task 5: "Print to Zebra" on the Labels page (frontend)

Load the `frontend-design:frontend-design` skill first.

**Files:**
- Modify: `frontend/src/pages/filament/LabelsPage.tsx`
- Create (if the page grows past its shape): `frontend/src/components/filament/ZebraPrint.tsx`
- Test: `frontend/src/pages/filament/LabelsPage.test.tsx`
- Docs: `frontend/CLAUDE.md` (the labels lines), `docs/features/spools-and-filament.md`
  (labels: the Zebra send)

**Interfaces:**
- Consumes: Task 4 `printLabelsToZebra`, `labelPrinterMissing`, `problemCode` from
  `api/unknownTrays.ts`; `labelsFile` and the page's own body builder (the one Download uses);
  `useSettingSaver('filament_label_printer_media')`, `useSettingSaver('filament_label_printer_darkness')`.

- [ ] **Step 1: Write the failing tests** in `LabelsPage.test.tsx`:
  `hides Print to Zebra while no printer is set`;
  `sends the labels as 203 dpi PNG to the Zebra` (host set; click: the labels request has
  `format: 'png'`, `dpi: 203`; the job request carries its bytes; a status line "Sent 2 labels
  to the Zebra.");
  `saves the print mode and darkness at each change` (the switch "Thermal transfer (ribbon)"
  PUTs `filament_label_printer_media: 'transfer'`; darkness 22 PUTs 22; the next job's query
  has both);
  `disables Print to Zebra for a sheet` (an Avery template, or the `sheet` layout: disabled,
  with the reason "A Zebra prints roll labels, one at a time.");
  `shows the printer's problem` (502 `label_printer_unreachable`: its detail in an alert; 409
  `label_printer_unset`: "Set the label printer in Settings.");
  `says the server has no Zebra route` (a bare 404).
  Each test also runs for `?printers=` place labels once (`it.each` over the two modes).
- [ ] **Step 2: Run them to see them fail.** `pnpm exec vitest run src/pages/filament/LabelsPage.test.tsx`.
  Expected: FAIL.
- [ ] **Step 3: Implement.** "Print to Zebra" sits beside Print and Download, with the switch
  and a darkness number (0 to 30) next to it. The body is Download's body with `format: 'png'`
  and `dpi: 203`. While a job runs, the button is busy and a second click does nothing.
- [ ] **Step 4: Run them to see them pass,** then `pnpm typecheck && pnpm lint && pnpm exec
  vitest run src/pages/filament`. Expected: PASS.
- [ ] **Step 5: Docs.** `frontend/CLAUDE.md` and `docs/features/spools-and-filament.md`: the
  Zebra send, the settings, and that the page hides it with no printer set.
- [ ] **Step 6: Commit.** `git commit -m "feat(filament): print labels to a network Zebra from the Labels page (#7170)"`.

### Task 6: Open the clusters PR (after this PR merges)

**Files:**
- Modify (in eh-homelab/clusters): `applications/scadbuddy/scadbuddy.yaml`, the `core`
  container's `SCADBUDDY_CORE_AREAS` (add `label-printer`)

- [ ] **Step 1:** Wait until this PR is merged and its image is deployed: check the API pod's
  `core` image with `kubectl -n bambuddy get pods -o jsonpath` (memory: confirm a rollout by
  pod images).
- [ ] **Step 2:** In a branch of eh-homelab/clusters, add `label-printer` to the value. The
  body says that the API pod has no NetworkPolicy, so core reaches the LAN printer, and that
  eh-homelab/clusters#3182 must allow the printer's address on port 9100 when it lands.
- [ ] **Step 3:** Open the PR (`Refs eh-homelab/ScadBuddy#7170`). After it syncs, check that
  `POST /api/v1/filament/label-printer/jobs` with no printer set answers 409
  `label_printer_unset`, not 404.

## Order and review

Task 1 first (Task 3 reads its setting names). Task 2 before Task 3. Task 4 needs Tasks 1 and
3; Task 5 needs Task 4. Task 6 runs after the merge. The PR body says `Fixes #7170`.

## Self-review

- Spec coverage: R1 Task 3; R2 Tasks 1 and 3; R3 Task 1 (person-only, host rules) and Task 3
  (no address in a request, the check again, no read); R4 Tasks 2, 3 and 5; R5 Task 1 (template,
  media, darkness) and Task 5 (controls); R6 Tasks 2 and 3; R7 Tasks 4 and 5; R8 Tasks 2 and 3;
  the clusters change Task 6.
- Names: `filament_label_printer_{host,port,media,darkness}`, `label-printer`,
  `/api/v1/filament/label-printer/jobs`, `printLabelsToZebra`, `Images`, `Encode`, `Sender`
  are the same in every task.
