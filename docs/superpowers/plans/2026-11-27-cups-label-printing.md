# Print Labels Through CUPS Implementation Plan (#7170)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Labels page sends spool, place and printer labels to a queue of ScadBuddy's CUPS
label print server, with the Zebra ZPL options a person picks, and with no browser print dialog.

**Architecture:** The label layer (#7216) stays the one layout. The page asks
`POST /api/v1/filament/labels` for a PDF, then posts those bytes to a new Go route in
`tools/go/core` (area `label-printing`). Core reads the CUPS base URL from a stored setting
that only a person sets, checks the queue against CUPS's own list (`CUPS-Get-Printers`), and
sends the PDF with one IPP `Print-Job`. CUPS's queue rasterizes, checks the label size, orders
and retries. Python changes only its settings.

**Tech Stack:** Go (`tools/go/core`, standard library only: `net/http`, `encoding/binary`),
Python 3.12 and FastAPI (settings only), React, vitest, msw, CUPS 2.4 in CI.

**Spec:** `docs/superpowers/specs/2026-11-27-cups-label-printing-design.md` (Facts, Decisions
R1 to R10, "The routes"). Read it first.

## Global Constraints

- New routes are Go, in `tools/go/core` (owner, #7667). Python changes here are edits to its
  existing settings code only. Only the API and core hold SQL (root `CLAUDE.md` "Database
  access"). Core runs no untrusted code and never parses the PDF.
- A request never names a server or a URL. The base URL is the stored setting
  `filament_label_server_url`, default `http://cups.cups.svc:8631`. Only a person's own session
  changes or resets it (403 `person_only`). A request names only a queue, which core checks
  against CUPS's list before it prints.
- Remembered choices with a control (#6219): `filament_label_queue` (a queue name or null) and
  `filament_label_job_options` `{media_type, darkness, print_rate}`; `media_type` in
  `Saved | Thermal | Direct` (default `Saved`), `darkness` `-1` or `1`..`30` (default `-1`),
  `print_rate` `Default` or `1`..`12` (default `Default`).
- The document moves as a binary body (`application/pdf`), never base64 in JSON (#2479).
- Limits, exact: body at most 8 MiB (`8 << 20`); a CUPS reply read at most 1 MiB (`1 << 20`);
  queue name `^[A-Za-z0-9_-]{1,127}$`; `status-message` cut to 300 characters; size match within
  1 mm; list timeout 5 s, print timeout 30 s, both fields of the client.
- Problem reasons, exact: `label_server_unreachable` (502), `label_queue_unknown` (422),
  `label_print_refused` (502, with `ipp_status`), `label_not_pdf` (415),
  `label_option_invalid` (422); a body past the bound is 413.
- IPP job attributes: `job-name` `ScadBuddy labels`, `requesting-user-name` `scadbuddy`,
  `document-format` `application/pdf`; PPD options as `nameWithoutLanguage` with the names
  `MediaType`, `Darkness`, `zePrintRate`, sent only when not the default.
- Tests never reach the real CUPS: a fake IPP server on `httptest.Server`. A timeout test uses
  a handler that never answers and a client timeout set by the test. No test sleeps or reads
  the date.
- Locally: lint, types and the focused tests only (`ruff`, `mypy`, `go vet`, focused
  `go test`, `pnpm typecheck`, `pnpm lint`, focused vitest). CI runs the rest.
- Simplified Technical English in docs and comments. No counts in docs. Cite a link for an
  external fact; say how a measured fact was measured.
- Commits are new commits, never amends. Title form `feat(filament): … (#7170)`; trailers as in
  the repo's attribution rule.

## Review Focus

1. **CUPS is down or the URL is wrong.** The list and the print answer 502
   `label_server_unreachable` within the timeout. The page says the label server does not
   answer and keeps Print and Download. Test: Task 4 `TestQueuesWhenCUPSIsDown` (a closed
   listener) and `TestQueuesTimeOut` (a handler that never answers).
2. **A queue is removed between the list and the print.** CUPS answers 0x0406 to `Print-Job`.
   The route answers 422 `label_queue_unknown`, and the page reads the queues again. Test:
   Task 3 `TestPrintQueueGoneIsUnknown`, Task 6 `refreshes the queues after label_queue_unknown`.
3. **A job that CUPS holds for the right labels.** `Print-Job` succeeds with `job-state`
   processing while the backend waits. The route answers 202, and the page says "Sent" with the
   queue's message, not "Printed". Test: Task 4 `TestPrintAnswersAcceptedNotPrinted`, Task 6
   `says sent, with the queue's message`.
4. **A sheet on a 2 x 1 queue.** The page offers no 2 x 1 queue for a Letter or A4 page, and no
   4 x 6 queue for a 2 x 1 label. Test: Task 6 `fits` unit tests and
   `offers no label queue for a sheet`.
5. **A slow CUPS reply, or a stopped queue.** A stopped queue that does not accept jobs answers
   0x0506; the route answers 502 `label_print_refused` with CUPS's message. A reply that is
   slow past 30 s is 502 `label_server_unreachable`; core holds no lock while it waits. Test:
   Task 3 `TestPrintRefusedCarriesTheMessage`, Task 4 `TestPrintTimesOut`.

---

### Task 1: The settings: a CUPS server URL, a queue and job options

Replaces the four `filament_label_printer_*` settings of commit f24b15e0c with three. Keeps the
template `roll_2x1in` and its PNG test. f24b15e0c was never on main, so no stored row needs a
move. The uncommitted edits left in the worktree by the stopped fix round
(`backend/CLAUDE.md`, `backend/tests/filament/test_label_printer.py`,
`frontend/src/mocks/features/settings.ts`) are dropped: step 1 runs `git checkout --` on them.

**Files:**
- Rename and rewrite: `backend/scadbuddy/filament/label_printer.py` to
  `backend/scadbuddy/filament/label_printing.py` (`git mv`)
- Delete: `backend/tests/filament/test_label_printer.py`
- Create: `backend/tests/filament/test_label_printing.py`
- Rename and rewrite: `backend/tests/api/test_label_printer_settings.py` to
  `backend/tests/api/test_label_printing_settings.py`
- Modify: `backend/scadbuddy/api/settings.py` (`SettingsView`, `_view`, `LABEL_PRINTER`,
  `_label_printer_by_a_person`), `backend/scadbuddy/library/settings_store.py`
  (`StoredSettings`, `SettingsPatch`, the validator), `backend/scadbuddy/core/setting_docs.py`,
  `backend/CLAUDE.md` (the #7170 sentence), `frontend/src/mocks/fixtures.ts`,
  `frontend/src/mocks/features/settings.ts`

**Interfaces:**
- Produces (Python): `DEFAULT_SERVER_URL: Final = "http://cups.cups.svc:8631"`;
  `server_url(value: str) -> str` (trimmed, a trailing `/` removed; `ValueError` otherwise);
  `class LabelJobOptions(BaseModel)` with `media_type: Literal["Saved", "Thermal", "Direct"] =
  "Saved"`, `darkness: int = -1` (`-1` or 1..30), `print_rate: Literal["Default", "1", …, "12"]
  = "Default"`, `extra="forbid"`.
- Produces (settings, read by name by Task 4 and Task 6): `filament_label_server_url`
  (view: `str`, the default when unset), `filament_label_queue` (`str | None`),
  `filament_label_job_options` (view: `LabelJobOptions`, the defaults when unset).
  `LABEL_PRINTER = frozenset({"filament_label_server_url"})`.

- [ ] **Step 1: Drop the stopped round's edits**

Run: `git checkout -- backend/CLAUDE.md backend/tests/filament/test_label_printer.py frontend/src/mocks/features/settings.ts && git status --short`
Expected: no ` M ` line for those three files.

- [ ] **Step 2: Write the failing tests**

`tests/filament/test_label_printing.py`:

```python
@pytest.mark.parametrize("value", ["http://cups.cups.svc:8631", "https://cups.internal.nullreference.io", "http://10.0.0.5:631/"])
def test_a_server_url_is_http_with_a_host(value: str) -> None:
    assert server_url(value) == value.rstrip("/")

@pytest.mark.parametrize("value", ["", "cups:631", "ftp://cups", "http://", "http://u:p@cups",
    "http://cups/printers", "http://cups?x=1", "http://cups#f", "javascript:alert(1)"])
def test_other_values_are_refused(value: str) -> None:
    with pytest.raises(ValueError):
        server_url(value)

@pytest.mark.parametrize("darkness", [0, 31, -2])
def test_darkness_is_minus_one_or_1_to_30(darkness: int) -> None:
    with pytest.raises(ValidationError):
        LabelJobOptions(darkness=darkness)
```

`tests/api/test_label_printing_settings.py` (keep the `AGENT` header and the `client` fixture
of the old file):

- `test_the_defaults`: the view has `filament_label_server_url == "http://cups.cups.svc:8631"`,
  `filament_label_queue is None`, `filament_label_job_options == {"media_type": "Saved",
  "darkness": -1, "print_rate": "Default"}`.
- `test_a_person_sets_and_resets_the_server`: PUT `https://cups.example.lan/` is 200 and reads
  back `https://cups.example.lan`; PUT `null` reads back the default.
- `test_an_agent_cannot_move_the_server` (params `{"filament_label_server_url": "http://other"}`,
  `{"filament_label_server_url": None}`, `{"reset": ["filament_label_server_url"]}`): 403,
  `type` ends with `person_only`, the stored value is unchanged.
- `test_an_agent_may_pick_the_queue_and_options`: with `AGENT`, PUT `filament_label_queue:
  "S-EDCDU03-2x1"` and `filament_label_job_options: {"media_type": "Thermal", "darkness": 20,
  "print_rate": "4"}` is 200 and reads back.
- `test_bad_values_are_a_422` (params: a URL with a path, `darkness` 31, `media_type` `"Ribbon"`,
  `print_rate` `"13"`, an extra key in the options): 422.

- [ ] **Step 3: Run the tests to see them fail**

Run: `cd backend && uv run pytest tests/filament/test_label_printing.py tests/api/test_label_printing_settings.py -q`
Expected: FAIL (`label_printing` not found). The API tests need Postgres; locally they may skip,
and CI runs them.

- [ ] **Step 4: Implement**

`git mv` the two files, then rewrite. `server_url` uses `urllib.parse.urlsplit`: scheme `http` or
`https`, a `hostname`, no `username`/`password`, no `query`, no `fragment`, path `""` or `"/"`.
`SettingsPatch.filament_label_server_url` gets a `field_validator` that calls it;
`filament_label_queue: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,127}$")`;
`filament_label_job_options: LabelJobOptions | None = None`. The view fills the defaults. Put the
`print_rate` literal as the 13 strings in one `Literal`. Remove every `filament_label_printer_*`
name, `printer_host`, `DEFAULT_PORT`, `DEFAULT_DARKNESS`, `MediaType`. `LABEL_PRINTER`'s message:
"Only a person changes the label print server: …". Setting docs:
- `filament_label_server_url`: "Label print server", "The CUPS server that the Labels page
  prints to, as an http or https URL. The default is the cluster's own server,
  http://cups.cups.svc:8631. Only a person's session changes it."
- `filament_label_queue`: "Label queue", "The CUPS queue that the Labels page printed to last.
  The Labels page saves it."
- `filament_label_job_options`: "Label print options", "Thermal transfer or direct thermal,
  darkness and print speed for a Zebra ZPL queue. Printer default leaves the printer's own
  setting. The Labels page saves the last choice."

The mocks: fixtures get the three defaults; `putSettings` in `mocks/features/settings.ts` puts
back the fixture default for `null` on `filament_label_server_url` and
`filament_label_job_options`. `backend/CLAUDE.md`: replace the `label_printer.py (#7170) …`
clause with one sentence naming `label_printing.py`, the three settings, `LABEL_PRINTER` and Go's
`internal/labelprint`.

- [ ] **Step 5: Run the tests and the checks**

Run: `cd backend && uv run pytest tests/filament/test_label_printing.py tests/api/test_label_printing_settings.py tests/api/test_filament_label_pdf.py -q && uv run ruff check . && uv run ruff format --check . && uv run mypy scadbuddy`
Expected: PASS (API tests may skip without Postgres); ruff and mypy clean.
Run: `git grep -n "filament_label_printer_\|label_printer\.py\|printer_host" -- . ':!docs/superpowers'`
Expected: no line.

- [ ] **Step 6: Commit**

`git add -A backend frontend/src/mocks` then commit
`feat(filament): label settings for a CUPS server, a queue and job options (#7170)`.

---

### Task 2: The IPP codec

**Files:**
- Create: `tools/go/core/internal/labelprint/ipp.go`, `tools/go/core/internal/labelprint/ipp_test.go`

**Interfaces:**
- Produces:

```go
type Tag byte // value and delimiter tags, RFC 8010 §3.5
const (TagOperation Tag = 0x01; TagJob Tag = 0x02; TagEnd Tag = 0x03; TagPrinter Tag = 0x04
  TagInteger Tag = 0x21; TagBoolean Tag = 0x22; TagEnum Tag = 0x23; TagText Tag = 0x41
  TagName Tag = 0x42; TagKeyword Tag = 0x44; TagURI Tag = 0x45; TagCharset Tag = 0x47
  TagLanguage Tag = 0x48; TagMimeType Tag = 0x49)
type Attr struct { Group Tag; Name string; Tag Tag; Values [][]byte } // one attribute, all its values
type Message struct { Version [2]byte; Code uint16; RequestID uint32; Attrs []Attr; Data []byte }
func (m Message) Encode(w io.Writer) error           // attributes in order, groups opened as Attrs change Group
func Decode(r io.Reader, limit int64) (Message, error) // ErrNotIPP for a short or bad stream
func (m Message) Strings(group Tag, name string) []string
func (m Message) Int(group Tag, name string) (int32, bool)
func (m Message) Groups(group Tag) [][]Attr           // one slice per group of that tag (a printer each)
func StrAttr(group Tag, name string, tag Tag, values ...string) Attr
func IntAttr(group Tag, name string, tag Tag, v int32) Attr
var ErrNotIPP = errors.New("not an IPP message")
```

The decoder keeps a collection's member records (tags 0x34, 0x4A, 0x37) as plain records and
never nests them; `Strings` and `Groups` skip them. `Data` is the bytes after the end tag (the
document in a request; empty in a reply).

- [ ] **Step 1: Write the failing tests**

- `TestEncodeMatchesRFC8010PrintJob`: the Print-Job request of RFC 8010 Appendix A.1 (version
  1.1, request id 1, `attributes-charset` `utf-8`, `attributes-natural-language` `en-us`,
  `printer-uri` `ipp://printer.example.com/ipp/print/pinetree`, `job-name` `foobar`,
  `ipp-attribute-fidelity` true, job group `copies` 20, `sides` `two-sided-long-edge`, then
  data `%!PDF...`) encodes to the appendix's bytes, copied into the test as a hex literal.
- `TestDecodeRFC8010PrintJobResponse`: Appendix A.2's bytes decode to status 0, `job-id` 147,
  `job-state` 3.
- `TestDecodeSkipsCollections`: Appendix A.7's request bytes (a `media-col` collection) decode
  with no error, `Strings(TagOperation, "printer-uri")` is
  `["ipp://printer.example.com/ipp/print/pinetree"]`, and `Strings(TagJob, "media-col")` is
  empty (a collection is not a string).
- `TestDecodeRefusesShortAndOverLimit`: a 7-byte stream, a value length past the end, and a
  stream past `limit` are `ErrNotIPP`.
- `TestGroupsSplitsPrinters`: two printer groups with `printer-name` `a` and `b` give two slices.

- [ ] **Step 2: Run them to see them fail**

Run: `cd tools/go/core && go test ./internal/labelprint/ -run 'Encode|Decode|Groups' -v`
Expected: FAIL (package has no Go files or undefined names).

- [ ] **Step 3: Implement `ipp.go`**

Big-endian `encoding/binary`. A value of a second and later value of one attribute has a zero
name length (RFC 8010 §3.1.5). `Decode` reads through an `io.LimitReader(r, limit+1)` and
fails past `limit`.

- [ ] **Step 4: Run them to see them pass**

Run: `cd tools/go/core && go vet ./internal/labelprint/ && go test -race ./internal/labelprint/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

`feat(core): an IPP codec for CUPS-Get-Printers and Print-Job (#7170)`.

---

### Task 3: The CUPS client

**Files:**
- Create: `tools/go/core/internal/labelprint/cups.go`, `tools/go/core/internal/labelprint/cups_test.go`,
  `tools/go/core/internal/labelprint/fakecups_test.go` (the fake server, shared with Task 4)

**Interfaces:**
- Consumes: Task 2's `Message`, `Encode`, `Decode`, `StrAttr`, `IntAttr`, `Groups`.
- Produces:

```go
type Queue struct {
  Name, Info, MakeAndModel, State, StateMessage, Media string
  StateReasons []string; Accepting bool; WidthMM, HeightMM *float64
} // JSON: name, info, make_and_model, state, state_message, media, state_reasons, accepting, width_mm, height_mm
type Options struct{ MediaType, Darkness, PrintRate string } // PPD choice names; "" is the default
type Job struct{ Queue string; ID int32; State string; StateReasons []string } // JSON: queue, job_id, job_state, job_state_reasons
type Client struct{ Base *url.URL; HTTP *http.Client; ListTimeout, PrintTimeout time.Duration }
func (c *Client) Queues(ctx context.Context) ([]Queue, error)
func (c *Client) Print(ctx context.Context, queue string, doc []byte, o Options) (Job, error)
func SizeOf(media string) (w, h float64, ok bool) // PWG self-describing name's last part, in mm
var ErrUnreachable = errors.New("label server unreachable")
var ErrQueueUnknown = errors.New("label queue unknown")
type RefusedError struct{ Status uint16; Message string } // Error() is Message
```

`Queues` posts `CUPS-Get-Printers` (0x4002) to `Base` + `/` with `requested-attributes`
`printer-name`, `printer-info`, `printer-make-and-model`, `printer-state`,
`printer-state-reasons`, `printer-state-message`, `printer-is-accepting-jobs`, `media-default`.
`printer-state` 3, 4, 5 is `idle`, `processing`, `stopped`. `job-state` 3 to 9 is `pending`,
`pending-held`, `processing`, `processing-stopped`, `canceled`, `aborted`, `completed`
([RFC 8011 §5.3.7](https://www.rfc-editor.org/rfc/rfc8011#section-5.3.7)). `Print` posts `Print-Job` (0x0002)
to `Base` + `/printers/<queue>` (path-escaped) with `printer-uri`
`ipp://<Base host>/printers/<queue>`, the job attributes of the Global Constraints and the
document as `Data`. `Content-Type: application/ipp` on both. A transport error, a deadline, an
HTTP status other than 200, a redirect (`CheckRedirect` returns `http.ErrUseLastResponse`) or
`ErrNotIPP` is `ErrUnreachable` (wrapped). Status 0x0406 is `ErrQueueUnknown`; any other status
above 0x00FF is `*RefusedError` with `status-message`. Each call runs under its own timeout.
`SizeOf` parses the last `_`-part `<w>x<h>in|mm` (`in` times 25.4).

- [ ] **Step 1: Write the fake server and the failing tests**

`fakecups_test.go`: `newFakeCUPS(t, queues []fakeQueue) *fakeCUPS` on `httptest.NewServer`; it
decodes each request with `Decode`, records it (`f.requests`), and answers
`CUPS-Get-Printers` with one printer group per queue and `Print-Job` with `job-id` 7,
`job-state` 5 (processing), `job-state-reasons` `job-printing`. Fields let a test set the status
and message of the next `Print-Job` reply, and `block chan struct{}` makes a handler wait until
the test ends (closed in `t.Cleanup`).

`cups_test.go`:
- `TestQueuesReadsCUPSList`: two fake queues (`S-EDCDU03-2x1`, media `oe_2x1-label_2x1in`, state
  3; `S-EDCDU03-4x6`, media `oe_4x6-label_4x6in`, state 4 with reason `media-needed` and message
  "1.02 in labels are loaded; this queue prints 6.00 in labels. …") come back in order, with
  `WidthMM` 50.8 and `HeightMM` 25.4 for the first, and the request asked for every attribute
  above.
- `TestSizeOf`: `oe_2x1-label_2x1in` is (50.8, 25.4); `na_letter_8.5x11in` is (215.9, 279.4);
  `iso_a4_210x297mm` is (210, 297); `custom` and `x_1xin` are not ok.
- `TestPrintSendsThePDFAndOptions`: `Print(ctx, "S-EDCDU03-2x1", pdf, Options{MediaType:
  "Thermal", Darkness: "20"})` sends op 0x0002 to `/printers/S-EDCDU03-2x1`, `printer-uri`
  `ipp://<host>/printers/S-EDCDU03-2x1`, `document-format` `application/pdf`, `job-name`
  `ScadBuddy labels`, `requesting-user-name` `scadbuddy`, job group `MediaType` `Thermal` and
  `Darkness` `20` as `TagName`, no `zePrintRate`, and `Data` equal to the PDF. The answer is
  `Job{Queue: "S-EDCDU03-2x1", ID: 7, State: "processing", StateReasons: ["job-printing"]}`.
- `TestPrintQueueGoneIsUnknown`: the fake answers 0x0406; `errors.Is(err, ErrQueueUnknown)`.
- `TestPrintRefusedCarriesTheMessage`: the fake answers 0x0506 with "Destination is not
  accepting jobs."; `err` is a `*RefusedError` with that status and message.
- `TestNotIPPIsUnreachable`: a server that answers `text/html` 200 `<html>` and one that
  answers 302 to another `httptest.Server` both give `ErrUnreachable`, and the second server
  gets no request.
- `TestReplyPastTheLimitIsUnreachable`: a 2 MiB reply is `ErrUnreachable`.

- [ ] **Step 2: Run them to see them fail**

Run: `cd tools/go/core && go test ./internal/labelprint/ -run 'Queues|SizeOf|Print|NotIPP|Limit' -v`
Expected: FAIL (undefined `Client`).

- [ ] **Step 3: Implement `cups.go`**

- [ ] **Step 4: Run them to see them pass**

Run: `cd tools/go/core && go vet ./internal/labelprint/ && go test -race ./internal/labelprint/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

`feat(core): a CUPS client that lists queues and sends a Print-Job (#7170)`.

---

### Task 4: The `label-printing` routes

**Files:**
- Create: `tools/go/core/internal/labelprint/routes.go`, `tools/go/core/internal/labelprint/routes_test.go`
- Modify: `tools/go/core/main.go` (a `case labelprint.Area` in `newRouter`), `tools/go/core/README.md`
  (the area, its routes and errors), `tools/go/core/main_test.go` or `public_test.go` (the area
  mounts), `tools/go/CLAUDE.md` (the layout line names `internal/labelprint`)

**Interfaces:**
- Consumes: Task 3's `Client`, `Queue`, `Job`, `Options`, the errors; Task 1's setting
  `filament_label_server_url` (a JSON string row in `settings`; no row or JSON `null` is the
  default).
- Produces:

```go
const Area = "label-printing"
const DefaultServer = "http://cups.cups.svc:8631"
const MaxBody = 8 << 20
type Service struct {
  Pool *pgxpool.Pool
  Server func(ctx context.Context) (*url.URL, error) // the stored setting, checked; nil means read Pool
  HTTP *http.Client; ListTimeout, PrintTimeout time.Duration
}
func New(pool *pgxpool.Pool, tel *telemetry.Telemetry) *Service // client: tel.Transport(httpx.Outside(nil), "cups"), 5 s and 30 s
func (s *Service) Public() []auth.Public // the two routes, class "api"
func ParseServer(raw string) (*url.URL, error) // the rule of design R3
```

Routes (design "The routes"): `GET /api/v1/label-printing/queues` answers `{queues}`.
`POST /api/v1/label-printing/queues/{queue}/jobs` reads at most `MaxBody` (`http.MaxBytesReader`,
413 past it), checks the `%PDF-` prefix (415 `label_not_pdf`), the query (`media_type`,
`darkness`, `print_rate` against design R6's lists; 422 `label_option_invalid`; `Saved`, `-1` and
`Default` map to `""`), the name pattern (422 `label_queue_unknown`), then lists the queues and
checks the name is in the list (422 `label_queue_unknown`), then prints and answers 202 with the
`Job`. Errors map to design R8. It logs `slog.Info("label job", "queue", …, "job_id", …, "bytes",
…, "caller", auth.CallerOf(ctx).Kind)`. The `case` in `newRouter` mounts `Public()` on the gate
as `filament-presets` does (its own case, because it needs `tel`).

- [ ] **Step 1: Write the failing tests**

`routes_test.go` serves `Public()` on a mux behind `auth.Off` (as `scan_test.go` does) with
`Server` returning the fake's URL and short timeouts set by the test:
- `TestQueuesRoute`: 200 and the two queues as JSON, with `width_mm` 50.8.
- `TestPrintRoute`: a body `%PDF-1.7 …` with `?media_type=Direct&darkness=-1` is 202
  `{queue, job_id: 7, job_state: "processing", job_state_reasons}`; the fake got `MediaType`
  `Direct` and no `Darkness`.
- `TestPrintAnswersAcceptedNotPrinted`: the fake answers `job-state` 4 (`pending-held`) with
  reason `job-hold-until-specified`; the route still answers 202, `job_state` `pending-held`.
- `TestUnknownQueueIsRefusedBeforePrint`: queue `Other` (not in the list) is 422
  `label_queue_unknown` and the fake got no `Print-Job`; queue `a/b` and a 128-character name are
  422 with no request to the fake at all.
- `TestNotAPDFIs415`, `TestBodyPastTheLimitIs413` (`MaxBody + 1` bytes), `TestBadOptionIs422`
  (`darkness=31`, `media_type=Ribbon`, `print_rate=13`).
- `TestQueuesWhenCUPSIsDown`: `Server` points at a listener the test closed; 502
  `label_server_unreachable`.
- `TestQueuesTimeOut` and `TestPrintTimesOut`: the fake's `block` never closes before the
  answer; `ListTimeout`/`PrintTimeout` 50 ms; 502 `label_server_unreachable`.
- `TestPrintRefusedIs502`: the fake answers 0x0506 "Destination is not accepting jobs."; 502
  `label_print_refused`, `ipp_status` 1286, `detail` that message.
- `TestParseServer`: the cases of Task 1's `server_url` test, the same answers.
- `TestServerSetting` (needs `SCADBUDDY_TEST_DATABASE_URL`, `testdb`): no row is
  `DefaultServer`; a row `"https://cups.example.lan"` is that; a row `"cups:631"` is an error and
  the route answers 502.
- In `public_test.go` (or `main_test.go`): naming `label-printing` in `SCADBUDDY_CORE_AREAS`
  serves `GET /api/v1/label-printing/queues` from Go, and without it the path goes on to Python.

- [ ] **Step 2: Run them to see them fail**

Run: `cd tools/go/core && go test ./internal/labelprint/ -run 'Route|Queue|Print|Body|PDF|Option|Server' -v`
Expected: FAIL.

- [ ] **Step 3: Implement `routes.go`, the `case`, and the README section**

The README section "Label printing (#7170)" gives the setting, the two routes in a table, the
problems, and one line: CUPS owns the size check, the order and the retries.

- [ ] **Step 4: Run them to see them pass**

Run: `cd tools/go/core && gofmt -l . && go vet ./... && go test -race ./internal/labelprint/ . -run 'Label|label|Public' -v`
Expected: no gofmt output; PASS (database tests skip without the URL).

- [ ] **Step 5: Commit**

`feat(core): label-printing routes send a label PDF to a CUPS queue (#7170)`.

---

### Task 5: A live check against the CUPS image in CI

The fake server proves core's side. This check proves CUPS's side: the `media-default` name for
`w144h72`, and that a job attribute `Darkness` and `MediaType` reaches the ZPL.

**Files:**
- Create: `tools/go/core/internal/labelprint/live_test.go`
- Modify: `.github/workflows/ci.yml` (`cups-image`, one step after "CUPS smoke", and the job's
  comment), `deploy/cups/README.md` (a paragraph: ScadBuddy prints over IPP from core,
  `tools/go/core/README.md` "Label printing")

**Interfaces:**
- Consumes: Task 3's `Client`.

- [ ] **Step 1: Write the test**

`TestLiveCUPS` skips unless `SCADBUDDY_TEST_CUPS_URL` is set. It reads the PDF at
`SCADBUDDY_TEST_CUPS_PDF`. It lists the queues and asserts that `S-EDCDU03-2x1` has `WidthMM`
and `HeightMM` within 1 mm of 50.8 and 25.4, and `MakeAndModel` `Zebra ZPL Label Printer`. It
prints the PDF to `S-EDCDU03-2x1` with `Options{MediaType: "Thermal", Darkness: "30"}` and asserts
a job id above 0. A second print to `Nope` is `ErrQueueUnknown`. It logs each queue's `Media`
with `t.Logf`, so the run records the measured name.

- [ ] **Step 2: Add the CI step**

After "CUPS smoke" in `cups-image` (the `cups` and `fakezebra` containers and network `cupsci`
still run; the 2 x 1 labels are loaded, `len` is `0208`):

```bash
img=$(sed -n 's/^FROM --platform=\$BUILDPLATFORM \(golang:[^ ]*\) AS build$/\1/p' tools/go/core/Dockerfile)
docker run --rm --entrypoint sh scadbuddy-cups:ci -c \
  'printf "%%!PS\n/Helvetica findfont 14 scalefont setfont 10 30 moveto (ScadBuddy) show showpage\n" > /tmp/t.ps && gs -q -sDEVICE=pdfwrite -o - /tmp/t.ps' \
  > "$RUNNER_TEMP/live.pdf"
before=$(grep -c '\^XZ' "$fz/out.zpl")
docker run --rm --network cupsci -v "$PWD:/src" -v "$RUNNER_TEMP/live.pdf:/live.pdf:ro" -w /src/tools/go/core \
  -e GOTOOLCHAIN=local -e SCADBUDDY_TEST_CUPS_URL=http://cups:8631 \
  -e SCADBUDDY_TEST_CUPS_PDF=/live.pdf "$img" go test ./internal/labelprint/ -run TestLiveCUPS -v
for _ in $(seq 30); do [ "$(grep -c '\^XZ' "$fz/out.zpl")" -gt "$before" ] && break; sleep 1; done
tail -c 2000 "$fz/out.zpl" | grep -q '~SD30' && tail -c 2000 "$fz/out.zpl" | grep -q '\^MTT' \
  || { docker logs cups; tail -c 2000 "$fz/out.zpl"; exit 1; }
```

The golang image comes from core's Dockerfile, so this adds no second toolchain pin. The build
uses the root `vendor/` through `go.work`, offline, as the `go` job does. The wait is a poll on
the fake Zebra's file, as the smoke's are, not a fixed delay.

- [ ] **Step 3: Check it locally where Docker allows, else in CI**

Run: `cd tools/go/core && go vet ./internal/labelprint/ && go test ./internal/labelprint/ -run TestLiveCUPS -v`
Expected: `SKIP` without the variable. Run `actionlint .github/workflows/ci.yml`; expected clean.
The step itself runs in CI (the `image` lane selects `cups-image` for a `tools/go/` change).

- [ ] **Step 4: Commit**

`ci(cups): check core's IPP client against the CUPS image (#7170)`.

---

### Task 6: The Labels page prints to a CUPS queue; the Settings row

Load the `frontend-design:frontend-design` skill before the UI work.

**Files:**
- Create: `frontend/src/api/labelPrinting.ts`, `frontend/src/api/labelPrinting.test.ts`,
  `frontend/src/mocks/features/labelPrinting.ts`,
  `frontend/src/pages/filament/LabelQueuePrint.tsx` (the control), its test
  `frontend/src/pages/filament/LabelQueuePrint.test.tsx`
- Modify: `frontend/src/pages/filament/LabelsPage.tsx` (mount the control beside Print and
  Download; pass the request body and the PDF page size), `frontend/src/pages/settings/fields.ts`
  (`filament_label_server_url`, section `filament`, kind `url`),
  `frontend/src/pages/settings/sections/filament.test.tsx`, `docs/features/spools-and-filament.md`
  (one paragraph: labels print to a CUPS queue)

**Interfaces:**
- Consumes: Task 4's routes and JSON; Task 1's settings.
- Produces (`api/labelPrinting.ts`, hand-typed as `unknownTrays.ts` is):

```ts
export interface LabelQueue { name: string; info: string; make_and_model: string; state: 'idle' | 'processing' | 'stopped'
  state_reasons: string[]; state_message: string; accepting: boolean; media: string; width_mm: number | null; height_mm: number | null }
export interface LabelJobOptions { media_type: 'Saved' | 'Thermal' | 'Direct'; darkness: number; print_rate: string }
export interface LabelJob { queue: string; job_id: number; job_state: string; job_state_reasons: string[] }
export const ZEBRA_ZPL = 'Zebra ZPL Label Printer'
export function labelQueues(): Promise<LabelQueue[]>
export function printLabels(queue: string, pdf: Blob, options: LabelJobOptions): Promise<LabelJob>
export function pageSizeOf(choice: LabelTemplateChoice, layout: Layout, templates: LabelTemplates | null): { widthMm: number; heightMm: number } | null
export function fits(queue: LabelQueue, page: { widthMm: number; heightMm: number } | null): boolean // within 1 mm each way, same orientation
```

`printLabels` posts the blob as the body with `Content-Type: application/pdf` and the options
as the query (a default option is left out). `pageSizeOf` is the template's size for `roll` with
a one-label template, else the sheet's page (Letter 215.9 x 279.4, A4 210 x 297); null while
the templates are not read. The control asks `labelsFile({...body, format: 'pdf'})`, then
`printLabels`. It saves the queue (`filament_label_queue`) and each option change
(`filament_label_job_options`) with `useSettingSaver`. It pre-picks the stored queue when it
fits, else the only fitting queue, else none. It shows each queue's `info`, state and
`state_message`; the options show only for `make_and_model === ZEBRA_ZPL` (Print mode:
Printer default, Direct thermal, Thermal transfer; Darkness: Printer default, 1..30; Speed:
Printer default, 1..12 in/s). After a 202 it says "Sent to <info> (job <id>)" and the queue's
message when the queue is not idle. The mock: a 2 x 1 queue (idle) and a 4 x 6 queue
(`processing`, `media-needed`, the zebra backend's message), and `POST …/jobs` answers 202, 422
`label_queue_unknown` for another name, and 415 for a body that is not a PDF.

- [ ] **Step 1: Write the failing tests**

`labelPrinting.test.ts`: `fits` is true for a 50.8 x 25.4 queue and a 50 x 25 page, false for a
25.4 x 50.8 page, false for a Letter page, false for a null size; `pageSizeOf` gives Letter for a
saved Letter sheet and for `layout: 'sheet'`; `printLabels` sends the blob and
`?media_type=Thermal&darkness=20` and leaves out `print_rate=Default`.

`LabelQueuePrint.test.tsx` (msw mock):
- `lists the queues and pre-picks the stored one`;
- `offers no label queue for a sheet` (a Letter page: the 2 x 1 and 4 x 6 queues are not
  offered, and the control says no queue fits this page);
- `shows the options only for a Zebra ZPL queue`;
- `saves the queue and each option`;
- `says sent, with the queue's message` (the 4 x 6 queue's message shows after a 202);
- `refreshes the queues after label_queue_unknown`;
- `says the label server does not answer` (a 502 `label_server_unreachable` for the list; Print
  and Download stay).

`filament.test.tsx`: the Filament section shows the "Label print server" row.

- [ ] **Step 2: Run them to see them fail**

Run: `cd frontend && pnpm gen:api && pnpm exec vitest run src/api/labelPrinting.test.ts src/pages/filament/LabelQueuePrint.test.tsx src/pages/settings/sections/filament.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Implement the module, the mock, the control, the page mount and the row**

- [ ] **Step 4: Run them to see them pass, and the checks**

Run: `cd frontend && pnpm exec vitest run src/api/labelPrinting.test.ts src/pages/filament src/pages/settings/sections/filament.test.tsx && pnpm typecheck && pnpm lint`
Expected: PASS; clean.

- [ ] **Step 5: Commit**

`feat(filament): print labels to a CUPS queue from the Labels page (#7170)`.

---

### Task 7: After merge: turn the area on in eh-homelab/clusters

**Files (eh-homelab/clusters):**
- Modify: `applications/scadbuddy/scadbuddy.yaml` (`SCADBUDDY_CORE_AREAS`: add `label-printing`)

- [ ] **Step 1: Check the deployed image has the area**

Read the API pod's images (`kubectl -n bambuddy get deploy scadbuddy -o jsonpath=…`, read only)
and confirm the core image's tag is a main build at or after this PR's merge commit.

- [ ] **Step 2: Open the PR**

Branch, add `label-printing` at the end of the comma list, commit
`feat(scadbuddy): serve label-printing from core (eh-homelab/ScadBuddy#7170)`, and open the PR.
Its body says: no NetworkPolicy change is needed (the API pod has no egress policy and the
`cups` namespace has none; design "Reachability"). Comment on eh-homelab/clusters#3182: the API
pod needs egress to `cups/cups` port 8631 when its egress closes.

- [ ] **Step 3: After the sync, check it**

From a signed-in browser on the Labels page, the queue list shows `S-EDCDU03-2x1` and
`S-EDCDU03-4x6`. Print one 2 x 1 spool label. Record the job id and the label in #7170.

---

## Self-review

1. **Spec coverage.** R1: Tasks 3, 4. R2: Task 1 (settings only), Task 4 (prefix check only).
   R3: Task 1 (person-only, URL rule), Task 4 (`ParseServer`, list check), Task 3 (no redirect,
   1 MiB). R4: Task 6 (PDF from the label layer, binary body), Task 4 (`%PDF-`). R5: Task 2,
   Task 3, Task 5. R6: Task 1 (stored), Task 4 (lists), Task 6 (control, Zebra only). R7:
   Task 6 (`pageSizeOf`, `fits`). R8: Tasks 3, 4. R9: Task 6. R10: Task 7. "The routes": Task 4.
2. **Step scan.** Each code step names its file, signature and pinned values; each test step
   names its tests and assertions. Task 4's `TestPrintAnswersAcceptedNotPrinted` uses job state
   4 (`pending-held`, RFC 8011 §5.3.7) with `job-hold-until-specified`.
3. **Type consistency.** `Queue`, `Job`, `Options` (Task 3) are the JSON that Task 6's
   `LabelQueue`, `LabelJob` read; `Options` fields are PPD choice names, and Task 6 sends design
   R6's names. `ErrQueueUnknown`, `ErrUnreachable`, `RefusedError` (Task 3) are what Task 4 maps.
   Setting names match across Tasks 1, 4 and 6.
4. **Review Focus.** Each line names its test and the task that owns it.
5. **Proportion.** The plan holds signatures and test assertions; the only code block of a body
   is the CI step, which the tests do not determine.
