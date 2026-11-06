#!/usr/bin/env python3
"""One MQTT pushall from the H2C, printed and optionally saved.

usage: uv run --with paho-mqtt printer_report.py [out.json] [--printer 1]
Reads ip, serial and access code from Bambuddy, subscribes to the printer's report
topic, publishes pushall, and prints state, job, error, mapping and stage list.
"""
import json
import ssl
import sys
import time
import urllib.request

import paho.mqtt.client as mqtt

BAMBUDDY = "https://bambuddy.internal.nullreference.io/api/v1"
# Bambu's stage ids (ha-bambulab CURRENT_STAGE_IDS / Bambu Studio get_stage_string);
# ids above 77 are newer H2-series firmware stages with no public name.
STAGES = {1: "auto bed levelling", 2: "heatbed preheating", 3: "XY mech sweep (vibration)",
          4: "changing filament", 7: "heating hotend", 8: "calibrating extrusion (flow dynamics)",
          9: "scanning bed surface", 10: "inspecting first layer", 11: "identifying build plate",
          12: "calibrating micro lidar", 13: "homing toolhead", 14: "cleaning nozzle tip",
          15: "checking extruder temperature", 19: "calibrating extrusion flow", 22: "filament unloading",
          24: "filament loading", 25: "calibrating motor noise", 29: "cooling chamber",
          39: "nozzle offset calibration", 40: "high-temperature bed levelling",
          54: "waiting for heatbed temperature", 72: "hotend type detection",
          74: "heatbed foreign-object detection"}

args = [a for a in sys.argv[1:] if not a.startswith("--")]
printer = "1"
if "--printer" in sys.argv:
    printer = sys.argv[sys.argv.index("--printer") + 1]
    args = [a for a in args if a != printer]
out_path = args[0] if args else None

p = json.load(urllib.request.urlopen(f"{BAMBUDDY}/printers/{printer}", timeout=20))
ip, serial, code = p["ip_address"], p["serial_number"], p["access_code"]
report: dict = {}


def on_connect(c, u, f, rc, props=None):
    c.subscribe(f"device/{serial}/report")
    c.publish(f"device/{serial}/request", json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}}))


def on_message(c, u, m):
    d = json.loads(m.payload).get("print", {})
    if isinstance(d.get("ams"), dict):  # the full pushall answer carries the ams block
        report.update(d)
        c.disconnect()


c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="skill-printer-report")
c.username_pw_set("bblp", code)
c.tls_set(cert_reqs=ssl.CERT_NONE)
c.tls_insecure_set(True)
c.on_connect, c.on_message = on_connect, on_message
c.connect(ip, 8883, 30)
t = time.time()
while not report and time.time() - t < 25:
    c.loop(1)
if not report:
    sys.exit("no pushall answer in 25 s")
if out_path:
    json.dump(report, open(out_path, "w"), indent=1)
mapping = report.get("mapping") or []
print("state", report.get("gcode_state"), "| job", report.get("subtask_name"),
      "| layer", report.get("layer_num"), "/", report.get("total_layer_num"),
      "| print_error", report.get("print_error"))
print("mapping", mapping, "UNMAPPED SLOT" if 65535 in mapping else "(all mapped)")
print("stages", [STAGES.get(s, s) for s in report.get("stg") or []], "current", report.get("stg_cur"))
if out_path:
    print("saved", out_path)
