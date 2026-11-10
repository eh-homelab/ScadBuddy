# Storefront phase 5: go live — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The storefront runs in prod as its own Deployment on `https://michaels-workshop.nullreference.io`, with no database secret, the back office host unchanged, and a customer can browse, customize, submit, answer a revision and pay with the fake provider there.

**Architecture:** The same image runs `python -m scadbuddy.storefront` (port 8083) as a new Deployment in eh-homelab/clusters (`applications/scadbuddy/`, namespace `bambuddy`). It is an HTTP client of the CRUD service (`http://scadbuddy:8082`) with a projected ServiceAccount token (#5325). Envoy Gateway serves the public host on the `public` ListenerSet behind Cloudflare. The host's `/api/v1/payments/webhooks/` prefix alone goes to the back-office API; everything else goes to the storefront.

**Tech Stack:** Kubernetes manifests (kustomize, ArgoCD), Envoy Gateway (Gateway API), external-dns with Cloudflare, cert-manager, the 1Password operator; ScadBuddy's deploy workflow (GitHub Actions); Playwright for the live check.

**Spec:** `docs/superpowers/specs/2026-11-02-storefront-design.md` §10 and §11 item 5. The issue is #5284, epic #4962.

## Global Constraints

- Public host `michaels-workshop.nullreference.io`; `SCADBUDDY_STOREFRONT_PUBLIC_URL=https://michaels-workshop.nullreference.io` (the owner, 2026-11-07, #5284).
- The storefront pod holds no `SCADBUDDY_DATABASE_URL`, `PGPASSWORD`, `PGPASSFILE`, `SCADBUDDY_SECRET_KEY_FILE` or Temporal address (#5295; `storefront/settings.py` `check_environment` refuses to start with any of them).
- The back office host does not change: `scadbuddy.internal.nullreference.io` (LAN) and `scadbuddy.sso.nullreference.io` (Pomerium).
- No back-office route is reachable from the public host, except the prefix `/api/v1/payments/webhooks/` on the API (#4957 "Deployment").
- GitOps only: every cluster change is a PR in eh-homelab/clusters, never `kubectl apply`. Follow that repo's CLAUDE.md (kustomize render, kubeconform, the Gateway API defaulted fields written out, the route on the `public` ListenerSet with `cloudflare-proxied: "true"`).
- Secrets come from 1Password (`homelab - prod`) through `OnePasswordItem`. A generated secret is never printed.
- Each change ships as a PR, opened ready, with auto-merge. Fix every claude-review finding.

## Review Focus

- A request for a back-office path on the public host (`/api/v1/orders`, `/settings`, `/`-relative SPA routes of the back office) must answer the storefront's 404, never the back office.
- The storefront pod must not reach Postgres (5432) or Temporal: its egress policy allows only DNS, the API's CRUD port 8082, and the tracing endpoint if it is set.
- The CRUD port 8082 admits the storefront's pods: without that, every storefront call fails closed.
- The client IP: Envoy's `X-Forwarded-For` reaches the pod; `SCADBUDDY_TRUSTED_PROXIES` names the Envoy proxy pods' CIDR, so render limits and Turnstile see the visitor, not the proxy.
- A new manifest the deploy workflow does not pin stays on its first image forever.

---

### Task 1: The deploy workflow pins the storefront (ScadBuddy)

**Files:**
- Modify: `.github/workflows/deploy.reusable.yml` (a `MANIFEST_STOREFRONT` entry for `applications/scadbuddy/scadbuddy-storefront.yaml`, rewritten with the others, under the same `expect_one` guard), `.github/scripts/scadbuddy-deploy-fastpath.sh`, `.github/scripts/scadbuddy-prime-digests.sh`, `.github/workflows/scadbuddy-verify-deploy.yml`, and the tests of those scripts (`.github/scripts/*.test.sh`)
- Modify: `README.md` "Deploying": a Storefront section (the Deployment, its environment, no database secret, the host, the webhook prefix, the two network policies, the cookie key item, how to roll the key)

**Interfaces:** the deploy PR rewrites the storefront manifest's image and its `scadbuddy.eh-homelab.io/{version,revision,source}` annotations like every other ScadBuddy manifest.

- [ ] **Step 1:** In the scripts' test suites, add a case with a storefront manifest; watch it fail.
- [ ] **Step 2:** Add the manifest to each list; the deploy workflow treats a missing storefront file as an error only once it exists on clusters' main (until Task 2 merges, it is skipped with a notice, so the next deploy does not fail).
- [ ] **Step 3:** Run `bash .github/scripts/*.test.sh`, actionlint and shellcheck (the `lint` job).
- [ ] **Step 4:** Commit `ci(deploy): pin the storefront Deployment's image (#5284)`; PR, auto-merge.

### Task 2: The storefront Deployment and its host (eh-homelab/clusters)

**Files (clusters):**
- Create: `applications/scadbuddy/scadbuddy-storefront.yaml` (Deployment and Service), `applications/scadbuddy/pdb-storefront.yaml`, `applications/scadbuddy/networkpolicy-storefront.yaml`, `clusters/prod/scadbuddy/onepassworditem-storefront-cookie-key.yaml`, `clusters/prod/scadbuddy/httproute-storefront.yaml`, the host's TLS Secret source (a `Certificate` like `clusters/prod/scadbuddy/certificate.yaml`, or the Cloudflare origin cert, whichever that repo's CLAUDE.md requires for a Cloudflare-proxied public host), and its `ReferenceGrant` for `kind: ListenerSet`
- Modify: `applications/scadbuddy/serviceaccounts.yaml` (`scadbuddy-storefront`, `automountServiceAccountToken: false`), `applications/scadbuddy/kustomization.yaml`, `clusters/prod/scadbuddy/kustomization.yaml`, the API's ingress NetworkPolicy (port 8082 also admits `app.kubernetes.io/name: scadbuddy-storefront`), `applications/envoy-gateway-config/listenerset-public.yaml` (a listener `https-michaels-workshop` for the host), and the API Deployment `applications/scadbuddy/scadbuddy.yaml` (`SCADBUDDY_STOREFRONT_PUBLIC_URL`, which the CRUD service uses to build mail links)

**Interfaces and values:**
- Deployment `scadbuddy-storefront`, label `app.kubernetes.io/name: scadbuddy-storefront`, 2 replicas, the pinned ScadBuddy image, `command: ["python", "-m", "scadbuddy.storefront"]`, port 8083, readiness and liveness on `GET /healthz`.
- `serviceAccountName: scadbuddy-storefront`, `automountServiceAccountToken: false`, a projected token volume `{path: token, audience: scadbuddy, expirationSeconds: 600}` at `/var/run/secrets/scadbuddy` (copy `scadbuddy-render.yaml`).
- Environment: `SCADBUDDY_API_INTERNAL_URL=http://scadbuddy:8082`, `SCADBUDDY_SERVICE_TOKEN_FILE=/var/run/secrets/scadbuddy/token`, `SCADBUDDY_STOREFRONT_COOKIE_KEY_FILE` (a file from the cookie-key Secret), `SCADBUDDY_STOREFRONT_PUBLIC_URL`, `SCADBUDDY_TRUSTED_PROXIES` (the pod CIDR the Envoy proxy pods run in; measure it from the cluster's pod CIDR and write where it was read).
- `securityContext`: non-root, `readOnlyRootFilesystem: true`, no privilege escalation, all capabilities dropped; an `emptyDir` `/tmp` with `sizeLimit: 64Mi`; requests and limits sized from the render worker's.
- NetworkPolicy `scadbuddy-storefront`: ingress only from the Envoy proxy pods (the labels the API's policy uses) on 8083; egress only to kube-dns, the API pods on 8082, and the tracing endpoint on 4318 if the API traces there. No 5432, no Temporal.
- HTTPRoute `scadbuddy-storefront` on `parentRefs: [{kind: ListenerSet, name: public, sectionName: https-michaels-workshop}]`, host `michaels-workshop.nullreference.io`, annotation `external-dns.alpha.kubernetes.io/cloudflare-proxied: "true"`. Rules: `PathPrefix /api/v1/payments/webhooks/` → Service `scadbuddy` port 8080; `PathPrefix /` → Service `scadbuddy-storefront` port 8083. Every defaulted field written out.
- The cookie key: a 1Password item `scadbuddy-storefront-cookie-key` in `homelab - prod`, 48 random bytes made with `op item create --generate-password` (never printed), mapped to the Secret key the Deployment mounts.

- [ ] **Step 1:** Create the 1Password item (the one action outside git; record the item's name, not its value, on #5284).
- [ ] **Step 2:** Write the manifests; run the repo's render and validation (`./scripts/validate-rendered-manifests.sh`, kubeconform).
- [ ] **Step 3:** PR `feat(scadbuddy): the storefront on michaels-workshop.nullreference.io (eh-homelab/ScadBuddy#5284)`, auto-merge; fix every review finding.
- [ ] **Step 4:** After ArgoCD syncs: the pods are Ready; `curl -sI https://michaels-workshop.nullreference.io/` answers 200 with the storefront's CSP; `/api/v1/orders` answers the storefront's 404; `https://michaels-workshop.nullreference.io/api/v1/payments/webhooks/fake` reaches the API (its 4xx, not the storefront's 404); the back office hosts answer as before. Record each command and answer on #5284.

### Task 3: The live check with the fake provider

The fake provider (`SCADBUDDY_PAYMENTS_FAKE_PROVIDER`) marks a request paid from its own page, so it must not stay on in prod.

**Files (clusters):** `applications/scadbuddy/scadbuddy.yaml` (set `SCADBUDDY_PAYMENTS_FAKE_PROVIDER=1` on the API), then a second PR that removes it.

- [ ] **Step 1:** PR 1 sets the variable; after the sync, run the Playwright check against the live host: browse a listing, customize it, render, add to order, submit (a test email the shop owns), open the order link from the outbox, answer a revision the shop sends from the back office, accept the price, pay through the fake provider, and see "Paid" after a fresh read. Record each step's result on #5284.
- [ ] **Step 2:** PR 2 removes the variable at once, whatever the check found; cancel the test order in the back office.
- [ ] **Step 3:** Close #5284 when the check passes, then epic #4962 once every child is closed.
