#!/bin/sh
# Declares the one label printer from env, then runs cupsd in the foreground
# (deploy/cups/README.md). The queue is rewritten at every start, so the Deployment's
# env is the only place it is configured.
#
#   CUPS_PRINTER_NAME       queue name, e.g. S-EDCDU03
#   CUPS_PRINTER_URI        device URI, e.g. socket://S-EDCDU03.internal.nullreference.io:9100
#   CUPS_PRINTER_INFO       description shown to clients (optional)
#   CUPS_DEFAULT_PAGE_SIZE  a PageSize of the Zebra ZPL PPD, e.g. w144h72 (2 x 1 in)
set -eu

: "${CUPS_PRINTER_NAME:?set CUPS_PRINTER_NAME}"
: "${CUPS_PRINTER_URI:?set CUPS_PRINTER_URI}"
: "${CUPS_DEFAULT_PAGE_SIZE:=w288h432}"
: "${CUPS_PRINTER_INFO:=$CUPS_PRINTER_NAME}"

case "$CUPS_PRINTER_NAME" in
  *[!A-Za-z0-9_-]* | "") echo "CUPS_PRINTER_NAME may hold only letters, digits, - and _" >&2; exit 1 ;;
esac

ppd="/etc/cups/ppd/${CUPS_PRINTER_NAME}.ppd"
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

cat > /etc/cups/printers.conf <<EOF
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
EOF

exec /usr/sbin/cupsd -f
