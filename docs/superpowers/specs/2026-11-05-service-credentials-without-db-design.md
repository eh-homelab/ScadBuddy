# Service credentials without a database secret

Status: decided (#5325), secure by default; §11 records the decisions. Part of the epic #5295;
blocks #5298 and #5299. Builds on the backend-auth spec (`2026-11-01-backend-auth-design.md`,
"the auth spec": §7.1 shape, §7.2 issue and rotation, §8.1 route classes, §13 D4) and on
`docs/security/trust-boundaries.md` ("the trust page"), which states the rule this spec
serves.

Every fact about the code was read from this repository at `a5043224b` on 2026-11-05. Every
fact about the cluster was read from `eh-homelab/clusters` `origin/main` at `9d87fd9d` and
from the live cluster with read-only `kubectl --context primary` on 2026-11-05 (the
commands are in §2.4). Facts about Kubernetes cite its documentation. Nothing in §6 to §9
was built or run. A claim marked **not tested** is an inference.

## 1. The problem

The rule (trust page, "The rule"): only the CRUD service gets SQL access. Every other
process is an HTTP client of the CRUD service with a short-lived service credential.

Today each caller gets its credential **through SQL**. It reads its newest sealed token
from a view that only its own Postgres role may select, and it opens the token with the
KEK (`backend/scadbuddy/auth/service_credentials.py:239-292` `TokenReader`;
`agent/src/api/serviceCredential.ts:62-82`). When #5298 and #5299 remove each caller's
database secret, that read stops working. Each caller needs another way to get and renew
its credential. One choice applies to every caller (the trust page asked this; §11 decides it).

The auth spec chose the database read (§13 D4 (a)) and rejected projected service-account
tokens (D4 (b)) because they "tie the API to Kubernetes and need a fallback for compose
anyway". The rule of 2026-11-05 removes the premise of D4 (a): "The caller's Postgres role
is the root of its identity" (auth spec §7.2). This spec reopens D4.

## 2. What exists today

### 2.1 Issue, rotation and verification

- Shape: `sbsvc_<16 hex id>.<43 base64url chars>`, 32 random bytes
  (`service_credentials.py:63`, `:68`, `:104`). Sent as `Authorization: Bearer`.
- The table `service_credentials` holds `secret_hash` (SHA-256), `secret_sealed`,
  `dek_sealed` and `kek_id` (`backend/scadbuddy/migrations/20261102T0645Z_service_credentials.sql:5-17`).
  The CHECK allows the callers `agent`, `print`, `projects` only (`:7`).
- Lifetime: **30 days** (`LIFETIME`, `service_credentials.py:64`). Rotation is not "every
  7 days": the API mints the next credential when the newest live one has **fewer than 7
  days left** (`ROTATE_BEFORE`, `:65`; `rotate_due`, `:126-143`). So a new token appears
  about every 23 days, and the two overlap for 7 days. It also mints when the newest one is
  sealed under another KEK (`:141`).
- Who rotates: the API, at start after its migrations (`backend/scadbuddy/main.py:1075-1089`
  `_grant_and_rotate_at_start`) and in the auth sweep on the prune Schedule
  (`main.py:1092-1106`). Without the KEK it issues nothing (`main.py:1061-1072`).
