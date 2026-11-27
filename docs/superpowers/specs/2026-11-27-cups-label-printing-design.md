# Print labels through CUPS (#7170)

## Goal

A person sends spool, place and printer labels from the Labels page to a label queue of the
label print server that ScadBuddy already runs (CUPS, `deploy/cups/`). The browser's print
dialog is not used. The queue owns the label size check, the job order and the retries. Core
sends the label layer's PDF to the queue with one IPP `Print-Job`. Core writes no ZPL.

Plan: `docs/superpowers/plans/2026-11-27-cups-label-printing.md`.

This design replaces the first design of #7170 (commit f8481d07e). In that design, core opened
raw TCP to the Zebra on port 9100 and wrote `^GF` graphics itself. That went around the print
server. The owner asked "Don't we use cups?" (2026-11-27). The ruling: print through CUPS.

## Facts this design uses

- The print server is CUPS 2.4 with CUPS's own "Zebra ZPL Label Printer" driver
  (`drv:///sample.drv/zebra.ppd`, filter `rastertolabel`), and Debian's `cups-filters` and
  `ghostscript`. `start.sh` makes one queue per label size from `CUPS_QUEUES`
  (`NAME PAGE-SIZE DEVICE-URI [DESCRIPTION]`). Each queue's PPD keeps only its own PageSize and
  no custom size. cupsd listens on port 8631. Source: `deploy/cups/README.md`,
  `deploy/cups/start.sh`.
- The `zebra` backend holds a job, "processing", until the printer's calibrated label length
  matches the queue. Meanwhile the queue's state reason is `media-needed` (or `cover-open`,
  `media-empty`, `paused`), with a message such as "1.02 in labels are loaded; this queue
  prints 6.00 in labels. Load them and calibrate (hold Feed). Waiting." A send that fails part
  way exits RETRY, and `cupsd.conf` retries it every 30 s for a week. Source:
  `deploy/cups/zebra`, `deploy/cups/README.md` "Label sizes".
- `cupsd.conf` lets any network client send `Print-Job` and read printers and jobs without a
  login. Admin and operator operations need the user `admin`. Source: `deploy/cups/cupsd.conf`
  (`<Policy default>`).
- A PDF prints through this chain today: `ci.yml`'s `cups-image` job sends a Ghostscript PDF
  with `lp` to the 2 x 1 queue, and the fake Zebra gets ZPL with `^PW406` (2 in at 203 dpi).
  Read in `ci.yml` at e4b1417b8.
- Prod, read from eh-homelab/clusters `origin/main` with `git show` on 2026-11-27: the
  Deployment and the Service are both `cups`, in namespace `cups`
  (`clusters/prod/cups/kustomization.yaml`, `namespace: cups`). The Service port is 8631
  (`applications/cups/service.yaml`). `CUPS_QUEUES` is `S-EDCDU03-2x1 w144h72 …?length=203`
  (the default queue) and `S-EDCDU03-4x6 w288h432 …?length=1218`
  (`applications/cups/deployment.yaml`). The HTTPRoute serves `cups.internal.nullreference.io`
  through the shared Gateway, and Envoy ends TLS (`applications/cups/httproute.yaml`). The
  in-cluster base URL is `http://cups.cups.svc:8631`. A pod's DNS search path ends in
  `cluster.local`, so `cups.cups.svc` resolves.
- Reachability from the API pod (namespace `bambuddy`) to `cups:8631`, read from the same
  manifests, not measured live. The API pod (`app.kubernetes.io/name: scadbuddy`; core runs in
  it) has one NetworkPolicy, `applications/scadbuddy/networkpolicy-agent.yaml`, with
  `policyTypes: [Ingress]` only, so its egress is open. `applications/cups/` and
  `clusters/prod/cups/` have no NetworkPolicy, so the cups pod's ingress is open. The only
  Cilium policies (`git grep CiliumNetworkPolicy`) are in `applications/claude-runner/` and
  `applications/scadbuddy/networkpolicy-agent-worker.yaml`, and neither selects these pods. So
  no policy change is needed now. eh-homelab/clusters#3182 (the network) must allow it when it
  closes the API pod's egress.
