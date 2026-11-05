#!/usr/bin/env python3
"""Read ScadBuddy's Temporal workflows.

  print_runs.py list [--since ISO] [--prefix print-] [--port 18234]
  print_runs.py show <workflow id or prefix> [--port 18234]
  print_runs.py show --file history.json

list: id, type, status, start/close, subject, sliced file id, last activity, first failure.
show: the timeline: activities with decoded inputs (trimmed) and results, failures,
workflow-task timeouts, the early update outcome and the final result.
"""
import base64
import json
import sys
import urllib.parse
import urllib.request

PORT = "18234"
TRIM = 500


def api(path: str):
    return json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/v1/namespaces/scadbuddy/{path}", timeout=120))


def payload(p):
    try:
        return json.loads(base64.b64decode(p["payloads"][0]["data"]).decode())
    except Exception:
        return None


def events_of(h):
    return h.get("history", {}).get("events") or h.get("events") or []


def attrs(ev):
    return next((v for k, v in ev.items() if k.endswith("EventAttributes")), {})


def summarize(events):
    out = {"subject": None, "run_id": None, "sliced": None, "last_activity": None, "failure": None, "started": None, "subtask": None, "final": None}
    last = None
    for ev in events:
        t = ev["eventType"].replace("EVENT_TYPE_", ""); a = attrs(ev)
        if t == "WORKFLOW_EXECUTION_STARTED":
            inp = payload(a.get("input") or {}) or {}
            out["subject"] = inp.get("subject") if isinstance(inp, dict) else None
        elif t == "ACTIVITY_TASK_SCHEDULED":
            last = a["activityType"]["name"]; out["last_activity"] = last
        elif t == "ACTIVITY_TASK_COMPLETED":
            r = payload(a.get("result") or {})
            if last == "print_sliced_upload" and isinstance(r, int):
                out["sliced"] = r
            if last == "lan_print" and isinstance(r, dict) and r.get("subtask_name"):
                out["started"], out["subtask"] = r.get("started_at"), r.get("subtask_name")
        elif t in ("ACTIVITY_TASK_FAILED", "ACTIVITY_TASK_TIMED_OUT") and not out["failure"]:
            f = a.get("failure") or {}
            out["failure"] = f"{last}: {f.get('message') or (f.get('cause') or {}).get('message')}"
        elif t == "WORKFLOW_EXECUTION_UPDATE_COMPLETED":
            o = payload(((a.get("outcome") or {}).get("success")) or {})
            if isinstance(o, dict) and isinstance(o.get("run"), dict):
                out["run_id"] = o["run"].get("id")
        elif t == "WORKFLOW_EXECUTION_COMPLETED":
            r = payload(a.get("result") or {})
            if isinstance(r, dict):
                out["final"] = r.get("status"); out["run_id"] = out["run_id"] or r.get("id")
                if r.get("error"):
                    out["failure"] = out["failure"] or r["error"].get("detail")
                res = r.get("result") or {}
                if isinstance(res, dict) and res.get("sliced_library_file_id"):
                    out["sliced"] = res["sliced_library_file_id"]
        elif t == "WORKFLOW_EXECUTION_FAILED":
            out["final"] = "failed"; out["failure"] = out["failure"] or (a.get("failure") or {}).get("message")
    return out


def brief_input(inp):
    """Trim the big LanPrintInput: spools to one line each, drop prepared/rack."""
    if not isinstance(inp, dict):
        return inp
    out = {k: v for k, v in inp.items() if k not in ("spools", "prepared", "rack")}
    if isinstance(inp.get("spools"), dict):
        out["spools"] = {}
        for k, sp in inp["spools"].items():
            rec = ((sp.get("records") or {}).get("bambuddy") or {}).get("record_id")
            out["spools"][k] = f"{sp.get('spool_id')} {sp.get('brand')} {sp.get('color_name')} {sp.get('label')} (bambuddy record {rec})"
    return out


def trim(v):
    s = json.dumps(v) if not isinstance(v, str) else v
    return s if len(s) <= TRIM else s[:TRIM] + f"… (+{len(s) - TRIM})"


