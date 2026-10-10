# Durable phase 5e: Deploy — Plan

**Goal:** The `agent-durable` worker runs in production, so new chats start durable (5d Ruling 1) instead of falling back to classic (5d Ruling 2b).

**Architecture:** Two PRs. This repository publishes and pins the image. eh-homelab/clusters runs it as a third container in the ScadBuddy pod and narrows who may reach the Temporal frontend.

**Spec:** `docs/superpowers/specs/2026-10-01-durable-printing-agents-flows-design.md` §6.3a (the container), §6.6 ("The route is the Update's only legitimate caller"), §10 phase 5. Sub-plan table: `2026-10-08-durable-phase-5a-agent-durable-foundation.md` (row 5e). Consumes "What 5d and 5e need from 5c" (`2026-10-09-durable-phase-5c-durable-session.md`) and "What 5e needs from 5d" (`2026-10-09-durable-phase-5d-mode.md`). Issue #1056.

## Rulings

- **Ruling 1, the image carries its own paths.** 5c listed `SCADBUDDY_DURABLE_TOOLS_JSON` and `SCADBUDDY_DURABLE_SKILLS_DIR` as deploy inputs, but the 5a stage shipped no manifest. The stage now copies `dist/tools.json` from `agent-build`, the same build as the `agent` image that serves those tools on `agent-tools`. It sets `SCADBUDDY_DURABLE_TOOLS_JSON`, `SCADBUDDY_DURABLE_SKILLS_DIR=/app/plugins/scadbuddy` (skills only, §6.3b) and `SCADBUDDY_DURABLE_CWD=/srv/agent` as `ENV`. clusters sets only the infrastructure variables the agent also has. The build parses the manifest beside the bundled-CLI check, so a bad one fails the build, not the worker's start.
- **Ruling 2, cache.** The stage now depends on `agent-build` (and through it `api-spec` on `base`). Its `type=gha` scope stays `agent-durable` at `mode=min` (CLAUDE.md, #743) and also reads `scope=agent`, whose `mode=max` index holds that chain. The same applies in `build-image.yml` and in `ci.yml`'s `agent-durable` job, whose timeout goes from 20 to 35 minutes for the extra stages.
- **Ruling 3, the publish job is `continue-on-error`**, as the agent's first publish was. A new GHCR package is private until its visibility is changed by hand (`build-image.yml` header), and that must not hold back a backend deploy. Once clusters names the image, a failed build still stops the deploy (Ruling 4).
- **Ruling 4, the pin is optional until clusters has the line, then strict.** The two PRs merge in order: this one first, so the image exists, and clusters second. So `deploy.reusable.yml` pins the `scadbuddy-agent-durable` line when `scadbuddy.yaml` has one, and says so in the deploy PR. A missing line is a notice. With the line present, it fails the deploy when:
  - the build published no such image (a rollback to before 5e), since a stale durable image beside a new agent could disagree on the tool manifest;
  - there is a near-miss image line;
  - there are two lines.

  Its digest is resolved from the tag, as the agent's is. The resolve waits up to 10 minutes and then warns rather than fails, so a build without the image still deploys a manifest without the line. `deploy-pin.test.sh` covers the cases (7b–7f). Making the line required, as the agent's is (#738), is a later one-line change once clusters has carried it for a while.
- **Ruling 5, no readiness probe** (clusters). The agent container has none, deliberately: readiness is per pod, and a not-Ready sidecar takes the backend's `:8080` out of the Service (clusters#1526). `/healthz` answers 200 while the process serves, whatever the worker's state, so a readiness probe would add nothing over liveness. Liveness only, with `initialDelaySeconds` like the agent's.
- **Ruling 6, the Temporal frontend's ingress policy** (§6.6). The spec calls it a `CiliumNetworkPolicy`. The repository's existing policies are all `networking.k8s.io/v1` `NetworkPolicy`, which Cilium enforces, and an L3/L4 rule needs nothing Cilium-specific, so the policy is one of those. It selects the frontend pods. It admits `7233` (gRPC) and `7243` (HTTP API) only from:
  - the ScadBuddy pod (API, agent, agent-durable);
  - the render and print workers;
  - Temporal's own services (the UI included);
  - the search-attributes hook Job;
  - the Temporal operator.

  `6933` (membership) is open only to Temporal's own pods. `9090` (metrics) stays open, since Prometheus and Alloy scrape it. The client list was taken from Hubble flows to the frontend pods on 2026-10-10 and from the repository's manifests. Kubelet probes come from the host, which Cilium always admits. Egress is untouched: the Temporal pods' archival egress to DO Spaces (clusters#1922) is not narrowed.
- **Ruling 7, several replicas are safe.** A session is one `DurableSession` workflow, and the conversation lives in it (5c Ruling 1), so any `agent` worker continues it. `agent-tools` activities run in whichever pod's agent polls. The only pod-local state a tool needs is a browser tab's bridge socket, and `browser_*` calls reach a tab on another replica through `PgTabRelay` (#1916, `agent/src/bridge/relay.ts`). Two known limits: a NOTIFY lost while the listener reconnects makes that call time out, and an owner that does not ack within 3 s answers "not connected".
- **Ruling 8, resources** (clusters). Nothing is measured yet, so these are sized from the image. The worker is one Python process: temporalio's Rust core, psycopg and the SDK, about 100–150 MiB idle. Each running segment adds one bundled Claude Code CLI, about 200–400 MiB, the same binary that makes up most of the agent's measured footprint (p95 ~370 MiB, max 1.07 GiB, with Chromium). There is no Chromium here. Requests are `cpu: 100m`, `memory: 384Mi` (the worker plus one CLI). Limits are `cpu: "2"` and `memory: 2Gi`, room for about four concurrent segments. An OOM kill restarts only this container, but while it is down the pod is not Ready, so the limit is generous. `/srv/agent` and `/tmp` are `emptyDir`s with a `sizeLimit` of `2Gi` each, as the agent's are. Re-size from a week of Prometheus data, as the agent was.

## Done when

- [ ] `build-image.yml` publishes `ghcr.io/eh-homelab/scadbuddy-agent-durable:sha-<short>` from main, and the package is public.
- [ ] The clusters PR merges, and ArgoCD rolls the pod with the third container Ready.
- [ ] `/healthz` on 8082 says `"worker": "running"`, and its log shows no error and no secret.
- [ ] `temporal task-queue describe --task-queue agent --namespace scadbuddy` lists a poller.
- [ ] `GET /api/v1/ai/settings/session-mode` answers `durable_available: true`.
- [ ] The next deploy PR (`deploy/scadbuddy`) moves the durable line with the other two.

## Still open after 5e

- From 5c/5d: `wait_for_user` is `proceed`-only (5d Ruling 8). The stuck-`running` reaper is merged (#2075–#2080).
- Per-client authorization at the Temporal frontend (§6.6, "Residual risk"): the render worker can still reach the frontend, so it could forge a `respond`.
- The durable pin becomes required in `deploy.reusable.yml` (Ruling 4).