- The Zebra ZPL PPD's options, from CUPS's `ppdc/sample.drv`
  ([2.4.x](https://github.com/OpenPrinting/cups/blob/2.4.x/ppdc/sample.drv), "Zebra ZPL
  Label Printer"):
  - `MediaType`: `Saved` (printer default; the PPD default), `Thermal` (thermal transfer),
    `Direct` (direct thermal).
  - `Darkness`: `-1` (printer default; the PPD default), `1` to `30`.
  - `zePrintRate`: `Default`, `1` to `12` (inches a second).
  - `zeMediaTracking` (`Continuous`, `Web` the default, `Mark`), `zeLabelTop` (-120 to 120
    dots), `zePrintMode` (`Saved`, `Tear`, `Peel`, `Rewind`, `Applicator`, `Cutter`),
    `zeTearOffPosition` (-120 to 120), `zeErrorReprint` (`Saved`, `Always`, `Never`).
- What `rastertolabel` does with them for ZPL
  ([filter/rastertolabel.c](https://github.com/OpenPrinting/cups/blob/2.4.x/filter/rastertolabel.c)):
  `MediaType` `Thermal` writes `^MTT`, `Direct` writes `^MTD`, and `Saved` writes neither.
  `Darkness` sets `cupsCompression`. The filter writes `~SD` only when that value is 1 to
  100, so `-1` keeps the printer's own darkness. `zePrintRate` writes `^PR`.
- An IPP client sets a PPD option per job. cupsd turns each job-group attribute of a job into
  the filters' option text (`get_options` in
  [scheduler/job.c](https://github.com/OpenPrinting/cups/blob/2.4.x/scheduler/job.c)), and
  the filters mark the PPD from that text. `lp -o Darkness=20` takes the same path.
- CUPS operations: `CUPS-Get-Printers` (0x4002) lists every queue with the attributes asked
  for ([CUPS IPP](https://openprinting.github.io/cups/doc/spec-ipp.html#CUPS_GET_PRINTERS)).
  `Print-Job` (0x0002) is in
  [RFC 8011 §4.2.1](https://www.rfc-editor.org/rfc/rfc8011#section-4.2.1). The binary
  encoding is [RFC 8010](https://www.rfc-editor.org/rfc/rfc8010), with byte examples in its
  Appendix A. `client-error-not-found` is 0x0406 and `server-error-not-accepting-jobs` is
  0x0506 (RFC 8011 Appendix B.1).
- A queue's size is in `media-default`, a PWG self-describing media name whose last part is
  `<width>x<height><unit>`, the unit `in` or `mm`
  ([PWG 5101.1](https://ftp.pwg.org/pub/pwg/candidates/cs-pwgmsn20-20130328-5101.1.pdf)).
  CUPS gives `custom_2x1in_2x1in` for `w144h72` and `na_index-4x6_4x6in` for 4 x 6 in
  (measured 2026-11-27 with `TestLiveCUPS`; see `deploy/cups/README.md`).
- The label layer (#7216): `POST /api/v1/filament/labels` answers `application/pdf` by
  default. With `layout: "roll"` and a one-label template, a page is one label of the
  template's size. A sheet template, or `layout: "sheet"`, gives Letter or A4 pages
  (`backend/scadbuddy/api/filament.py` `render_labels`, `backend/scadbuddy/filament/models.py`
  `LabelsRequest`). The page draws at most 100 labels (`MAX_LABELS`).
- `roll_2x1in` (commit f24b15e0c) is 50.8 x 25.4 mm. That is 144 x 72 pt, the 2 x 1 queue's
  `w144h72` exactly.
- Core is the API pod's front. A path that no named area serves goes on to Python. A public
  area mounts behind `internal/auth` (#8038). Python's OpenAPI does not hold Go's routes, so
  the frontend types a Go route by hand (`frontend/src/api/unknownTrays.ts`). Source:
  `tools/go/core/README.md` "Public routes".

## Decisions

- **R1.** The print routes are new Go code in `tools/go/core`: package `internal/labelprint`,
  area `label-printing`, behind the shared person-auth gate (`internal/auth`, #8038), class
  `api`. The print route takes any label PDF that the label layer (#7216) makes, so inventory
  labels (#7169) can use it later.
- **R2.** No Python route is added. Python edits only its settings (the three below) and keeps
  the template `roll_2x1in`. Only the API and core hold SQL; core reads the settings rows
  itself. Core runs no untrusted code. It checks the body's first bytes and passes the bytes
  on. It never parses the PDF.
- **R3.** A request never names a server or a URL. The CUPS server's base URL is the stored
  setting `filament_label_server_url`. With no row, it is `http://cups.cups.svc:8631`. Only a
  person's own session changes or resets it (403 `person_only`, `api/settings.py`
  `LABEL_PRINTER`, as `FLOW_ACCESS`). The value is an `http` or `https` URL with a host, no
  user, no query, no fragment, and a path of `/` or none. Python checks it on write. Core
  checks it again on read and treats a bad value as no server (502). A request names only a
  queue. Core checks the name first (`[A-Za-z0-9_-]`, 1 to 127 characters, the names that
  `start.sh` allows). Then core checks it against the queues that CUPS itself lists
  (`CUPS-Get-Printers`), before it prints. Core follows no redirect and reads at most 1 MiB of
  a CUPS reply. It gives the caller only the parsed attributes below, never CUPS's bytes. So
  no request can aim core at a host of its own (no SSRF).
- **R4.** The browser asks `POST /api/v1/filament/labels` for a PDF (`format: "pdf"`), as it
  does for a download. It posts the bytes, unchanged, as the body of core's print route
  (`Content-Type: application/pdf`, at most 8 MiB). Core checks that the body starts with
  `%PDF-` and sends it with `document-format` `application/pdf`. Why PDF and not PNG: the PDF
  is vector, so Ghostscript rasterizes it once, at the queue's own resolution and size. The
  `cups-image` CI job already prints a PDF through this chain. And the PDF is the label
  layer's default, one label a page. A PNG goes through `imagetoraster`, which scales an image
  that was rasterized before at a guessed dpi. Why the browser brings the bytes: core then
  needs no second path to Python, no forwarded credential and no knowledge of each label
  kind's request. The document moves as a binary body, never as base64 in JSON (#2479).
- **R5.** The IPP client is a small hand-written encoder and decoder in
  `internal/labelprint/ipp.go`, for two operations: `CUPS-Get-Printers` and `Print-Job`. Why
  not a library: the Go IPP library ([phin1x/go-ipp](https://github.com/phin1x/go-ipp)) adds a
  module and its CUPS adapter to vendor for two requests. RFC 8010's encoding is a flat list
  of `tag, name-length, name, value-length, value` records. A collection is more records of
  the same form. So the decoder is short, and it skips what it does not need. RFC 8010
  Appendix A's byte examples are its golden tests. The other tests use a fake IPP server
  (`httptest.Server`) that decodes what core sent and answers; never the real CUPS. One live
  check (Task 5) runs the client against the image in the `cups-image` CI job.
- **R6.** The print options are a remembered choice with a control (#6219): the stored-only
  setting `filament_label_job_options`, `{media_type, darkness, print_rate}`. `media_type` is
  `Saved`, `Thermal` or `Direct` (default `Saved`). `darkness` is `-1` or `1` to `30`
  (default `-1`). `print_rate` is `Default` or `1` to `12` (default `Default`). These are the
  PPD's own choice names. Thermal transfer vs direct thermal is a PPD option (`MediaType`), so
  the page offers it. The page offers the options only for a queue whose
  `printer-make-and-model` is `Zebra ZPL Label Printer`. The other PPD options
  (`zeMediaTracking`, `zeLabelTop`, `zePrintMode`, `zeTearOffPosition`, `zeErrorReprint`)
  belong to the loaded stock and the printer's set-up, not to one print. They stay at the
  queue's defaults. Core sends each option that is not its default as a job attribute
  (`nameWithoutLanguage`) with the PPD option's name. Core refuses any value outside the lists
  above (422). The queue is a remembered choice too: `filament_label_queue` (a queue name, or
  null).
- **R7.** The page lists the queues and pre-picks the remembered one. The page offers a queue
  for its labels only when CUPS reports the queue's size, and that size matches the PDF's page
  within 1 mm in each direction, in the same orientation. The PDF's page is the template's
  label size for `layout: "roll"` with a one-label template. Otherwise it is the sheet's page
  (Letter 215.9 x 279.4 mm, A4 210 x 297 mm). So a sheet never goes to a 2 x 1 queue, and a
  Letter queue on the same server can take a sheet. Core does not refuse a size: CUPS's queue
  owns that. The picked queue is the remembered queue when it fits the page, else the only queue
  that fits, else none. A picked queue always fits the page.
- **R8.** Errors are problems with a `reason`:
  - no server (a bad stored value), or CUPS does not answer, times out, or answers bytes that
    are not IPP: 502 `label_server_unreachable`;
  - a queue name that is malformed or not in CUPS's list, or CUPS answers 0x0406 for it: 422
    `label_queue_unknown`;
  - CUPS answers any other status above 0x00FF to `Print-Job`: 502 `label_print_refused`, with
    CUPS's `status-message` in `detail` (cut to 300 characters) and the status code in
    `ipp_status`;
  - a body past 8 MiB: 413 `label_too_large`. A body that does not start with `%PDF-`: 415 `label_not_pdf`. An
    option value outside R6's lists: 422 `label_option_invalid`.

  The print route answers 202 when CUPS has accepted the job, not when the label has printed.
  The answer holds the `job_id`, `job_state` and `job_state_reasons`. Core logs each job
  (queue, job id, bytes, the caller's kind), never the document.
- **R9.** The Labels page gets "Print to label printer" beside Print and Download: a queue
  picker, the options for a Zebra ZPL queue, and a button. It shows each queue's state and its
  CUPS message, so a message such as "1.02 in labels are loaded; …" is on the page. The page
  also prints printer and place labels (`?printers=`, `?locations=`), so one control covers
  them. The Settings page's Filament section gets the server URL row. The mocks serve both
  routes.
- **R10.** After merge, an eh-homelab/clusters PR adds `label-printing` to
  `SCADBUDDY_CORE_AREAS` in `applications/scadbuddy/scadbuddy.yaml`. No NetworkPolicy change is
  needed (Facts, "Reachability"). The PR comments on eh-homelab/clusters#3182 that the API pod
  needs egress to `cups/cups:8631`.

## The routes

| Route | Body | Answer |
|---|---|---|
| `GET /api/v1/label-printing/queues` | none | 200 `{queues: [Queue]}`, in CUPS's order |
| `POST /api/v1/label-printing/queues/{queue}/jobs?media_type=&darkness=&print_rate=` | the PDF's bytes, `application/pdf`, at most 8 MiB | 202 `{queue, job_id, job_state, job_state_reasons}` |

A `Queue` is `{name, info, make_and_model, state, state_reasons, state_message, accepting,
media, width_mm, height_mm}`. `state` is `idle`, `processing` or `stopped`. `media` is the
`media-default` name. `width_mm` and `height_mm` come from its last part, or are null when it
does not parse. A missing query parameter is the option's default, and core sends no
attribute for a default. The job's `job-name` is `ScadBuddy labels`, and its
`requesting-user-name` is `scadbuddy`, never the person's name.

Timeouts: the list 5 s, the print 30 s. Both are fields of the client, so a test sets them.
Two pods can print at the same time; CUPS puts the jobs in order.

## Out of scope

- An assistant tool that prints labels (#7169 can add one on this route).
- A job's status after CUPS accepts it, a cancel, or a job list. The CUPS web page has them.
- Any option other than R6's three, a custom size, or a printer outside CUPS.
