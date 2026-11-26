# Label print server (CUPS)

`ghcr.io/eh-homelab/scadbuddy-cups` is CUPS with the Zebra ZPL driver. It shares the
network label printer with every computer and with ScadBuddy, so a PDF (a ScadBuddy label
page, a Pirate Ship shipping label) prints from any print dialog without a Zebra driver on
the client.

- Image: the Dockerfile's `cups` stage, built by `build-image.yml`'s `cups` job and checked
  by `ci.yml`'s `cups-image` job. Debian 13's `cups`, `cups-filters` and `ghostscript`; the
  driver is CUPS's own "Zebra ZPL Label Printer" (`drv:///sample.drv/zebra.ppd`, with the
  `rastertolabel` filter). CUPS marks PPD drivers deprecated
  ([OpenPrinting/cups#103](https://github.com/OpenPrinting/cups/issues/103)); 2.4 still runs them.
- `start.sh` declares the queues from `CUPS_QUEUES` at every start, then runs `cupsd -f`
  as uid 10001 on port 8631. One queue per line, `NAME PAGE-SIZE DEVICE-URI [DESCRIPTION]`;
  the first is the default:

  ```text
  S-EDCDU03-2x1 w144h72  zebra://S-EDCDU03.internal.nullreference.io:9100?length=203  Zebra GK420t, 2 x 1 in labels
  S-EDCDU03-4x6 w288h432 zebra://S-EDCDU03.internal.nullreference.io:9100?length=1218 Zebra GK420t, 4 x 6 in labels
  ```

  PAGE-SIZE is a PageSize of the Zebra ZPL PPD (`w144h72` is 2 x 1 in, `w288h432` is 4 x 6
  in; the size in points, 72 a inch). Each queue's PPD keeps only its own size and no
  custom size, so a print dialog offers nothing else.

## Label sizes

One queue per label size, and a job prints only on its own labels. The `zebra` backend
(`deploy/cups/zebra`, URI `zebra://HOST[:PORT]?length=DOTS[&tolerance=DOTS][&interval=S]`)
asks the printer for its host status (`~HS`) before it sends a job. It sends only when the
head is closed, labels are in, the printer is not paused, and the label length the
printer measured at its last calibration is within `tolerance` dots (default 10 % of
`length`) of `length`. Otherwise the job waits, processing, and the backend asks again
every `interval` seconds (30). Meanwhile the queue shows the state reason
`media-needed` (or `cover-open`, `media-empty`, `paused`) and a message such as "1.02 in
labels are loaded; this queue prints 6.00 in labels. Load them and calibrate (hold
Feed). Waiting." Once the right labels are loaded and calibrated, the job prints; it is
never cancelled for the wrong labels. Only that queue waits; the others print.

- `length` is the label's length in dots at 203 dpi: 203 for 1 in, 1218 for 6 in. The
  GK420t reported `0208` with 2 x 1 in labels loaded (`~HS`, 2026-11-25).
- The printer measures length only, not width, so two sizes of the same length cannot be
  told apart.
- After a roll swap, calibrate (hold Feed until the light flashes once): the length the
  backend reads is the last calibration's.
- Exiting `CUPS_BACKEND_RETRY` would also keep a job, but cupsd clears the queue's message
  between tries (measured 2026-11-26), so the backend waits in place instead. A send that
  fails part way does exit RETRY; `cupsd.conf` retries it every 30 s for a week.

- State: `/var/lib/cups/etc` is cupsd's ServerRoot (queues, PPDs, TLS files) and
  `/var/lib/cups/spool` its RequestRoot (jobs not yet printed). Mount one durable volume
  at `/var/lib/cups`; `start.sh` creates both subdirectories and copies in each default
  file that is missing, so a PVC whose root belongs to root (with `fsGroup: 10001`) works.
  cupsd must own and chmod those directories, which is why they are not the volume's
  root. Its cache is `/var/lib/cups/cache`, on the same volume for the same reason (an
  emptyDir's root is root's too); cupsd rebuilds it when it is missing.
- Configuration: `/etc/cups/cupsd.conf` and `cups-files.conf`, the image's. Mount a file
  over either to replace it. Leave `CUPS_QUEUES` unset and `start.sh` declares no
  queue: the queues in `/var/lib/cups/etc` (on the volume, or set by `lpadmin` inside
  the pod) are the deployment's own. This supports any printer the image has a driver
  for: CUPS's sample drivers (Zebra ZPL, EPL, CPCL, Dymo, generic PCL and ESC/P) and
  driverless IPP Everywhere and AirPrint printers through cups-filters.
- `cupsd.conf` lets a network client print, see jobs and cancel one job. Every
  administration and operator operation, and the web page's `/admin`, asks for the user
  `admin` and its password (HTTP Basic auth). `start.sh` reads the password from
  `CUPS_ADMIN_PASSWORD_FILE` (default `/run/secrets/cups-admin/password`, a mounted
  Secret), and PAM checks it (`pam_pwdfile`, against a crypt in
  `/var/lib/cups/etc/admin.passwd` that `start.sh` rewrites at every start; cupsd runs
  without root, so not against `/etc/shadow`). With no password file, every admin login
  fails. A remote `lpadmin` without the login gets `Forbidden`, and the web page answers
  401 without it and 200 with it (the CI smoke checks both, and a pause through the web
  page). This holds inside the pod too: `kubectl exec … cupsdisable` is refused, because
  it cannot log in.
  A queue added on the web page stays; changes made there to a `CUPS_QUEUES` queue stay
  until the next start, which rewrites that queue from env.
- TLS ends at the shared Envoy Gateway; cupsd serves plain HTTP. Clients add the printer
  as `ipps://cups.internal.nullreference.io/printers/<queue>`, e.g. `S-EDCDU03-2x1`
  (Windows: "Select a shared printer by name" with the `https://` form and the Microsoft
  PS Class Driver).
- No mDNS: AirPrint discovery does not cross into the cluster, so a phone or computer adds
  the printer by that URL.
- Finding printers: the admin page's "Find New Printers" lists what CUPS's `snmp`
  backend finds. Its default asks only the pod's own broadcast address, and mDNS stays on
  its own segment, so neither reaches a LAN printer from the cluster. Mount an
  `snmp.conf` over `/etc/cups/snmp.conf` (a ConfigMap, `subPath`) with one `Address`
  line per printer-subnet host; `start.sh` copies it into `/var/lib/cups/etc` at every
  start, and the backend asks each address by unicast SNMP (community `public`), which
  crosses VLANs. Without a mount, the image's default (`Address @LOCAL`) applies. Measured
  2026-11-26 from the prod pod over 172.16.0.0/24: it found the Zebra as
  `socket://172.16.0.53` ("Zebra Technologies ZTC GK420t") in about 2 s. A printer with
  SNMP off is not found; add it by URI.
- Queues added on the admin page are kept: with `CUPS_QUEUES` set, `start.sh` rewrites
  only the queues it declared (listed in `/var/lib/cups/etc/scadbuddy-queues`), removes
  one dropped from `CUPS_QUEUES`, and keeps the first `CUPS_QUEUES` queue the default.

The printer (measured 2026-11-25 on its web page and with `~HS`/`~HD` on :9100): Zebra
GK420t, 203 dpi, firmware V61.17.17Z (Zebra's last for this model), direct thermal, gap
media, raw ZPL on 9100.
