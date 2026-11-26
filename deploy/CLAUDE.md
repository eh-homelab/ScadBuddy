# Deploy — notes for agents

These notes cover `deploy/`; paths are repo-relative; the root `CLAUDE.md` has the repo-wide rules.

## Layout
- `deploy/grafana/` — the ScadBuddy Grafana dashboards (#988, tracing spec §7): the
  overview `scadbuddy.json` (uid `scadbuddy`; never change it) and one `<name>.json` per
  area (`scadbuddy-api`, `-web`, `-renders`, `-printing`, `-agent`, `-filament`), each with uid
  `<name>` and its own `configMapGenerator` ConfigMap `<name>-dashboard` in
  `cattle-dashboards` for the rancher-monitoring sidecar, datasources only as the
  `DS_PROMETHEUS`/`DS_TEMPO` variables. A new dashboard is a new file plus its
  generator entry. clusters vendors the directory in `clusters/prod/scadbuddy/grafana/`
  with a `.revision` file (eh-homelab/clusters#3734), and `deploy.reusable.yml`
  replaces that copy with the deployed commit's files in the image's deploy PR. Only
  `kustomization.yaml` and `[a-z0-9-]+.json` files may live here (no subdirectory):
  the deploy rejects anything else.
  A query may read only series `core/metrics.py` or a camera module
  (`backend/scadbuddy/camera/*.py`, #7328) declares and span names the
  service emits (`lint-dashboard.sh` checks both, in every file); a span named at run
  time (a server span, `bambuddy.<op>`, Temporal's) is matched by attribute instead.

- `deploy/camera/` — MediaMTX's configs for the printer camera relay and recorders (#7328):
  `mediamtx-relay.yml` and `mediamtx-recorder.yml`, copied into the `camera-relay` and
  `camera-recorder` images at `/etc/mediamtx/mediamtx.yml`. The real-MediaMTX e2e
  (`backend/tests/camera/test_e2e_mediamtx.py`) runs them too. Their comments cite
  MediaMTX's docs and the measurements they rest on; a MediaMTX bump re-measures them (the
  camera spec §2).

- `deploy/cups/` — the label print server's `cupsd.conf`, `start.sh` and `zebra` backend,
  copied into the Dockerfile's `cups` image (`ghcr.io/eh-homelab/scadbuddy-cups`). One
  queue per label size comes from the Deployment's `CUPS_QUEUES`; the backend holds a job
  until the printer's calibrated label length matches its queue. State (queues, jobs) is
  under `/var/lib/cups`, one volume; admin needs the user `admin` and the password mounted
  at `CUPS_ADMIN_PASSWORD_FILE`. `README.md` there has the variables and the
  printer's measured facts. Its pin in eh-homelab/clusters is moved by hand, as the camera
  images' are.

- `deploy/security/` — the browser and parser pods' Localhost profiles (#6779):
  `seccomp/intake-sandbox.json`, `seccomp/profile-fetch.json` (containerd v2.2.7's default
  for a pod with no capabilities, plus only what each pod's sandbox needs) and the AppArmor
  profiles `apparmor/scadbuddy-intake-sandbox`, `apparmor/scadbuddy-profile-fetch`. CI runs
  the intake sandbox and the profile-fetch driver tests under them. The profile-fetch
  files are the browser pod's (`scadbuddy-profile-fetch-browser`, #6780); the worker pod
  runs no browser and keeps the runtime's default. eh-homelab/clusters
  keeps byte-for-byte copies in `applications/scadbuddy/` with their sha256 in
  `apparmor-loader.yaml`: change a file here, then there. What each rule is for, and how it
  was measured: `docs/security/trust-boundaries.md`, "Browser and parser pod profiles".

## Prod checks
- kubectl context `primary`, namespace `bambuddy`. Postgres is pod `scadbuddy-db-1`,
  container `postgres`
  (`kubectl -n bambuddy exec scadbuddy-db-1 -c postgres -- psql -U postgres -d scadbuddy -tAc …`).
  Read only whether a secret is set, never its value.
- A merged eh-homelab/clusters change is live only once Argo's `scadbuddy` app has
  `.status.operationState.phase` `Succeeded` with `.syncResult.revision` at or after the
  merge. While another sync is running (an API rollout), the change is applied on the next
  one (2026-11-14, clusters#3578).
- Temporal's `matching.numTaskqueueReadPartitions` must equal `TASK_QUEUE_PARTITIONS` in
  `backend/scadbuddy/workflows/pollers.py` and `agent/src/temporal/pollers.ts` (4,
  Temporal's default). Both read a queue's pollers partition by partition (#7306, #7350); a
  worker that polls only a higher partition is not seen, and nothing reports it. Prod's
  dynamic config (`kubectl -n bambuddy get configmap scadbuddy-temporal-v2-dynamicconfig -o yaml`,
  in eh-homelab/clusters) does not set it (read 2026-11-24). Set it there only together with
  both constants.

## See also

- Metrics series come from `backend/scadbuddy/core/metrics.py`: see `backend/CLAUDE.md`.