- The seal exists only for the hand-off. Verification uses the hash only
  (`service_credentials.py:8-10`; auth spec §7.1: "the sealed copy exists only so the caller
  can be handed its token").
- Verification: `AuthGate` takes a bearer that starts with `sbsvc_`
  (`backend/scadbuddy/auth/gate.py:57`, `:88-99`) and calls `resolve`, which looks up the
  hash among live rows and writes `last_used_at` at most once a minute
  (`service_credentials.py:145-164`). So each request with a credential costs one `SELECT`.
- Authorization: the credential carries scopes, which are route classes
  (`service_credentials.py:43-47` `SCOPES`; `backend/scadbuddy/auth/routes.py:22-30`).
  `would_refuse` admits an `internal.*` route only for a credential with that class, and an
  `api` route only for one with `api` (`routes.py:172-192`). Today:

  | Caller | Scopes |
  |---|---|
  | `agent` | `api`, `internal.workspace` |
  | `print` | `internal.outputs`, `internal.library` |
  | `projects` | `api` (the whole class; the comment says "phase 5 narrows what it may assert", `service_credentials.py:41-42`) |

- Revocation: `POST …/revoke` in Settings sets `revoked_at`; a revoke of the live one mints
  a replacement (`service_credentials.py:174-182`; `backend/scadbuddy/api/service_credentials.py:97`;
  auth spec §7.2). These routes are `person` only (`routes.py:82-84`).

### 2.2 How each caller reads it

| Caller | Where | Reads | KEK needed for the credential |
|---|---|---|---|
| print worker | `backend/scadbuddy/worker.py:1158-1160` | `TokenReader("print", database_url, kek)`; re-read every hour, every minute while it has none (`service_credentials.py:233-236`) | yes |
| projects worker | `worker.py:1422-1424` | `TokenReader("projects", …)` | yes |
| projects in-process (the API serves the `projects` queue) | `backend/scadbuddy/flows/component.py:107-109` | the API reads its own database for the `projects` token, then sends it to itself over HTTP | yes |
| agent sidecar and agent worker | `agent/src/main.ts:297`; `agent/src/api/serviceCredential.ts:52-82` | `service_credentials_agent`; also `auth_sessions_agent` for a person's session (`agent/src/auth/browserSession.ts:93`) | yes |
| render worker | — | does not call the API today (auth spec §3) | — |
| slicer | — | transfer tokens, a MAC per request (`backend/scadbuddy/slicing/transfer_token.py:1-6`) | no (the API signs them) |

A reader without the KEK returns no token and sends no header (`service_credentials.py:259-261`,
`serviceCredential.ts:53`). The API serves that in `off` and `observe` only.

### 2.3 Why each pod holds the KEK today

The KEK is one key, `scadbuddy-agent-kek`, mounted in every pod but the slicer (trust page,
"What each pod holds"). The credential is only one of its uses:

| Pod | Uses of the KEK besides the credential |
|---|---|
| API | seals stored settings secrets (`backend/scadbuddy/library/settings_store.py:786`, `:1244-1282`); mints transfer tokens and run tokens (`slicing/transfer_token.py:28-30`; `backend/scadbuddy/auth/run_tokens.py:1-9`); mints the service credentials |
| render | opens the sealed settings it reads (Bambuddy render key, Google Fonts key) (clusters `applications/scadbuddy/scadbuddy-render.yaml:218-224`) |
| print | opens printer access codes (`worker.py:1182-1183`, `:1114-1117`); signs transfer tokens for the LAN print's blob reads (`worker.py:1270-1272`); opens sealed settings (clusters `scadbuddy-print.yaml:174-181`) |
| projects | required: flow payloads are sealed per run under keys the KEK wraps (`worker.py:1400-1404`; `backend/scadbuddy/flows/keys.py:25-39`); approval hash key (`flows/keys.py:53-59`) |
| agent (sidecar, worker) | opens the Claude credential, plugin secrets, push and channel secrets, OAuth grants; payload data keys; workspace sealing; approval hash and fetch-grant keys (`agent/src/main.ts:174-239`, `:262`, `:356`, `:384-397`) |

So "no credential in the database" removes one use of the KEK in each caller, not all of
them. §9 says what that means.

### 2.4 The cluster

Read on 2026-11-05:

- No ScadBuddy Deployment sets `serviceAccountName`. The API, render, print, projects,
  agent worker and agent-durable pods run as the namespace's `default` ServiceAccount, with
  the token mounted (automount not set). Only the slicer and `playwright-mcp` set
  `automountServiceAccountToken: false` (clusters `scadbuddy-slicer.yaml:89`,
  `playwright-mcp.yaml:62`; live: `kubectl -n bambuddy get deploy -o custom-columns=…serviceAccountName,…automountServiceAccountToken`).
- The namespace is `bambuddy`, which Bambuddy itself owns (clusters
  `clusters/prod/scadbuddy/kustomization.yaml:4-8`, `namespace-transformer.yaml`). The
  `default` ServiceAccount is shared with Bambuddy's slicer, `pgadmin` and
  `nas-share-backup`, which also run as `default` (live, same command). So the `default`
  ServiceAccount names no ScadBuddy caller.
- RBAC in `bambuddy`: Roles and RoleBindings exist only for the CNPG clusters
  (`bambuddy-db`, `scadbuddy-db`, `scadbuddy-temporal-db` and their `-barman-cloud`)
  (`kubectl -n bambuddy get sa,role,rolebinding -o name`). No ScadBuddy pod has RBAC.
- `kubectl auth can-i --list --as=system:serviceaccount:bambuddy:default -n bambuddy`:
  self-reviews, discovery, and GET on `/.well-known/openid-configuration` and
  `/openid/v1/jwks`. No resource verbs on Secrets, and no `tokenreviews`.
- The service-account issuer is `https://kubernetes.default.svc.cluster.local`; its
  `jwks_uri` is `https://192.168.11.51:6443/openid/v1/jwks`, signing alg `RS256`
  (`kubectl get --raw /.well-known/openid-configuration`). Every ServiceAccount may read
  both, through the default binding of `system:service-account-issuer-discovery` to
  `system:serviceaccounts` (live `get clusterrolebinding`; [Kubernetes: issuer
  discovery][k8s-discovery]).
- Server version `v1.35.9+rke2r1` (`kubectl version`).
- Secrets come from the 1Password operator: one `OnePasswordItem` per Secret, e.g. the KEK
  (`clusters/prod/scadbuddy/onepassworditem-agent-kek.yaml`) and each caller's database
  password (`onepassworditem-print-db.yaml`, `-projects-db.yaml`, `-render-db.yaml`,
  `-agent-db.yaml`).
- Every ScadBuddy Deployment rolls with `maxSurge: 1`, `maxUnavailable: 0`
  (clusters `scadbuddy.yaml:152-156`, `scadbuddy-agent-worker.yaml:45-49`,
  `scadbuddy-render.yaml:68-72`, `scadbuddy-print.yaml:88-92`, `scadbuddy-projects.yaml:90-94`).
  So an old pod and a new pod of each run side by side during a deploy.
- The agent sidecar reaches the API on loopback in the same pod (clusters
  `scadbuddy.yaml:521`); the agent worker through the Service
  (`scadbuddy-agent-worker.yaml:109-110`).

### 2.5 What Kubernetes gives a pod

Cited from the Kubernetes documentation:

- A projected `serviceAccountToken` volume has an `audience` and an `expirationSeconds`,
  which "defaults to 1 hour and must be at least 10 minutes (600 seconds)"
  ([projected volumes][k8s-projected]). A `subPath` mount never receives the refreshed token
  (same page).
- "The kubelet proactively requests rotation for the token if it is older than 80% of its
  total time-to-live (TTL), or if the token is older than 24 hours", and "the application is
  responsible for reloading the token when it rotates" ([configure service
  accounts][k8s-configure]).
