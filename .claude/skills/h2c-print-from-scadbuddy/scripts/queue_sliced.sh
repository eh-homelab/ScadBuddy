#!/usr/bin/env bash
# Put a ScadBuddy sliced library file into Bambuddy's queue on the H2C with every
# calibration forced on.
#   queue_sliced.sh SLICED_ID NAME 'FLAT_MAPPING_JSON' [--cali on|auto|off] [--timelapse] [--printer 1] [--plate 1]
# --timelapse has the printer record one (Bambuddy pulls it into the archive after the
# print); without it an order attempt linked to this archive shows "no timelapse".
# SLICED_ID is the ScadBuddy native library id of the SLICED file (a run's
# sliced_library_file_id, or the print_sliced_upload result in its Temporal history).
set -euo pipefail
S=https://scadbuddy.internal.nullreference.io/api/v1; B=https://bambuddy.internal.nullreference.io/api/v1
sid=$1; name=$2; mapping=$3; shift 3; cali=on; printer=1; plate=1; timelapse=false
while [ $# -gt 0 ]; do case $1 in --cali) cali=$2; shift 2;; --timelapse) timelapse=true; shift;; --printer) printer=$2; shift 2;; --plate) plate=$2; shift 2;; *) echo "unknown $1"; exit 2;; esac; done
dir=$(mktemp -d); f="$dir/$name.3mf"
curl -sf -m 300 -o "$f" "$S/print/library/$sid/file"
python3 -I - "$f" "$plate" <<'PY'
import json, sys, zipfile
z = zipfile.ZipFile(sys.argv[1]); names = z.namelist()
g = f"Metadata/plate_{sys.argv[2]}.gcode"
if g not in names: sys.exit(f"{sys.argv[1]} has no {g}: not a sliced file (entries: {names[:8]})")
s = json.loads(z.read("Metadata/project_settings.config"))
print("sliced ok; colours", s.get("filament_colour"), "tower", s.get("wipe_tower_x"), s.get("wipe_tower_y"))
PY
fid=$(curl -sf -m 300 -F "file=@$f;type=model/3mf" "$B/library/files?generate_stl_thumbnails=false" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["id"]); assert d.get("file_type")=="gcode.3mf", d.get("file_type")')
echo "bambuddy library file $fid"
vib=true; [ "$cali" = off ] && vib=false
curl -sf -m 60 -X POST "$B/queue/" -H 'content-type: application/json' -d "{\"printer_id\":$printer,\"library_file_id\":$fid,\"plate_id\":$plate,\"ams_mapping\":$mapping,\"use_ams\":true,\"bed_levelling\":\"$cali\",\"flow_cali\":\"$cali\",\"nozzle_offset_cali\":\"$cali\",\"vibration_cali\":$vib,\"layer_inspect\":true,\"timelapse\":$timelapse,\"quantity\":1,\"require_previous_success\":false}" \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); d=d[0] if isinstance(d,list) else d; print("queue item", d.get("id"), d.get("status"), "position", d.get("position"), "mapping", d.get("ams_mapping"), "cali", d.get("bed_levelling"), d.get("flow_cali"), d.get("nozzle_offset_cali"), "timelapse", d.get("timelapse"))'
rm -rf "$dir"
