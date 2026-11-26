#!/bin/bash
# Prepares CUPS's state, declares one queue per label size from CUPS_QUEUES, then runs cupsd in
# the foreground (deploy/cups/README.md).
#
# The configuration is /etc/cups/cupsd.conf and cups-files.conf: the image's, or files
# mounted over them. The state lives under /var/lib/cups, which may be a volume that
# starts empty: etc/ is cupsd's ServerRoot (queues, PPDs, TLS files), spool/ its
# RequestRoot (jobs), cache/ its CacheDir. They are created here, so cupsd owns them
# (and may chmod them, which it does at every start) even when the volume's
# root belongs to root; each file of the image's ServerRoot that etc/ lacks is copied
# in, and none is overwritten except snmp.conf, which is replaced from
# /etc/cups/snmp.conf at every start.
#
# With CUPS_QUEUES set, its queues are rewritten at every start, so the Deployment's env
# is the only place they are configured. Queues added on the admin page (a printer
# found by "Find New Printers", say) are kept across starts: start.sh rewrites only the
# queues it declared, listed in etc/scadbuddy-queues. With CUPS_QUEUES unset, every queue
# in /var/lib/cups/etc is the deployment's own.
#
#   CUPS_QUEUES  one queue per line: NAME PAGE-SIZE DEVICE-URI [DESCRIPTION...]
#                PAGE-SIZE is a PageSize of the Zebra ZPL PPD (w144h72 is 2 x 1 in,
#                w288h432 is 4 x 6 in); the queue's PPD keeps only that size and
#                no custom size, so a print dialog offers nothing else. DEVICE-URI is usually
#                zebra://HOST:9100?length=DOTS, which holds a job until the loaded
#                labels match (deploy/cups/zebra). The first queue is the default.
#   CUPS_ADMIN_PASSWORD_FILE
#                a file holding the password of the web page's and IPP's `admin`
#                user (default /run/secrets/cups-admin/password, a mounted Secret).
#                Without it, every admin login fails.
set -eu

state=/var/lib/cups
mkdir -p "$state/etc/ppd" "$state/spool/tmp" "$state/cache"
cp -Rn /usr/local/share/scadbuddy-cups/server-root/. "$state/etc/"
# snmp.conf is configuration, not state: /etc/cups/snmp.conf (the image's, or a file
# mounted over it, deploy/cups/README.md "Finding printers") replaces the ServerRoot
# copy at every start. A mount inside the state volume would make its parent directory
# root's on a new volume, and then etc/ppd could not be created.
cp /etc/cups/snmp.conf "$state/etc/snmp.conf"

# pam_pwdfile's file (the image's /etc/pam.d/cups): `admin:` and the password's crypt.
admin_file=${CUPS_ADMIN_PASSWORD_FILE:-/run/secrets/cups-admin/password}
rm -f "$state/etc/admin.passwd"
if [ -s "$admin_file" ]; then
  (umask 077 && printf 'admin:%s\n' "$(head -n1 "$admin_file" | tr -d '\r\n' | openssl passwd -6 -stdin)" > "$state/etc/admin.passwd")
else
  echo "no admin password at $admin_file: admin logins are refused" >&2
fi

run_cupsd() {
  exec /usr/sbin/cupsd -f -c /etc/cups/cupsd.conf -s /etc/cups/cups-files.conf
}

if [ -z "${CUPS_QUEUES:-}" ]; then
  # Every queue is the deployment's own now; a stale list would later drop a queue
  # added meanwhile under one of its names.
  rm -f "$state/etc/scadbuddy-queues"
  run_cupsd
fi

conf="$state/etc/printers.conf"
managed="$state/etc/scadbuddy-queues"
new="$state/etc/printers.conf.new"
: > "$new"
names=()
first=1
while read -r name size uri info; do
  [ -z "$name" ] && continue
  case "$name" in
    *[!A-Za-z0-9_-]*) echo "queue name $name may hold only letters, digits, - and _" >&2; exit 1 ;;
  esac
  if [ -z "$uri" ]; then
    echo "queue $name needs NAME PAGE-SIZE DEVICE-URI" >&2
    exit 1
  fi
  names+=("$name")
  ppd="$state/etc/ppd/$name.ppd"
  /usr/lib/cups/daemon/cups-driverd cat drv:///sample.drv/zebra.ppd 2>/dev/null |
    awk -v size="$size" '
      /^\*(PageSize|PageRegion|ImageableArea|PaperDimension) / {
        opt = $2; sub(/\/.*/, "", opt); sub(/:$/, "", opt)
        if (opt != size) next
      }
      # The Zebra model allows custom sizes (sample.drv VariablePaperSize); a custom
      # size would print at its own width on the labels of this queue.
      /^\*(CustomPageSize|ParamCustomPageSize|MaxMediaWidth|MaxMediaHeight)[ :]/ { next }
      /^\*Default(PageSize|PageRegion|ImageableArea|PaperDimension):/ { sub(/: .*/, ": " size) }
      { print }
    ' > "$ppd"
  if ! grep -q "^\*PageSize ${size}/" "$ppd"; then
    echo "queue $name: $size is not a page size of the Zebra ZPL PPD" >&2
    exit 1
  fi
  # The first queue is the default.
  tag=Printer
  [ "$first" = 1 ] && tag=DefaultPrinter
  first=0
  cat >> "$new" <<CONF
<${tag} ${name}>
Info ${info:-$name}
MakeModel Zebra ZPL Label Printer
DeviceURI ${uri}
State Idle
Accepting Yes
Shared Yes
JobSheets none none
QuotaPeriod 0
PageLimit 0
KLimit 0
OpPolicy default
ErrorPolicy retry-job
</${tag}>
CONF
done <<<"$CUPS_QUEUES"

if [ "${#names[@]}" -eq 0 ]; then
  echo "CUPS_QUEUES declares no queue" >&2
  exit 1
fi

# Keep every queue start.sh did not declare (now or at the last start), as a plain
# queue: the default is CUPS_QUEUES's first. A queue dropped from CUPS_QUEUES goes,
# with its PPD.
drop=" ${names[*]} "
if ! [ -f "$managed" ] && [ -f "$conf" ]; then
  # The first start with this list (or after CUPS_QUEUES was unset): every queue on
  # the Zebra ZPL driver is one start.sh declared, so one dropped from CUPS_QUEUES
  # in the same rollout goes rather than being kept as an admin-page queue.
  while read -r old; do
    drop+="$old "
    case " ${names[*]} " in *" $old "*) ;; *) rm -f "$state/etc/ppd/$old.ppd" ;; esac
  done < <(awk '
    /^<(Default)?Printer / { name = $2; sub(/>$/, "", name) }
    /^MakeModel Zebra ZPL Label Printer$/ { print name }
  ' "$conf")
fi
if [ -f "$managed" ]; then
  while read -r old; do
    [ -n "$old" ] && drop+="$old "
    [ -n "$old" ] && case " ${names[*]} " in *" $old "*) ;; *) rm -f "$state/etc/ppd/$old.ppd" ;; esac
  done < "$managed"
fi
if [ -f "$conf" ]; then
  awk -v drop="$drop" '
    /^<(Default)?Printer / { name = $2; sub(/>$/, "", name); skip = index(drop, " " name " ") > 0 }
    !skip {
      sub(/^<DefaultPrinter /, "<Printer "); sub(/^<\/DefaultPrinter>/, "</Printer>")
      print
    }
    /^<\/(Default)?Printer>/ { skip = 0 }
  ' "$conf" >> "$new"
fi
mv "$new" "$conf"
printf '%s\n' "${names[@]}" > "$managed"

run_cupsd
