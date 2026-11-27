# ZPL labels to a network Zebra (#7170)

## Goal

A person sends spool, place and type labels straight to a Zebra GK420t on the LAN, as raw ZPL
on TCP port 9100. The browser's print dialog is not used. The stock is 2 in x 1 in, direct
thermal by default. Thermal transfer (with a ribbon) is a choice for labels that see heat.

Plan: `docs/superpowers/plans/2026-11-27-zpl-label-printer.md`.

## Facts this design uses

- The GK420t prints direct thermal and thermal transfer at 203 dpi, and it reads ZPL. Its
  options include an internal 10/100 Ethernet print server. Source: the
  [GK420t User Guide](https://www.zebra.com/content/dam/support-dam/en/documentation/unrestricted/guide/product/gk420t-ug-en.pdf),
  "Introduction" (PDF page 5) and "Options". Read 2026-11-27 with `pdftotext`.
- The same guide's "ZPL Configuration" table (Table 13, PDF page 105) gives the defaults
  `~SD` DARKNESS 10.0 and `^PW` PRINT WIDTH 832 dots. It says to set the print mode with
  `^MT` (PDF page 25). Read the same way.
- `^GFa,b,c,d,data` is a graphic field. Type `A` is ASCII hex, two hex digits per image byte.
  `b` and `c` are the byte count, 1 to 99999; `d` is the bytes per row. Source:
  [ZPL `^GF`](https://docs.zebra.com/us/en/printers/software/zpl-pg/c-zpl-zpl-commands/r-zpl-gf.html).
- `^MTa`: `T` thermal transfer, `D` direct thermal. Source:
  [ZPL `^MT`](https://docs.zebra.com/us/en/printers/software/zpl-pg/c-zpl-zpl-commands/r-zpl-mt.html).
- `~SD##` sets the darkness, 00 to 30. Source:
  [ZPL `~SD`](https://docs.zebra.com/us/en/printers/software/zpl-pg/c-zpl-zpl-commands/r-zpl-sd.html).
- Port 9100 is the port the issue names. It is not measured on this printer: the printer is
  not on hand.
- The label layer (#7216) gives PNG output: `POST /api/v1/filament/labels` with `format: "png"`
  and `dpi: 203` answers one greyscale PNG per page. One page is `labels.png` (`image/png`).
  More pages are `labels.zip` (`application/zip`, stored), in request order
  (`backend/scadbuddy/filament/label_png.py`, `backend/scadbuddy/api/filament.py`
  `render_labels`). With the `roll` layout, one page is one label. Read in the code at
  origin/main e4b1417b8.
- The PNG carries its dpi (Pillow writes `pHYs`; `render_pngs` saves with `dpi=(dpi, dpi)`).
- The smallest custom label is 40 x 25 mm (`label_templates.py` `MIN_WIDTH_MM`,
  `MIN_HEIGHT_MM`). 2 in x 1 in is 50.8 x 25.4 mm, so it fits as a template.
- Core is the API pod's front: every request reaches core first, and a path that no named
  area serves goes on to Python (`tools/go/core/README.md`). A public area mounts in
  `main.go` `publicPackages` behind `internal/auth`. No ingress change is needed.
- Python's OpenAPI does not hold Go's routes. The frontend types a Go route by hand
  (`frontend/src/api/unknownTrays.ts`, plan G12 of #7143).
- On prod, `SCADBUDDY_CORE_AREAS` is set in eh-homelab/clusters
  `applications/scadbuddy/scadbuddy.yaml`. The API pod has no NetworkPolicy there
  (`ls applications/scadbuddy/networkpolicy-*.yaml`, 2026-11-27), so core can reach a LAN
  address now. eh-homelab/clusters#3182 (the network) must allow it later.

## Decisions

- **R1.** The send is a new Go route in `tools/go/core`, area `label-printer`, on the shared
  person-auth gate (`internal/auth`, #8038). It prints any label image that the label layer
  gives, not a spool-only label. Inventory labels (#7169) use the same route later.
- **R2.** No Python route is added. Python changes only its settings and one template. Only
  the API and core hold SQL. Core runs no untrusted code: it decodes PNGs with Go's standard
  library and writes ZPL that it builds itself.
- **R3.** The printer's address is two stored-only settings, `filament_label_printer_host`
  and `filament_label_printer_port` (default 9100). A request names no address. So the route
  cannot open a connection to a host that a caller picks (no SSRF). Only a person's own
  session may change or reset the two settings (403 `person_only`, as `MAIL_CONNECTION`):
  an agent or a service could otherwise move the printer to a host of its own. The host is a
  DNS name or an IP literal, with no scheme, port, path or user. Core checks the stored
  value again before it dials. Core never reads from the socket, so no printer bytes reach
  the caller.
- **R4 (option a).** The browser asks `POST /api/v1/filament/labels` for PNGs at 203 dpi, as
  it does for a download. Then it posts the answer's bytes, unchanged, to core. The body is a
  PNG or the label layer's stored ZIP of PNGs. Core reads the type from the bytes, never from
  a header. Core converts each image to a 1-bit `^GFA` graphic and sends one ZPL job. Why: the
  layout stays in one place (the label layer), and core needs no second path to Python, no
  forwarded credential and no Python client. Option b would need core to call Python with the
  person's cookie or a service credential, and to know every label kind's request body. The
  hex in `^GFA` is the printer's own wire format on the socket. It is not an image in JSON,
  so the base64 rule (#2479) is kept.
- **R5.** The default stock is 2 in x 1 in. A new built-in template `roll_2x1in`
  ("Roll label 2 x 1 in", 50.8 x 25.4 mm) is added beside `roll_50x25`. The page's template
  choice stays the one remembered choice for size. The media type and the darkness are
  remembered choices with a control in the Zebra controls: `filament_label_printer_media`
  (`direct` or `transfer`, default `direct`) and `filament_label_printer_darkness` (0 to 30,
  default 10, the printer's own default). They are stored-only settings, not part of the
  address, so a change of the print choice never writes the address. The page saves each at
  each change (`useSettingSaver`) and sends both on the request. A request that names
  neither gets the stored values, then the defaults.
- **R6.** The GK420t is not on hand. Tests use a fake TCP listener (`net.Listen` on
  `127.0.0.1:0`) that records the bytes, and `net.Pipe` for a printer that never reads. The
  dial and write timeouts are fields of the sender, so a test sets them. No test sleeps or
  reads the date.
- **R7.** The Labels page gets "Print to Zebra" beside Print and Download. It shows only when
  `filament_label_printer_host` is set. The page also prints printer and place labels
  (`?printers=`, `?locations=`), so one control covers them. The Settings page's Filament
  section gets the host and port rows. The mocks serve the route.
- **R8.** Errors are problems with a `code`: no printer set, 409 `label_printer_unset`; the
  printer refuses, does not answer or times out, 502 `label_printer_unreachable`; a body
  past 8 MiB, 413; bytes that are neither a PNG nor a ZIP of PNGs, 422 `label_not_png`; an
  image wider than 832 dots, or with more than 99999 bytes of graphic, 422 `label_too_large`;
  a PNG whose `pHYs` says other than 203 dpi, 422 `label_wrong_dpi`; more than 100 labels,
  422 `too_many_labels`. Core logs each send (host, port, labels, ZPL bytes, the caller's
  kind), never the image.

## The ZPL job

One job is one TCP connection. It is `~SD<dd>`, then for each image:

```text
^XA^MT<D|T>^PW<width>^LL<height>^LH0,0^FO0,0^GFA,<n>,<n>,<row>,<hex>^FS^XZ
```

`<row>` is `ceil(width / 8)`, `<n>` is `row * height`, and `<hex>` is upper-case hex with no
line breaks. A bit is 1 (a black dot) when the pixel's grey, composed on white, is below 128.
A row's unused low bits are 0. Core sends one job at a time per process (a mutex), so two
clicks never mix their bytes. Two API pods can each open a connection; the printer takes one
connection at a time.

## Out of scope

- An assistant tool that prints to the Zebra.
- Printer status (`~HS`), a printer list, or more than one label printer.
- A 300 dpi Zebra. The route accepts 203 dpi only.
