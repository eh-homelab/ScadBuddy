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
- `start.sh` declares the one queue from env at every start, then runs `cupsd -f` as uid
  10001 on port 8631:

  | Variable | Meaning |
  |---|---|
  | `CUPS_PRINTER_NAME` | queue name (the printer's host name, `S-EDCDU03`) |
  | `CUPS_PRINTER_URI` | device URI, `socket://S-EDCDU03.internal.nullreference.io:9100` |
  | `CUPS_PRINTER_INFO` | description clients show (optional) |
  | `CUPS_DEFAULT_PAGE_SIZE` | a PageSize of the PPD: `w144h72` is 2 x 1 in, `w288h432` is 4 x 6 in (default) |

- State: `/var/lib/cups/etc` is cupsd's ServerRoot (queues, PPDs, TLS files) and
  `/var/lib/cups/spool` its RequestRoot (jobs not yet printed). Mount one durable volume
  at `/var/lib/cups`; `start.sh` creates both subdirectories and copies in each default
  file that is missing, so a PVC whose root belongs to root (with `fsGroup: 10001`) works.
  cupsd must own and chmod those directories, which is why they are not the volume's
  root. The cache (`/var/cache/cups`) is rebuilt at start and needs no volume.
- Configuration: `/etc/cups/cupsd.conf` and `cups-files.conf`, the image's. Mount a file
  over either to replace it. Leave `CUPS_PRINTER_NAME` unset and `start.sh` declares no
  queue: the queues in `/var/lib/cups/etc` (on the volume, or set by `lpadmin` inside
  the pod) are the deployment's own. This supports any printer the image has a driver
  for: CUPS's sample drivers (Zebra ZPL, EPL, CPCL, Dymo, generic PCL and ESC/P) and
  driverless IPP Everywhere and AirPrint printers through cups-filters.
- `cupsd.conf` lets a network client print, see jobs and cancel one job. Every
  administration and operator operation is refused (a remote `lpadmin` gets `Forbidden`,
  checked by the CI smoke), so with the image's `cupsd.conf` the queue is set only by env,
  the volume or a mounted config.
  Connections from inside the pod are trusted as local; nothing else runs there.
- TLS ends at the shared Envoy Gateway; cupsd serves plain HTTP. Clients add the printer
  as `ipps://cups.internal.nullreference.io/printers/S-EDCDU03`.
- No mDNS: AirPrint discovery does not cross into the cluster, so a phone or computer adds
  the printer by that URL.

The printer (measured 2026-11-25 on its web page and with `~HS`/`~HD` on :9100): Zebra
GK420t, 203 dpi, firmware V61.17.17Z (Zebra's last for this model), direct thermal, gap
media, raw ZPL on 9100.