def show(events, wid=""):
    print("workflow", wid)
    last = None
    for ev in events:
        t = ev["eventType"].replace("EVENT_TYPE_", ""); a = attrs(ev); ts = ev["eventTime"][11:19]
        if t == "WORKFLOW_EXECUTION_STARTED":
            print(ts, "STARTED", a.get("workflowType", {}).get("name"), "queue", a.get("taskQueue", {}).get("name"), "input", trim(payload(a.get("input") or {})))
        elif t == "ACTIVITY_TASK_SCHEDULED":
            last = a["activityType"]["name"]
            inp = brief_input(payload(a.get("input") or {}))
            print(ts, "  schedule", last, "queue", a.get("taskQueue", {}).get("name"), trim(inp) if inp is not None else "")
        elif t == "ACTIVITY_TASK_COMPLETED":
            print(ts, "  done    ", last, trim(payload(a.get("result") or {})))
        elif t in ("ACTIVITY_TASK_FAILED", "ACTIVITY_TASK_TIMED_OUT", "ACTIVITY_TASK_CANCELED"):
            f = a.get("failure") or {}
            print(ts, "  FAILED  ", last, t, "|", f.get("message") or (f.get("cause") or {}).get("message"), "| retry", a.get("retryState"))
            for p in ((f.get("applicationFailureInfo") or {}).get("details") or {}).get("payloads") or []:
                try: print("            details", trim(json.loads(base64.b64decode(p["data"]).decode())))
                except Exception: pass
        elif t == "WORKFLOW_TASK_TIMED_OUT":
            print(ts, "  workflow task timed out (Temporal persistence slow; the client may have seen HTTP 500)")
        elif t == "WORKFLOW_EXECUTION_UPDATE_COMPLETED":
            print(ts, "  update answered", trim(payload(((a.get("outcome") or {}).get("success")) or {})))
        elif t == "START_CHILD_WORKFLOW_EXECUTION_INITIATED":
            print(ts, "  child", a.get("workflowType", {}).get("name"), a.get("workflowId"))
        elif t == "WORKFLOW_EXECUTION_COMPLETED":
            print(ts, "COMPLETED", trim(payload(a.get("result") or {})))
        elif t == "WORKFLOW_EXECUTION_FAILED":
            print(ts, "WORKFLOW FAILED", (a.get("failure") or {}).get("message"))
    s = summarize(events)
    print("summary:", json.dumps(s))


def main():
    global PORT
    a = sys.argv[1:]
    if "--port" in a:
        PORT = a[a.index("--port") + 1]; del a[a.index("--port"):a.index("--port") + 2]
    if not a:
        print(__doc__); return 2
    if a[0] == "list":
        since = a[a.index("--since") + 1] if "--since" in a else None
        prefix = a[a.index("--prefix") + 1] if "--prefix" in a else "print-"
        q = f'WorkflowId STARTS_WITH "{prefix}"' + (f' AND StartTime > "{since}"' if since else "")
        d = api("workflows?" + urllib.parse.urlencode({"query": q}))
        for e in d.get("executions", []):
            wid = e["execution"]["workflowId"]
            h = api("workflows/" + urllib.parse.quote(wid, safe="") + "/history")
            s = summarize(events_of(h))
            print(wid[:24], e["status"].replace("WORKFLOW_EXECUTION_STATUS_", ""), e.get("startTime", "")[:19], (e.get("closeTime") or "")[11:19],
                  "| subject", s["subject"], "| run", (s["run_id"] or "")[:8], "| sliced", s["sliced"], "| started", s["subtask"], (s["started"] or "")[11:19], "| last", s["last_activity"], "| final", s["final"], "| failure", s["failure"])
        return 0
    if a[0] == "show":
        if "--file" in a:
            h = json.load(open(a[a.index("--file") + 1])); show(events_of(h), h.get("workflowId", "")); return 0
        key = a[1]
        d = api("workflows?" + urllib.parse.urlencode({"query": f'WorkflowId STARTS_WITH "{key}"'}))
        ex = d.get("executions") or []
        if not ex:
            print("no workflow starts with", key); return 1
        wid = ex[0]["execution"]["workflowId"]
        show(events_of(api("workflows/" + urllib.parse.quote(wid, safe="") + "/history")), wid); return 0
    print(__doc__); return 2


sys.exit(main())
