# Dashboards — notes for agents

These notes cover `deploy/`; paths are repo-relative; the root `CLAUDE.md` has the repo-wide rules.

## Layout
- `deploy/grafana/` — the ScadBuddy Grafana dashboards (#988, tracing spec §7): the
  overview `scadbuddy.json` (uid `scadbuddy`; never change it) and one `<name>.json` per
  area (`scadbuddy-api`, `-web`, `-renders`, `-printing`, `-agent`), each with uid
  `<name>` and its own `configMapGenerator` ConfigMap `<name>-dashboard` in
  `cattle-dashboards` for the rancher-monitoring sidecar, datasources only as the
  `DS_PROMETHEUS`/`DS_TEMPO` variables. A new dashboard is a new file plus its
  generator entry. clusters will pull them in as a remote
  resource pinned to a full SHA, once clusters#1596 Phase 5 adds the line, and
  `deploy.reusable.yml` moves that `ref` with the image.
  A query may read only series `core/metrics.py` declares and span names the
  service emits (`lint-dashboard.sh` checks both, in every file); a span named at run
  time (a server span, `bambuddy.<op>`, Temporal's) is matched by attribute instead.

## See also

- Metrics series come from `backend/scadbuddy/core/metrics.py`: see `backend/CLAUDE.md`.
