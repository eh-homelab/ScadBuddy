# Models repository switch: runbook

How to put `data/models-next` in the place of `data/models` on prod (#7314, step 5). The
design is the
[spec](superpowers/specs/2026-11-24-models-repository-lfs-design.md), "Switch". The command
is `python -m scadbuddy.library.models_switch`
([`backend/scadbuddy/library/models_switch.py`](../backend/scadbuddy/library/models_switch.py)).

**Nothing on this page was run on prod.** Each command block says so. The tests run the
command on a small repository with real git, git-lfs and Postgres
([`backend/tests/test_models_switch.py`](../backend/tests/test_models_switch.py)). The facts
about the cluster are read from eh-homelab/clusters on 2026-11-25, and not checked live.

## The rules

- **Nothing deletes `models-old`.** It stays until the owner says yes to its delete.
- **No API starts while `data/models` is missing or empty.** Its start sweep would delete
  each template's outputs and presets
  ([#7649](https://github.com/eh-homelab/ScadBuddy/issues/7649)). Argo CD must not be able
  to start one.
- The SQL runs only in the API's image, with the API's own credential (root `CLAUDE.md`,
  "Database access").
- When a step does not give what this page says, stop. Do not go on.

## What the command changes

| Step | What |
|---|---|
| dump | Each row that the next steps change, in a directory of its own |
| renames | `models` to `models-old`, then `models-next` to `models` |
| rows | `commit` and `render_key` of `order_items` and `order_item_revisions`; `commit` of `storefront_listings`, and not `updated_at`; `model_version` in `outputs.meta`; `scope_key` and `body` of the `template_version` rows of `analyzer_decisions`; `models_repo_head`; no row in `templates` and `template_copy_runs`; no `models` row in `git_remote_pushes` |

The rows change in one transaction. A second `run` with the same dump finishes one that
stopped, at any point. `rollback` with that dump undoes it.

An output's record is its `outputs` row alone since release B of #7257. The switch reads
and writes no file under `data/outputs/` (`models_switch.py`, module docstring). The dump
is of format 2 (`FORMAT`), with no `meta.json` files. The command refuses a dump of
format 1, made by an older build: finish or roll back such a switch with that build's
image (`_refuse_older_format`).

An order item whose `render_key` is not the key of its inputs at the old id keeps that key.
Its commit changes. `preflight` names each one.

## Before the day

1. The builder that keeps each version (PR #7658) is on prod. The switch refuses an output
   whose version the old repository has and the map does not.
2. The image that has `models_switch` is the one that runs. Use that same image for the pod
   below, so that the code and the schema agree.
3. Read the list of what must not run (next section), and choose a quiet hour.
4. Merge no deploy. The deploy pipeline merges `deploy(scadbuddy)` PRs in eh-homelab/clusters
   by itself, so one can land in the window. It does nothing while the automated sync is
   off, and it runs when the sync is on again: then the image is not the one of this day.

## 1. Argo CD: automated sync off

Two Applications put the Deployment back. `scadbuddy` (path `clusters/prod/scadbuddy`) has
`prune` and `selfHeal` on. Its own CR comes from `clusters-prod`, which has `selfHeal` on
too, so a live patch of `scadbuddy` alone is undone.

The way to do it: a PR to eh-homelab/clusters that removes `syncPolicy.automated` from
`argocd-apps/scadbuddy/application.yaml`. Merge it, and wait for `clusters-prod` to sync.
Check that it is off, and check again after five minutes:

```bash
# NOT RUN. Expected: no output.
kubectl -n argocd get application scadbuddy -o jsonpath='{.spec.syncPolicy.automated}'
# NOT RUN. Expected: no sync is in progress (Succeeded).
kubectl -n argocd get application scadbuddy -o jsonpath='{.status.operationState.phase}'
```

In an emergency, with no PR: take `automated` from `clusters-prod` first and from `scadbuddy`
second, and put both back after. That stops every app that `clusters-prod` owns from healing.

```bash
# NOT RUN. The emergency way only.
kubectl -n argocd patch application clusters-prod --type=json \
  -p '[{"op":"remove","path":"/spec/syncPolicy/automated"}]'
kubectl -n argocd patch application scadbuddy --type=json \
  -p '[{"op":"remove","path":"/spec/syncPolicy/automated"}]'
```

## 2. Let the work end

The API settles its own lost slices and jobs, so do this while it runs. In the Temporal UI
(`scadbuddy-temporal.internal.nullreference.io`, namespace `scadbuddy`), this query must find
nothing. The database does not tell about listing preparations: they have no table.

```text
ExecutionStatus = "Running" AND WorkflowType IN
  ("TemplatePipeline", "RenderPiece", "RenderPreview", "PrepareListing", "Operation",
   "MigrateInputs", "StoreMigration")
```

Then read what the database says. A `psql` in the Postgres pod is another role, so it does not
make the switch refuse:

```bash
# NOT RUN. Expected: 0 in each row.
kubectl -n bambuddy exec scadbuddy-db-1 -c postgres -- psql -U postgres -d scadbuddy -tAc "
  SELECT 'render_jobs', count(*) FROM render_jobs WHERE state IN ('pending', 'running')
  UNION ALL SELECT 'operations', count(*) FROM operations WHERE status = 'running'
  UNION ALL SELECT 'slices', count(*) FROM storefront_slice_prices WHERE status = 'running'
  UNION ALL SELECT 'backfills', count(*) FROM outputs
    WHERE backfill IS NOT NULL AND backfill->>'error' IS NULL"
```

An open flow run (`workflow_runs` `running` or `waiting`) does not stop the switch. A flow that
holds a commit of a template in its own state keeps the old id: end it first, or accept that
its call is refused.

Note these, for the checks at the end: `GET /api/v1/outputs/copy-status`,
`GET /api/v1/settings/git-remote`, one approved order item with a saved output, and one
published listing with its prices.

## 3. All API replicas at 0

```bash
# NOT RUN.
kubectl -n bambuddy scale deployment scadbuddy --replicas=0
# NOT RUN. Expected after the 60 s of grace: "No resources found".
kubectl -n bambuddy get pods -l app.kubernetes.io/name=scadbuddy
# NOT RUN. Expected: 0 in both.
kubectl -n bambuddy get deployment scadbuddy -o jsonpath='{.spec.replicas} {.status.replicas}'
# NOT RUN. Expected: no row. A pgAdmin that is open logs in as the same role: close it.
kubectl -n bambuddy exec scadbuddy-db-1 -c postgres -- psql -U postgres -d scadbuddy -c "
  SELECT pid, usename, application_name, client_addr FROM pg_stat_activity
  WHERE datname = 'scadbuddy' AND backend_type = 'client backend' AND usename <> 'postgres'"
```

The other Deployments (render, print, projects, storefront, the agent's) do not mount
`data/models` and hold no database secret, so they stay. Their calls fail and are tried again
until the API is back. `ScadBuddyApiReplicasUnavailable` fires: that is expected.

## 4. The pod

A pod of the API's image, with the API's credential and the share. It is not a change in
eh-homelab/clusters.

- It has no `app.kubernetes.io/name: scadbuddy` label: the Service, the PodDisruptionBudget
  and the NetworkPolicies select by it. So it has no access to Temporal, and it needs none.
- It has the API's database secret, and not its KEK nor its Bambuddy key: no step here opens
  a sealed value.
- The share is mounted at its `Scadbuddy` subPath only, never at its root (eh-homelab/clusters
  #1811).
- It has no deadline: a pod that ends in the middle leaves `data/models` away.

```bash
# NOT RUN. The image of the Deployment, for the manifest below.
IMAGE=$(kubectl -n bambuddy get deployment scadbuddy \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="scadbuddy")].image}')
echo "$IMAGE"
# NOT RUN.
kubectl -n bambuddy apply -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: scadbuddy-models-switch
  namespace: bambuddy
  labels:
    app.kubernetes.io/name: scadbuddy-models-switch
  annotations:
    backup.velero.io/backup-volumes-excludes: nas-share
spec:
  restartPolicy: Never
  nodeSelector:
    kubernetes.io/arch: amd64
  serviceAccountName: scadbuddy
  automountServiceAccountToken: false
  securityContext:
    runAsNonRoot: true
    runAsUser: 10001
    runAsGroup: 10001
    fsGroup: 10001
  containers:
    - name: switch
      image: $IMAGE
      command: ["sleep", "infinity"]
      env:
        - name: SCADBUDDY_DATABASE_URL
          valueFrom:
            secretKeyRef:
              name: scadbuddy-db-app
              key: uri
        # The settings ask for an address. Nothing here connects to it.
        - name: SCADBUDDY_TEMPORAL_ADDRESS
          value: scadbuddy-temporal-v2-frontend.bambuddy:7233
        - name: SCADBUDDY_TEMPORAL_NAMESPACE
          value: scadbuddy
      volumeMounts:
        - name: nas-share
          mountPath: /data
          subPath: Scadbuddy
      resources:
        requests:
          cpu: "1"
          memory: 1280Mi
        limits:
          memory: 4Gi
      securityContext:
        allowPrivilegeEscalation: false
        capabilities:
          drop: ["ALL"]
  volumes:
    - name: nas-share
      persistentVolumeClaim:
        claimName: nas-share
EOF
# NOT RUN.
kubectl -n bambuddy wait --for=condition=Ready pod/scadbuddy-models-switch --timeout=10m
```

## 5. The switch

Run each command in the pod, and keep all of its output:

```bash
# NOT RUN. The last build, now that nothing changes the old one, and its check.
kubectl -n bambuddy exec scadbuddy-models-switch -- python -m scadbuddy.library.models_next build
kubectl -n bambuddy exec scadbuddy-models-switch -- python -m scadbuddy.library.models_next check
# NOT RUN. It only reads.
kubectl -n bambuddy exec scadbuddy-models-switch -- python -m scadbuddy.library.models_switch preflight
```

`check` must say that there are no differences. `preflight` prints a count for each table, a
`note:` for each thing that it leaves, and last `ready: <old HEAD> to <new HEAD>, <n> ids`.
Its exit code is 0. A `refused:` line is a reason to stop: settle it, and run it again. Read
each note. Then:

```bash
# NOT RUN. The dump goes beside the repositories, in a directory that is not there yet.
kubectl -n bambuddy exec scadbuddy-models-switch -- \
  python -m scadbuddy.library.models_switch run --dump /data/models-switch-1
```

It must say these lines, in this order (`note:` lines come first):

```text
dumped <rows> rows to /data/models-switch-1
renamed models to models-old
renamed models-next to models
<rows> rows are changed
done: /data/models is the new repository, at <new HEAD>; the old one is /data/models-old, and it stays
```

When it stops, it says why and which of the three directories are there. **Start no API.**
Give the same `run` again. When it cannot go on, use `rollback` (below). When the connection of
`kubectl exec` was lost, the command may still run: look with
`kubectl -n bambuddy exec scadbuddy-models-switch -- ps aux`, and wait. A second `run` at the
same time is refused by a lock.

## 6. One replica, and the checks

```bash
# NOT RUN. The new repository is in place before this, and not after it.
kubectl -n bambuddy exec scadbuddy-models-switch -- ls /data/models | head
kubectl -n bambuddy scale deployment scadbuddy --replicas=1
kubectl -n bambuddy rollout status deployment scadbuddy --timeout=10m
```

The one replica copies every template to rows before it serves (`run` emptied
`template_copy_runs`). If that takes longer than the startupProbe's budget, the pod restarts
and the next start continues the copy (each batch keeps its rows); raise the startupProbe's
`failureThreshold` for this step when the pod restarts.

| Check | Expected |
|---|---|
| `GET /api/v1/model-records/copy-status` | `finished_at` is set and after the start; `incomplete` is false; `added` is the number of templates; `unreadable` is empty |
| `GET /api/v1/outputs/copy-status` | The same as in step 2: `cut_over_at` is set. No copy runs since release B of #7257, so nothing in it moves |
| The model list and one mesh model | Each template is there. The mesh model opens, and it renders |
| The approved item | Its saved output is still its output. Its quote stands: the item page asks for no new one |
| The listing | The page shows it with the prices that it had |
| `GET /api/v1/settings/git-remote` | `models`: after at most one sweep (300 s), `error` is null and `behind` is 0. The remote had no `models` branch on 2026-11-24, so nothing is in its way; a branch that is there with the old history would refuse it, and nothing forces it |
| The API's log | No error that names a commit or a template |

A saved output of a version that is in no repository opens with a message, and not a 500.

Make one save of a template, and see it in its history. Do this last: it is the point after
which a rollback loses something.

## 7. All replicas, and Argo CD on

```bash
# NOT RUN.
kubectl -n bambuddy scale deployment scadbuddy --replicas=3
kubectl -n bambuddy rollout status deployment scadbuddy --timeout=15m
kubectl -n bambuddy delete pod scadbuddy-models-switch
```

Revert the PR of step 1, merge it, and wait for `clusters-prod` to sync. The app must be
`Synced` and `Healthy`, with `automated` there again:

```bash
# NOT RUN. Expected: {"prune":true,"selfHeal":true}, then Synced Healthy.
kubectl -n argocd get application scadbuddy -o jsonpath='{.spec.syncPolicy.automated}{"\n"}'
kubectl -n argocd get application scadbuddy \
  -o jsonpath='{.status.sync.status} {.status.health.status}{"\n"}'
```

Keep `/data/models-old` and `/data/models-switch-1`.

## Rollback

It is for the first minutes only. What was saved after the switch names new ids:

- A commit that was made on the new repository stays in `models-next`, and the old one does
  not have it.
- An order, a listing or an output that was made after the switch names an id that the old
  repository does not have. The command names each one, and leaves it.
- A row that the switch changed, and that something else changed after it,
  stops the rollback before it changes anything: it holds neither the old value nor the new
  one. That is how the limit shows.

The automated sync is off (step 1), and all replicas are at 0 (step 3). Then, in the pod of
step 4:

```bash
# NOT RUN.
kubectl -n bambuddy scale deployment scadbuddy --replicas=0
kubectl -n bambuddy get pods -l app.kubernetes.io/name=scadbuddy
kubectl -n bambuddy exec scadbuddy-models-switch -- \
  python -m scadbuddy.library.models_switch rollback --dump /data/models-switch-1
```

Its last line starts with `done: /data/models is the old repository`. It renames back, puts
back the dumped columns, sets
`models_repo_head`, and empties `templates` and `template_copy_runs`. It deletes no repository,
no output and no dump. A `run` after it needs a dump directory of its own when a row changed
in between. Then start one replica and do the checks of step 6: the HEAD is the old one, and
the off-box push fails as it did before.

A rollback that stopped is finished by the same command.

## After

- `models-old` and the dump stay until the owner says yes to their delete. That is a decision
  of its own, after a clone with `git lfs pull` gave every byte.
- `template_media.source_commit` still holds the old id of an order revision's commit. It is
  not in the list of what the switch changes.
- Storefront renders have names that come from `render_key`. A variant that is asked for again
  may be rendered and sliced once more.
