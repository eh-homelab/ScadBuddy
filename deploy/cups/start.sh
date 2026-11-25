#!/bin/sh
# Prepares CUPS's state, declares the one label printer from env, then runs cupsd in
# the foreground (deploy/cups/README.md).
#
# The configuration is /etc/cups/cupsd.conf and cups-files.conf: the image's, or files
# mounted over them. The state lives under /var/lib/cups, which may be a volume that
# starts empty: etc/ is cupsd's ServerRoot (queues, PPDs, TLS files), spool/ its
# RequestRoot (jobs). Both are created here, so cupsd owns them even when the volume's
# root belongs to root; each file of the image's ServerRoot that etc/ lacks is copied
# in, and none is overwritten.
#
# With CUPS_PRINTER_NAME set, the queue is rewritten at every start, so the
# Deployment's env is the only place it is configured. With it unset, the queues in
# /var/lib/cups/etc are the deployment's own.
#
#   CUPS_PRINTER_NAME       queue name, e.g. S-EDCDU03
#   CUPS_PRINTER_URI        device URI, e.g. socket://S-EDCDU03.internal.nullreference.io:9100
#   CUPS_PRINTER_INFO       description shown to clients (optional)
#   CUPS_DEFAULT_PAGE_SIZE  a PageSize of the Zebra ZPL PPD, e.g. w144h72 (2 x 1 in)
set -eu

state=/var/lib/cups
mkdir -p "$state/etc/ppd" "$state/spool/tmp"
cp -Rn /usr/local/share/scadbuddy-cups/server-root/. "$state/etc/"

run_cupsd() {
  exec /usr/sbin/cupsd -f -c /etc/cups/cupsd.conf -s /etc/cups/cups-files.conf
}

if [ -z "${CUPS_PRINTER_NAME:-}" ]; then
  run_cupsd
fi

: "${CUPS_PRINTER_URI:?set CUPS_PRINTER_URI}"
: "${CUPS_DEFAULT_PAGE_SIZE:=w288h432}"
: "${CUPS_PRINTER_INFO:=$CUPS_PRINTER_NAME}"

case "$CUPS_PRINTER_NAME" in
  *[!A-Za-z0-9_-]* | "") echo "CUPS_PRINTER_NAME may hold only letters, digits, - and _" >&2; exit 1 ;;
esac

ppd="$state/etc/ppd/${CUPS_PRINTER_NAME}.ppd"
/usr/lib/cups/daemon/cups-driverd cat drv:///sample.drv/zebra.ppd > "$ppd" 2>/dev/null
if ! grep -q "^\*PageSize ${CUPS_DEFAULT_PAGE_SIZE}/" "$ppd"; then
  echo "CUPS_DEFAULT_PAGE_SIZE ${CUPS_DEFAULT_PAGE_SIZE} is not a page size of the Zebra ZPL PPD" >&2
  exit 1
fi
sed -i \
  -e "s/^\*DefaultPageSize: .*/*DefaultPageSize: ${CUPS_DEFAULT_PAGE_SIZE}/" \
  -e "s/^\*DefaultPageRegion: .*/*DefaultPageRegion: ${CUPS_DEFAULT_PAGE_SIZE}/" \
  -e "s/^\*DefaultImageableArea: .*/*DefaultImageableArea: ${CUPS_DEFAULT_PAGE_SIZE}/" \
  -e "s/^\*DefaultPaperDimension: .*/*DefaultPaperDimension: ${CUPS_DEFAULT_PAGE_SIZE}/" \
  "$ppd"

cat > "$state/etc/printers.conf" <<CONF
<DefaultPrinter ${CUPS_PRINTER_NAME}>
Info ${CUPS_PRINTER_INFO}
MakeModel Zebra ZPL Label Printer
DeviceURI ${CUPS_PRINTER_URI}
State Idle
Accepting Yes
Shared Yes
JobSheets none none
QuotaPeriod 0
PageLimit 0
KLimit 0
OpPolicy default
ErrorPolicy retry-job
</DefaultPrinter>
CONF

run_cupsd