- A pod-bound token carries the pod's name and uid, and since v1.32 (stable) the node's name
  and uid; it carries a JTI, shown by TokenReview as `authentication.kubernetes.io/credential-id`
  ([service accounts admin][k8s-admin]).
- Verification in your own code: "TokenReview API (recommended)" or OIDC discovery.
  TokenReview "invalidates tokens that are bound to API objects such as … Pods … when those
  objects are deleted … If you use OIDC validation instead, your clients continue to treat
  the token as valid until the token reaches its expiration timestamp." The application
  "should always define the audience that it accepts" ([service accounts][k8s-sa]).
- The pod's credentials "are revoked 60 seconds beyond the `.metadata.deletionTimestamp`"
  ([configure service accounts][k8s-configure]).
- Creating a TokenReview needs `create` on `tokenreviews.authentication.k8s.io`; the
  built-in ClusterRole `system:auth-delegator` grants it. **Not verified from the docs page
  text** (the fetch of the authentication page did not reach that section); it is the
  common practice and must be checked when the clusters PR is written.

## 3. The callers after the epic

| Caller | Language | Calls the CRUD service for | Untrusted code in its pod |
|---|---|---|---|
| render worker (new caller) | Python, `backend/` | render jobs, blob refs, assets (#5298; trust page "What changes") | template `pipeline/` Python until #5296 moves it out |
| print worker | Python | outputs, library (today), print runs, outcomes, slices, printer state (#5298) | none |
| projects worker | Python | flow host routes (today), flow runs, decisions, grants, payload keys (#5298) | none (Monty runs host functions only, trust page) |
| agent sidecar | TypeScript, in the API pod | public routes, workspace blobs (today), the agent's data (#5299) | none (role `api`, no turn) |
| agent worker | TypeScript | the same | Claude Code and its tools; an `allow_refused` package can turn Bash on (trust page) |
| agent-durable | Python | nothing; it runs no worker (trust page) | none |
| slicer | Rust | transfer URLs only | none |

## 4. Criteria

Each option is judged on: what each pod holds at rest; issue and rotation; revocation;
bootstrap on a fresh deploy and on a rolling deploy; what a stolen or compromised pod gets
and for how long; local dev in one process (`SCADBUDDY_TEMPORAL_WORKER_INPROCESS`), the
e2e stack (`e2e-stack/compose.yaml`, one `app` container with every worker in-process,
`:73-106`) and the test suites; and what the cluster must grant.

## 5. Options

### A. The CRUD service writes each caller's credential into a Kubernetes Secret

The API mints as today and writes the token into a Secret per caller
(`scadbuddy-svc-print`, …). Each pod mounts its Secret as a volume (not `subPath`, so the
kubelet updates it) and re-reads the file.

- At rest: each pod holds its plaintext token in a tmpfs file. The API's ServiceAccount
  holds `get`, `update`, `patch` on Secrets in `bambuddy`, and `create` for the first run.
- Issue and rotation: as today (30 days, 7 days' overlap), the write replacing the view.
  The kubelet updates a mounted Secret with a delay (the kubelet sync period plus its cache
  TTL, [Kubernetes: Secrets][k8s-secret-update]; the exact delay **not measured** here).
- Revocation: as today, plus a write of the replacement into the Secret.
- Bootstrap: a fresh deploy starts workers before the Secret exists; the volume needs
  `optional: true` or the pod stays in `ContainerCreating`. Rolling: old and new pods read
  the same Secret; works.
- Stolen pod: the token, for up to 30 days, from anywhere that reaches the API. Revoke by
  hand.
- Outside Kubernetes: needs another writer (a file the API writes into a shared volume).
  Compose can share a volume; tests write the file.
- Cluster: RBAC on Secrets. `create` cannot be limited by `resourceNames`
  ([RBAC: referring to resources][k8s-rbac]), and the namespace is Bambuddy's (§2.4), which
  holds Bambuddy's own Secrets (27 Secrets live: `kubectl -n bambuddy get secret -o name | wc -l`).
  A compromised API could then rewrite any Secret it may `update`. A token at rest in etcd
  (encryption at rest **not checked**) and in Velero backups unless excluded.
- Verdict: it moves a long-lived secret from Postgres into etcd and gives the API write
  access to Secrets in a shared namespace. No.

### B. Projected service-account tokens, exchanged at the CRUD service

Each Deployment gets its own ServiceAccount. Its pod mounts a projected token with
audience `scadbuddy` and a short `expirationSeconds`. The caller presents that token at an
exchange route and gets a short-lived ScadBuddy credential back. The CRUD service verifies
the Kubernetes token by **TokenReview** (B1) or offline against the issuer's JWKS (B2).
A variant accepts the Kubernetes token directly on every request (B3).

- At rest: each pod holds a JWT for its own ServiceAccount, good for 10 to 60 minutes, that
  the kubelet renews. No secret the operator makes or rotates. No KEK for the credential.
- Issue and rotation: the kubelet issues and rotates the JWT (§2.5). The CRUD service
  issues the ScadBuddy credential at each exchange; the caller exchanges again before it
  expires. No sweep, no 7-day overlap.
- Revocation: delete the pod. With B1, its JWT fails TokenReview at once and its next
  exchange is refused (§2.5); the credential it already holds lives until it expires or is
  revoked in Settings. With B2, the JWT stays valid offline until its `exp` (§2.5).
- Bootstrap: fresh deploy: a worker that starts before the API retries the exchange (as it
  retries the view today, `service_credentials.py:234-236`). Rolling: an old and a new API
  pod both verify any pod's JWT (TokenReview asks the apiserver; JWKS is the cluster's).
  Old workers keep reading the view while they still hold a database URL (§8).
- Stolen pod (an attacker with code execution in it): the JWT and the current credential,
  for as long as they control the pod, plus at most the JWT's lifetime (B2) or the
  credential's lifetime (B1) after it is deleted. Only that caller's scopes. A JWT copied
  out of the cluster is useless to the apiserver for other uses, because its audience is
  `scadbuddy` (§2.5).
- Outside Kubernetes: no kubelet. Needs a fallback (§7).
- Cluster: a ServiceAccount per Deployment; `automountServiceAccountToken: false` on every
  ScadBuddy pod; one projected volume per caller container. B1: the API's ServiceAccount
  needs `system:auth-delegator` (a ClusterRoleBinding, §2.5), and the API pod needs an
  apiserver token to call TokenReview. B2: nothing new; any ServiceAccount may read the
  JWKS (§2.4). The JWKS URL is a control-plane node's address; whether pods reach
  `192.168.11.51:6443` is **not tested**, and no egress policy selects the API pod today
  (trust page).
- B3 (accept the JWT on every request): each request then costs a TokenReview (B1) or
  a signature check (B2), and the JWT goes into every request log path that might keep a
  header. Scopes and revocation in ScadBuddy's own table are lost. No.

### C. A credential sealed under the KEK, delivered at start

The CRUD service hands each caller its sealed token (a file, an env value, or an
unauthenticated route), and the caller opens it with the KEK.

- At rest: each pod holds the KEK, which opens every sealed secret ScadBuddy has: the
  Bambuddy key, the Claude credential, every payload key (§2.3). The sealed token itself
  adds little.
- The route that delivers it must know the caller without a credential, which is the
  problem to solve. A file needs a writer (option A's problem).
- Stolen pod: the KEK, which is the root of every sealed value, until the owner rotates it
  by hand (docs/ai/operating.md §3.3). A stolen KEK plus a database dump opens everything.
- The KEK in every pod is the cost the epic wants to end (trust page, gap 4: an
  `allow_refused` package can read the KEK). C ties the credential to the widest secret.
- Verdict: no. It keeps today's dependency on the KEK and only moves the database read.

### D. A static per-caller bootstrap secret from 1Password, exchanged for short-lived tokens

Each caller gets a random secret in a 1Password item, synced to a Secret by the operator
(as the database passwords are, §2.4). The caller presents it at the exchange route; the
CRUD service compares its hash and issues the short-lived credential.

- At rest: each pod holds a long-lived secret. The CRUD service holds the hashes (a file
  or env from the same items).
- Rotation: by hand in 1Password, then a restart of the caller and the CRUD service in the
  right order, or a two-secret overlap. Today's database passwords rotate the same way
  (`onepassworditem-print-db.yaml` comment: the pods "need a restart").
- Revocation: change the item; until then the secret works from anywhere that reaches the
  API.
- Bootstrap: no ordering problem; the Secret exists before the pod.
- Stolen pod: the bootstrap secret, which mints fresh credentials until a person rotates
  it.
- Outside Kubernetes: works as it is (a file or env). That is its strength.
- Cluster: one `OnePasswordItem` per caller, nothing else.
- Verdict: the right fallback outside Kubernetes (§7), weaker than B inside it.

### E. Other options considered

- **E1. Mutual TLS from a cluster CA (cert-manager, SPIFFE/SPIRE).** The pod's identity is a
  short-lived certificate. Strong, but it needs an issuer per workload, TLS termination in
  the API (today plain HTTP on 8080), and a fallback outside Kubernetes. More moving parts
  than B for the same property. Not recommended now.
- **E2. The parent hands the credential in-process.** When the API itself serves a queue
  (`SCADBUDDY_TEMPORAL_WORKER_INPROCESS`, `flows/component.py:107-109`), the API can mint a
  credential in memory and give it to the in-process caller. No exchange, no file. This is
  the dev and e2e answer for one process (§7), not a cluster answer.
- **E3. Signed self-contained ScadBuddy tokens** (a MAC under a key derived from the KEK, as
  the transfer and run tokens are). Verification needs no `SELECT`, but revocation then
  needs a deny list, and Settings loses "last used". The current opaque token with one
  indexed `SELECT` per request is cheap enough. Keep it opaque.

### 5.1 Comparison

| | A Secret | B1 SA token + TokenReview | B2 SA token + JWKS | C sealed + KEK | D 1Password bootstrap |
|---|---|---|---|---|---|
| Pod holds at rest | token, 30 d | JWT, 10 min | JWT, 10 min | KEK (opens everything) | static secret |
| Rotation | API, 23 d | kubelet + exchange, automatic | same | API, 23 d | by hand |
| Revocation | Settings | delete pod: next exchange fails at once; Settings | Settings; JWT lives to `exp` | Settings; KEK by hand | 1Password + restart |
| Fresh deploy | needs `optional` volume | retry | retry | needs a writer | none |
| Rolling deploy | ok | ok | ok | ok | ok |
| Stolen pod gets | 30 d token | ≤ credential life after pod deleted | ≤ JWT life + credential life | KEK, until rotated | mint until rotated |
| Outside Kubernetes | shared file | needs D or E2 | needs D or E2 | file | works |
| Cluster grants | Secrets RBAC in a shared namespace | per-caller SAs, `system:auth-delegator` | per-caller SAs | none new | one item per caller |
| KEK in callers | no | no | no | yes | no |

## 6. Recommendation

**B1 in Kubernetes, D outside it, E2 in one process.** Every caller uses one exchange route
and gets the same short-lived opaque `sbsvc_` credential. Only the proof it presents
differs.

### 6.1 Identity per caller

| Caller | Kubernetes proof (ServiceAccount, audience `scadbuddy`) | Caller name | Container that mounts the token |
|---|---|---|---|
| render worker | `scadbuddy-render` | `render` (new) | the worker container only |
| print worker | `scadbuddy-print` | `print` | the worker |
| projects worker | `scadbuddy-projects` | `projects` | the worker |
| agent worker | `scadbuddy-agent-worker` | `agent` | the agent |
| agent sidecar | `scadbuddy` (the API pod's) | `agent` | the `agent` container only |
| agent-durable | none: `automountServiceAccountToken: false`, no projected token | — | — |
| slicer | none, unchanged (transfer tokens) | — | — |

The CRUD service keeps a fixed map from `system:serviceaccount:bambuddy:<name>` to the
caller name. An unknown ServiceAccount is refused. `default` is never in the map (§2.4: it
is shared with Bambuddy's pods).

The sidecar shares the API pod's ServiceAccount. The API container is the CRUD service and
already holds the database, so the API container being able to act as `agent` is no
escalation. The projected volume is mounted in the `agent` container only, and the API
container keeps only the apiserver token it needs for TokenReview. A pod's containers all
share its ServiceAccount; a per-container mount limits which file a container sees, not
which identity the pod has.

### 6.2 The exchange

`POST /api/v1/auth/service-token`, a new route class `exchange`: no cookie, no service
credential, the proof in the body. Shaped after OAuth 2.0 Token Exchange ([RFC 8693][rfc8693]):

```
POST /api/v1/auth/service-token
Content-Type: application/x-www-form-urlencoded

grant_type=urn:ietf:params:oauth:grant-type:token-exchange
&subject_token=<the projected JWT, or the bootstrap secret>
&subject_token_type=urn:ietf:params:oauth:token-type:jwt      (Kubernetes)
                  | urn:scadbuddy:params:token-type:bootstrap (outside Kubernetes)
```

Answer `200 {"access_token": "sbsvc_…", "token_type": "Bearer", "expires_in": 900,
"scope": "<classes>"}`, or `401` with the auth spec's problem body (§8.2 there).

The CRUD service, for a JWT:

1. Sends a TokenReview with `spec.audiences: ["scadbuddy"]`. Refuses unless
   `status.authenticated` is true and `status.audiences` contains `scadbuddy`.
2. Maps `status.user.username` to the caller (§6.1). Refuses an unmapped one.
3. Reads the pod name and uid from `status.user.extra`
   (`authentication.kubernetes.io/pod-name`, `…/pod-uid`) and the JTI
   (`…/credential-id`) (§2.5).
4. Mints a credential: a row in `service_credentials` as today, with new columns
   `proof` (`legacy` for a sealed 30-day row, `k8s`, `bootstrap`, `inprocess`), `pod_name`, `pod_uid`, and **no sealed
   copy**. `expires_at` = now + 15 minutes.

For a bootstrap secret: the SHA-256 of the presented value is compared, in constant time,
with the hash for each caller that the CRUD service read at start (§7.2).

Rate limit: the route answers at most a few exchanges a minute per caller and pod, so a
loop cannot fill the table: 6 a minute per pod and 30 a minute per caller, then `429` with
`Retry-After` (§11 D5).

### 6.3 Token format and lifetime

- The Kubernetes token: `expirationSeconds: 600`, the minimum (§2.5). The kubelet renews it
  at 80%, so after 8 minutes. The caller re-reads the file before each exchange, never
  `subPath` (§2.5).
- The ScadBuddy credential: today's opaque `sbsvc_<id>.<secret>` (§2.1), so `AuthGate`,
  `resolve` and the scopes do not change. Lifetime **15 minutes** (§11 D4). The caller exchanges again at
  half-life (7.5 minutes) and at once on a `401` from any route; on a failed exchange it keeps
  the current credential until it expires and retries every 5 s doubling to 60 s.
- Rows: about one per pod per 7.5 minutes; 11 caller pods today (§2.4 replicas: render 2,
  print 1, projects 1, agent worker 3, API 3 sidecars, agent-durable 0) make about 2,100 rows
  a day. `delete_expired` already forgets rows expired more than `older_than` ago
  (`service_credentials.py:193-204`); its default becomes a day for `proof` ≠ `legacy`.

### 6.4 Authorization: one scope set per caller, deny by default

Scopes stay route classes (§2.1). A route is in exactly one class, and the completeness
test already fails for an unclassified route (`routes.py:1-7`). Each new CRUD route that
#5298 and #5299 add belongs to a class of its caller's, named in its first PR:

| Caller | Classes |
|---|---|
| `render` | `crud.render` |
| `print` | `internal.outputs`, `internal.library`, `crud.print` |
| `projects` | `flow.host` (the flow host routes the auth spec §7.1 names: `render`, outputs create, the print queue, `arrange`, `operations` reads), `crud.projects` |
| `agent` | `api`, `internal.workspace`, `crud.agent` |

- Today `would_refuse` checks the credential's classes for `internal.*` and `api` only
  (`routes.py:172-192`). The change makes it one rule for a service credential: it passes a
  route only when the route's class is in its scopes. Any other class is refused.
- `projects` loses `api`: a new class `flow.host` holds the routes `workflows/flow_routes.py`
  calls. This keeps the auth spec §7.1's table, which today's code widens (§2.1).
- `agent` keeps `api`: its tools call almost every public route (`agent/src/tools/coverage.ts`
  requires a tool or an entry for every operation). That is the widest scope, held by the
  pod with the least trusted code (§3). `enforce` does not wait for #779 (the sandbox pod);
  until #779, no package can switch `Bash` or `PowerShell` on (§11 D3, #5296).
- `crud.<caller>` classes are disjoint: no caller can call another caller's CRUD routes.

### 6.5 Revocation

- One credential: Settings, as today (§2.1). The exchange then mints a new one at the next
  exchange; for a compromised pod that is not enough, so:
- A pod: delete it. Its JWT fails TokenReview at once (§2.5), so it cannot exchange again.
  Settings gets "revoke every credential of this pod" (by `pod_uid`). The pod then holds
  nothing that works.
- A caller: Settings "suspend caller" refuses its exchanges and revokes its live rows,
  until resumed. This is new and stored in the `service_credentials` feature, not in the
  environment.

## 7. Outside Kubernetes

### 7.1 One process (dev, the e2e stack, most tests)

With `SCADBUDDY_TEMPORAL_WORKER_INPROCESS` (or the projects or print variant), the API
serves the caller's queue itself. The API mints a credential with `proof = inprocess` and
gives the caller an in-memory provider that renews it in-process (E2). No route, no file.
This covers `e2e-stack/compose.yaml` (one `app` container, `:84-91`) and
`flows/component.py:107-109`.

### 7.2 Several processes without Kubernetes (compose, a laptop with a separate worker)

The bootstrap secret of option D. The CRUD service reads
`SCADBUDDY_SERVICE_BOOTSTRAP_DIR`, one file per caller (`render`, `print`, `projects`,
`agent`), and keeps only their hashes. Each caller reads
`SCADBUDDY_SERVICE_BOOTSTRAP_FILE`. A compose file mounts the same secret to both. A caller
with neither a projected token path nor a bootstrap file gets no credential, which `off`
and `observe` still serve, as today (§2.2).

The caller picks its proof by configuration only:
`SCADBUDDY_SERVICE_TOKEN_FILE` (a projected JWT) or `SCADBUDDY_SERVICE_BOOTSTRAP_FILE`.
Setting both is a start error. The CRUD service accepts the bootstrap proof only when the
directory is set, so a cluster that does not set it has no static secret to steal.

### 7.3 Tests

- Backend: the TokenReview call sits behind a `ProofVerifier` protocol. Tests use a fake
  that maps fixed JWT strings to ServiceAccounts, and the bootstrap verifier with a temp
  directory. The existing tests that read the view
  (`backend/tests/auth/test_service_credentials.py:39-69`) move to the exchange.
- Agent: `agent/test/serviceCredential.test.ts` tests the exchanging reader against a
  fake backend route.
- Real TokenReview is never called in CI. The clusters PR is checked on the cluster by a
  one-off `kubectl create token scadbuddy-print --audience scadbuddy` and a call to the
  route (§8 step 3).

## 8. Migration, with no downtime

Every step merges with the mode where it is today, and old and new builds run side by side
(§2.4: `maxSurge: 1`).

1. **Backend and agent, one PR each.** The exchange route, the `exchange` class, the
   TokenReview and bootstrap verifiers, the in-process provider, the caller `render`, and
   the new columns (a new migration that also widens the `caller` CHECK, CLAUDE.md
   "Migrations"). The readers (`TokenReader`, `ServiceCredential`) get an exchange source,
   used when `SCADBUDDY_SERVICE_TOKEN_FILE` or `_BOOTSTRAP_FILE` is set; **otherwise they
   read the view as today**. An exchange that answers `404` (an old API pod during the
   roll) falls back to the view while the caller still has a database URL. Both kinds of
   token resolve through the same `resolve`, so any API pod accepts either.
2. **Clusters PR.** A ServiceAccount per Deployment (§6.1); `automountServiceAccountToken:
   false` on every ScadBuddy pod; the projected volume and `SCADBUDDY_SERVICE_TOKEN_FILE`
   on each caller container; the API pod's ServiceAccount bound to `system:auth-delegator`
   and its own apiserver token mounted in the API container only. Merge after step 1's
   image is deployed.
3. **Check.** Settings lists each caller's credentials with `proof = k8s` and a `pod_name`;
   `scadbuddy_auth_would_refuse_total` stays at zero (auth spec §9.1). Add a counter
   `scadbuddy_service_token_exchanges_total{caller, proof, result}` in `core/metrics.py`.
4. **Stop the database path.** When every caller exchanges (no `legacy` row used for a
   week; `last_used_at` shows it), the API stops minting sealed 30-day rows; a new
   migration drops the views, `grant_caller_views` and the sealed columns' NOT NULL; then a
   later one drops the columns. `TokenReader`'s database path and
   `ServiceCredential.read` are removed. This step is the prerequisite #5298 and #5299 need
   before they remove a caller's database secret.

## 9. What it means for the KEK

The credential stops needing the KEK in every caller (§6.2 mints no sealed copy). That
alone removes the KEK from no pod, because each caller has other uses (§2.3). After the
epic:

| Pod | Still needs the KEK? | Why, and what would remove it |
|---|---|---|
| CRUD service (API) | yes | it seals and opens every stored secret and derives the transfer and run token keys |
| render | no, if the CRUD routes return the opened render settings it needs (the render key, the Google Fonts key) to the `render` credential | #5298 |
| print | no, if the CRUD service opens printer access codes for `crud.print` and mints the LAN blob transfer URLs (as it does for the slicer) | #5298 |
| projects | yes, unless the CRUD service releases a run's unwrapped payload data key to `crud.projects` (a key-release route) | #5342 (§11 D6) |
| agent (sidecar, worker) | yes for the payload codec and workspace sealing, unless the same key release serves `crud.agent`; the Claude credential stays behind the agent's loopback gateway | #5342 (§11 D6) |
| agent-durable | no | it runs no worker (trust page gap 7) |
| slicer | no | unchanged |

A key-release route moves the KEK to one pod, at the cost of a pod that can ask for any
subject's data key with its scope. That still beats the KEK in every pod: a stolen key is
one subject's, and each release is a logged request.

## 10. What a stolen pod gets, after this design

| Pod | Gets | For how long after the pod is deleted |
|---|---|---|
| render (with template Python until #5296) | a `crud.render` credential and the JWT | the credential: ≤ 15 min, or until revoked by pod; the JWT: refused at once |
| print | `internal.outputs`, `internal.library`, `crud.print` | the same |
| projects | `flow.host`, `crud.projects`; the KEK until §9 | the same; the KEK until rotated |
| agent worker | `api`, `internal.workspace`, `crud.agent`; the KEK until §9 | the same; the KEK until rotated |

Today the same pods get a database password and the KEK, good until a person rotates them
(trust page, "What each pod holds").

## 11. Decisions

Each one takes the more secure option. The cost is stated with it.

1. **D4 is reopened.** In the cluster a caller proves itself with its projected
   ServiceAccount token, checked by TokenReview (B1). Outside it, compose uses the per-caller
   bootstrap secret file (§7). The database view goes once every caller exchanges (§8).
   Cost: the API depends on the Kubernetes API for each exchange.
2. **TokenReview, not offline JWKS.** The API's ServiceAccount gets `system:auth-delegator`.
   A deleted pod's token is refused at once, and the pod name and uid are bound to each
   credential. Cost: one cluster-wide `create` grant on TokenReview and SubjectAccessReview,
   held by the CRUD service only.
3. **`enforce` does not wait for #779.** It narrows every caller now. Until #779 lands, no
   plugin package can switch `Bash` or `PowerShell` on, `allow_refused` or not, so no
   model-driven shell runs in a pod that holds the agent's credential (#5296). Cost: a
   package that needs a shell waits for the sandbox pod.
4. **Lifetimes.** The Kubernetes token lives 600 s, the minimum. The ScadBuddy credential
   lives 15 minutes and is renewed at half-life, so a stolen one is good for at most
   15 minutes. Cost: about 2,100 rows a day (§6.3), pruned after a day.
5. **Rate limit.** 6 exchanges a minute per pod and 30 a minute per caller, then `429` with
   `Retry-After`. A renewing pod needs about one exchange every 7.5 minutes.
6. **The KEK goes to the CRUD service only.** A key-release route hands a subject's
   unwrapped data key to a caller whose scope covers that subject, and logs each release;
   secrets a caller needs in plaintext (printer access codes, render keys) come from routes
   for that purpose. Then the KEK leaves every other pod (#5342). Cost: one more route
   class to scope and audit.
7. **ScadBuddy moves to its own namespace first** (eh-homelab/clusters#3200), with one
   ServiceAccount per Deployment and `automountServiceAccountToken: false` on every pod.
   Its ServiceAccounts and grants then never sit beside Bambuddy's. Cost: one migration of
   every ScadBuddy object, done before the ServiceAccounts.
8. **The render worker becomes a caller now** with `crud.render` only, so #5298 can take its
   database secret away before #5296 moves template Python out. Template code then reaches
   at most a 15-minute `crud.render` credential, not a database password.

## 12. What was not verified

- The `system:auth-delegator` ClusterRole's exact rules (§2.5); no TokenReview was run.
- Whether a pod can reach the JWKS URL `https://192.168.11.51:6443/openid/v1/jwks`.
- Whether etcd encrypts Secrets at rest (option A).
- The kubelet's delay for a mounted Secret update in this cluster (option A).
- That a TokenReview on v1.35 returns `pod-name`, `pod-uid` and `credential-id` in
  `status.user.extra` for a projected token: taken from the documentation's example
  ([service accounts admin][k8s-admin]), not run here.
- Every number of rows and requests in §6.3 is arithmetic from today's replica counts.

[k8s-projected]: https://kubernetes.io/docs/concepts/storage/projected-volumes/
[k8s-configure]: https://kubernetes.io/docs/tasks/configure-pod-container/configure-service-account/
[k8s-discovery]: https://kubernetes.io/docs/tasks/configure-pod-container/configure-service-account/#service-account-issuer-discovery
[k8s-admin]: https://kubernetes.io/docs/reference/access-authn-authz/service-accounts-admin/
[k8s-sa]: https://kubernetes.io/docs/concepts/security/service-accounts/
[k8s-secret-update]: https://kubernetes.io/docs/concepts/configuration/secret/#using-secrets-as-files-from-a-pod
[k8s-rbac]: https://kubernetes.io/docs/reference/access-authn-authz/rbac/#referring-to-resources
[rfc8693]: https://www.rfc-editor.org/rfc/rfc8693
