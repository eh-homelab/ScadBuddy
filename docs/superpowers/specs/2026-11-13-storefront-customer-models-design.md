# Storefront: print the customer's own models, and buy things for their project

Status: **approved by the owner on 2026-11-13 (#6133), with changes, which this revision
applies.** It amends the storefront spec (`2026-11-02-storefront-design.md`, "the storefront
spec"). It builds on PR #6128 (the MakerWorld import through ScadBuddy's API, #6119) once
that PR merges.

The owner's decisions:

1. Customers can give MakerWorld links or upload STL and 3MF files to have them printed.
2. "Allow all links, but process them based on the link type; an unknown handler = left for
   backoffice to process manually."
3. "Users can specify a link is to be purchased for their project (pass along costs)."
4. On approval (2026-11-13):
   - a request takes several files and several links;
   - files go straight to the store through presigned PUTs, into a **staging area only**;
   - an imported file goes into ScadBuddy's own library and its staging object is deleted;
   - imports go through a flow;
   - a MakerWorld link is imported by #6128's `makerworld_import`, not by a fetcher of
     ScadBuddy's own; when that import is refused or needs sign-in, the link falls back to
     `manual`;
   - purchases take a configurable handling fee;
   - the intake worker is its own Deployment;
   - own-model items default to `together` approval;
   - the `purchase_pending` gate leaves the submit's deposit request alone;
   - up to 5 purchase links per item, on design requests too.

Every other choice is marked **(call)**: the author made it, and the owner may overturn it.
Licensing is the owner's call and this spec does not rule on it.

Every fact about the code was read at `origin/main` `f7a7f39d2` on 2026-11-13, and at PR
#6128's head that day for its code. A measured fact says where and how it was measured.

## 1. Why

Today a storefront customer can buy a listed product or send a design request (storefront
spec §5.4). The design request takes photos, videos and PDFs only
(`frontend/src/storefront/components/RequestForm.tsx:113`;
`backend/scadbuddy/storefront/wire.py` `REQUEST_KINDS`). Many customers already have models:
files they made, or pages on MakerWorld, Printables or Thingiverse. They often need parts
that the shop does not print, such as magnets, screws or a paid model file. Today they send
all of this by message.

## 2. Goals, non-goals, anti-goals

Goals:

- A "Print my own model" entry. The customer gives model files and links, a quantity,
  colour preferences, a note, and links to things to buy.
- Each file and each importable link ends as a file in ScadBuddy's own library. The order
  quotes it as a `library_file` item (#5544), through the existing slice quote, accept and
  pay flow.
- The shop records what it paid for each thing it buys. The customer sees that amount, and a
  handling fee if the shop sets one, before they pay.

Non-goals: automatic purchase; an instant price for an uploaded model; a fetcher of
ScadBuddy's own for any site (MakerWorld goes through Bambuddy, #6128).

Anti-goals (never):

- The CRUD service parses, renders or slices a customer's file in the request path (#5295).
- The CRUD service or the storefront fetches a customer's link.
- A customer's link becomes a preview, an unfurl or a prefetch anywhere.
- An order item points at a staging key once its file is imported.
- A file goes through JSON or text as base64.

## 3. Zones (#5295)

Only the CRUD service (today's API process) has SQL access. No untrusted input is parsed or
run in a pod that holds a database secret (root `CLAUDE.md`, "Database access";
`docs/security/trust-boundaries.md`).

| Step | Process | DB secret | What it does with the customer's bytes |
| --- | --- | --- | --- |
| Presign | storefront edge, then CRUD | edge none; CRUD yes | text fields only: names, sizes, types. No bytes. |
| Upload | the browser, straight to the bucket | n/a | the bytes go to a staging key; no ScadBuddy process sees them |
| Claim | CRUD | yes | a server-side `CopyObject` of each staging object to its frozen key, and a `HEAD` of the copy for its size and ETag. No bytes read. |
| Check and preview | intake worker (new, §7) | none | the full check, the plates, the preview, the SHA-256 |
| Import | CRUD's `library` queue, through a flow (§8) | yes | copies the bytes to the native library; reads no structure (the worker's plates are passed in, §8.3) |
| MakerWorld link | Bambuddy, through #6128's API | n/a | Bambuddy downloads; the native copy is #6128's |

## 4. The customer's flow

### 4.1 The form

The storefront gets a "Print my own model" entry at `/print-my-model` **(call)**:

1. **Your models**: any mix of files (`.stl`, `.3mf`) and links (any `http`/`https` link,
   §9). At most 20 files and 10 links per request (§6.1).
2. **How many**: one quantity for the request (1 to the item quantity cap); the note can say
   "two of the base, one of the lid".
3. **Colours**: "Let the shop choose" (the default), or up to 8 of the price book's offered
   filaments.
4. **Anything else**: a plain-text note, 0 to 5000 characters (`MAX_REQUEST_TEXT`).
5. **Things to buy for this project**: 0 to 5 purchase links (the owner's cap), each with a
   note of up to 500 characters, such as "the 6 mm magnets, 20 of them" (§10). The
   design-request form gets the same list (the owner's decision).

Turnstile shows under the form.

### 4.2 Sending

1. The page asks the edge for upload slots (`POST /api/store/v1/uploads`, §6.2) with each
   file's name, size and type. That one call takes the upload allowance and Turnstile.
2. The browser `PUT`s each file to its presigned URL, straight to the bucket, with a
   progress bar per file. A failed `PUT` can be tried again on the same URL until it expires.
3. "Add to my order" sends the request (`POST /api/store/v1/draft/models`, §6.3), naming the
   upload ids, the links, the quantity, the colours and the note.

### 4.3 The cart

The item reads "Your models" with one line per file and link. A file shows "Checking", then
its preview and "Ready", or a plain reason ("This file is not a 3MF: it is not a readable
zip"). A refused file blocks submit until the customer removes it (`model_file_refused`,
409) **(call)**. A file still being checked does not block submit. Each link and purchase
link is shown back as escaped text only, never an anchor (the owner's ruling).

A staged file of a cart not submitted within 72 hours of its claim
(`model_requests.STAGING_TTL`) is deleted by its watcher (§6.5), and the line reads "This
file expired; upload it again" **(call)**: an abandoned cart must not keep up to 1 GiB in
the bucket for the 30-day cart TTL. A new claim or other activity does not move that
deadline.

### 4.4 After submit

Submit (storefront spec §5.5) signals each staged file's watcher (§6.5): from then on the
file has no deadline, and its watcher runs the import (§8) and waits until the file is
imported or a person declines it. The built-in `customer-model-intake` flow (§8) keeps the
orchestration. It imports each ready file into the
native library and deletes its staging object, and it imports each MakerWorld link through
#6128. Each import makes a `library_file` item on the order (§5.4). The shop slices a quote
per item and the customer accepts it on the order page (a `library_file` item has no
revisions, `lifecycle.py` `NO_REVISIONS`, so the quote's accept is the one accept, which is
the `together` default the owner chose). A link the flow cannot import stays `manual` for the
shop (§9.3).

## 5. The data

All schema changes are new migration files (root `CLAUDE.md`, "Migrations").

### 5.1 The request item

An own-model request is an `ItemKind` `request` item (storefront spec §4.2) with a row in
the side table `order_item_model_requests` (`item_id`, `colour_preferences`); a design request
has none. As built (PR 1) this is a side table, not columns on `order_items`, so no
`OrderItem` read changes. `colour_preferences` (jsonb) records what the customer asked:
`{"shop_choice": true}` or `{"filaments": [<price book id>, …]}`, at most 8.

### 5.2 Staging uploads: `storefront_uploads`

One row per presigned slot: `id` (uuid4 hex), `visitor` (the visitor id's sha256), `object_key`
(`<prefix>/staging/<id>`), `filename`, `size` (declared, signed into the URL), `media_type`
(`model/stl` or `model/3mf`), `created_at`, `expires_at` (the URL's expiry), `claimed_at`.
The row owns its staging object: its watcher (§6.5) deletes the object with the row.

### 5.3 The request's sources: `order_item_sources`

One row per file or link of a request item:

| Column | Meaning |
| --- | --- |
| `id`, `order_id`, `item_id` | the request item |
| `kind` | `file` or `link` |
| `purpose` | `print` (files always) or `purchase` (links only, §10) |
| `upload_id` | a file's `storefront_uploads` row, until imported (then null) |
| `filename`, `size`, `media_type` | a file's |
| `url`, `link_type`, `canonical` | a link's text, its handler (`makerworld`, `printables`, `thingiverse`, `manual`) and canonical id; `purchase` is always `manual` |
| `note` | the customer's note on a link, at most 500 characters |
| `check_status`, `check_reason`, `facts`, `sha256` | a file's check (§7): `checking`, `ready`, `refused`, `failed` |
| `preview_key` | the preview PNG's blob, held by `blob_refs (order_item_source, <id>)` |
| `status` | `print`: `open`, `imported`, `manual`, `declined`. `purchase`: `pending`, `recorded`, `not_needed` |
| `status_reason` | why it is `manual` (the import's refusal in plain words) or `declined` |
| `library_file_id` | the native library file once imported |
| `result_item_id` | the `library_file` item the import made |
| `cost_entry_id`, `charge_id` | a recorded purchase's (§10.2), each UNIQUE |
| `shop_note` | the shop's note, shown to the customer |

A CHECK keeps an imported file's `upload_id` null: no order row names a staging key after
import (the owner's rule).

### 5.4 The items the imports make

Each import adds one `library_file` item (#5544: `library_file_id`, `library_filename`,
`library_source` `native`) to the order with the request's quantity, the file's name as its
title, and `order_items.source_item_id` naming the request item (new, nullable FK). When
every `print` source of a request item is `imported` or `declined`, the request item's
status becomes `cancelled` with the audit reason `replaced_by_items`; the customer's view
reads it as "Replaced by the items below" **(call)**. A request item with a `manual` source
stays open for the shop.

### 5.5 Charges

`ChargeKind` gains `purchase` and `handling`. `order_charges.purchase_link_id` (nullable FK,
UNIQUE): a CHECK sets it exactly for `purchase`. `handling` is at most one row per order,
written only by the lifecycle (§10.3).

## 6. Uploads straight to the store

### 6.1 Caps

- **Per file: 256 MiB** **(call)**. The largest of the 16 real MakerWorld 3MFs in
  `test-fixtures/makerworld/` is 98,325,251 bytes (`ls -la` in the main checkout, 2026-11-13),
  so 256 MiB is 2.7 times the largest real file seen. It bounds the intake worker's disk and
  time per check, and it is far under S3's 5 GB limit for one `PUT`
  ([S3 quick facts](https://docs.aws.amazon.com/AmazonS3/latest/userguide/qfacts.html), cited
  in `store/s3.py`). Cloudflare's 100 MB cap does not apply: the bytes skip the proxied host.
- **Per request: 20 files, 10 print links, 1 GiB declared in all** **(call)**. A kit of parts
  often comes as one STL per part, and 20 covers a typical kit. Each file costs one worker
  check and one import, so the count bounds the work one request starts. The 1 GiB total
  bounds what one allowance can stage: 6 requests an hour per client (`REQUEST_UPLOADS`,
  `storefront/ratelimit.py`) is at most 6 GiB an hour, and each watcher removes what is never
  claimed (§6.5).
- **Purchase links**: 5 per item (the owner's cap).

### 6.2 The presign step

Edge route `POST /api/store/v1/uploads`, JSON
`{files: [{filename, size, media_type}]}`, at most 20. Before anything else, as
`add_request` does: 404 `requests_disabled`; 428 `idempotency_key_required`; the
`UploadAllowance` (the design requests' allowance, shared, so the two forms do not add up);
Turnstile, a new action `model`, after the allowance. No `UploadGate`: no body bytes pass.

CRUD route `POST /api/v1/crud/storefront/uploads` (area `models`, service credential): needs
the S3 store (`listings.py` `public_content`; any other backend is 503 `storefront_needs_s3`,
as public media is), checks each entry (a name ending `.stl` or `.3mf` that agrees with
`media_type`, `1 <= size <= 256 MiB`, the per-request total), writes the rows, starts one watcher per row (§6.5), and only then answers per file
`{id, url, headers: {"Content-Type": …, "x-amz-acl": "private"}, expires_at}`. A watcher
that cannot be started is 503 `uploads_unavailable` with no URL, and the new rows are
dropped: no file can land in the staging area without a watcher.

Each URL is a presigned `PUT` (`S3ContentBackend.presign_put`, new) for exactly one key,
`<prefix>/staging/<id>`, with `ContentType` and `ContentLength` signed in, valid 15 minutes
**(call)**. botocore signs both headers: a URL made with aiobotocore for a DO Spaces endpoint
carried `X-Amz-SignedHeaders=content-length;content-type;host` (generated offline in
`backend/`'s environment, 2026-11-13). So the store refuses a body of any other length or
type. As built (PR 1) the canned ACL `private` is signed in too (`ACL`, so
`X-Amz-SignedHeaders=content-length;content-type;host;x-amz-acl`): a PUT that asks for
another ACL, `public-read` among them, does not match the signature. The bucket itself names
no ACL and no policy (`infra/do-spaces/scadbuddy-store/main.tf`), so it is private, and every
staging object is private; a staging object is read only through a presigned GET. S3 checks
the expiry when the request starts, so a slow upload that started in time
completes ([AWS, presigned URLs](https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html),
"Expiration time").

### 6.3 The claim

Edge route `POST /api/store/v1/draft/models`, JSON
`{uploads: [ids], links: [{url, purpose, note}], quantity, colours, note}`. It is 404
`requests_disabled` while "Ask for a design" is off. A request that names an upload is not
asked Turnstile again: the allowance and the check were spent at that upload's presign, and
each slot is claimed once. A request that names none (links only) takes them here, as the
presign does: the design requests' `UploadAllowance` (429 `request_limited`), then Turnstile
action `model`, a passed check spending one (found in review of #6147: a links-only request
skipped every gate). CRUD route
`POST /api/v1/crud/storefront/draft/models` (area `models`): each upload must be the
visitor's, unclaimed and not cleaned up. Each is then frozen: a server-side `CopyObject` of
`<prefix>/staging/<id>` to `<prefix>/staging/<id>.frozen`, a key no presigned URL names, and a
`HEAD` of the copy, which must answer exactly the declared size (else 422 `upload_missing` or
`upload_size`, naming the file; a copy of the wrong size is deleted). The customer's PUT URL
stays valid up to 15 minutes after the claim, so nothing after the claim reads the original:
the check and the import read only the frozen copy. Then one transaction adds the request
item, its sources and a `checking` state per file, claims the uploads and keeps each copy's
ETag (`storefront_uploads.frozen_etag`). After commit it deletes the writable originals (best
effort; the watcher deletes every staging key of the file when it ends, §6.5), signals each
file's watcher `claimed` (§6.5) and starts one `CustomerFileCheck` per file (§7). A retry with the same idempotency
key answers its first reply, as `add_request` does. The CRUD service never reads the bytes.

### 6.4 The bucket's CORS rule

Yes, the bucket needs one. The page is on `https://michaels-workshop.nullreference.io`; the
`PUT` goes to the bucket's host, cross-origin, with a `Content-Type` that is not a CORS
"simple" type, so the browser sends a preflight `OPTIONS` first, and the bucket must answer
it. DigitalOcean Spaces takes CORS rules per bucket: origin, methods, allowed headers, max
age ([DigitalOcean, configure CORS](https://docs.digitalocean.com/products/spaces/how-to/configure-cors/)).
The rule, added to the tofu module `infra/do-spaces/scadbuddy-store` in eh-homelab/clusters
(the prod bucket `eh-scadbuddy-store`):

- `AllowedOrigin`: `https://michaels-workshop.nullreference.io` only;
- `AllowedMethod`: `PUT` only;
- `AllowedHeader`: `content-type` and `x-amz-acl`;
- `ExposeHeader`: `ETag`;
- `MaxAgeSeconds`: 3600.

No `GET`: presigned downloads are plain navigations or `<img>` loads, which need no CORS.

The edge's page CSP (`storefront/static.py`, `connect-src 'self'`) must allow the bucket's
origin for the `PUT`. The edge reads it from the CRUD service with the upload caps
(`GET /api/v1/crud/storefront/uploads/config`, cached as the site key is, `SiteKey`), and adds
that origin to `connect-src` once read **(call)**.

### 6.5 Each file's watcher, and the backstop

The owner's rule (2026-11-13): "start a workflow to watch the file that ends when work is
done, or if the file times out." Each upload has one watcher, the workflow `StagedFile`
(`workflows/staged_file.py`) on the `library` queue, id `staged-file:<upload id>`. As built
(PR 1, #6147):

- **Start.** The presign route starts the watchers before it answers any URL (§6.2), with
  `USE_EXISTING` and `ALLOW_DUPLICATE`: a presign sent again (its replay re-signs the same
  slots) joins the running watcher, and a watcher that ended is started again. This is the
  owner's `REJECT_DUPLICATE` intent, one watcher per file, in a form a replay can pass.
- **One step.** Each turn runs one activity, `staged_file_step`
  (`orders/storefront/staged_files.py` `step`). It reads the file's state from Postgres under
  the order's lock (the lock a claim, a line's removal and a submit take) and, when the file
  is due, cleans it up in the same transaction. The database is the truth: a signal only
  wakes the watcher early, so a lost signal costs time, never a file.
- **Signals.** `claimed` (the claim's route), `unclaimed` (a line's removal), `submitted`
  (the submit), and, for later PRs, `checked` (the intake worker), `imported`, `declined`,
  `closed` and `retry`. Each is sent after its commit and may be lost.
- **Deadlines** (the TTLs below, measured from the database's timestamps): an upload never
  claimed is cleaned up `UNCLAIMED_GRACE` after its URL's expiry; a claimed upload of a cart
  not submitted, `STAGING_TTL` after its claim; a claimed upload whose line is gone, at once.
  A submitted order's file has **no deadline**: the watcher reads it again every hour and
  ends only when the file is imported (§8.3) or a person declines it.
- **Clean-up.** In one transaction: a cart's line reads `EXPIRED` (`check_status`
  `refused`), and the row is deleted. After the commit, the staging object and its preview
  are deleted; a missing object is no error. A step that fails is retried with backoff and
  no limit, so the watcher does not end before its clean-up succeeded; a retry after a crash
  between the commit and the deletes finds no row and deletes the objects again. The order's
  page is told (`OrderChanged`).
- **History.** A watcher holds a few signals and one step per deadline; one that waits long
  (a submitted file) continues as new past 2000 events.

| TTL | Value | What it bounds |
| --- | --- | --- |
| `UPLOAD_URL_TTL` | 15 minutes | how long a presigned `PUT` may start |
| `UNCLAIMED_GRACE` | 1 hour | an upload never claimed, after its URL's expiry |
| `STAGING_TTL` | 72 hours | a claimed upload of a cart not submitted, after its claim |
| `ORPHAN_AGE` | 1 day | the backstop's threshold |
| (submitted) | none | a submitted order's file waits for its import or a decline |

**The backstop.** The storefront drafts sweep (`housekeeping_sweep_storefront_drafts`, both
Schedules) runs `staged_files.sweep_staged` after the carts; it owns no file's life. It
deletes an object under `staging/` that no row names, older than `ORPHAN_AGE`, and it starts
the watcher again of a row older than `ORPHAN_AGE` whose watcher is not running (Temporal's
describe answers not found or closed), and the check again (§7) of a file source still
`checking` past `CHECK_STALE` (1 hour) whose check is not running; that watcher then applies the rules above, so a
submitted file is never deleted by the backstop. One pass checks at most 1000 rows
(`SWEEP_PASS_BATCHES` batches of `SWEEP_BATCH`, one describe each, so the sweep keeps
inside its 5-minute timeout); the next pass goes on from where it stopped, and past the
newest row it starts again from the oldest. Each repair is logged and counted
(`scadbuddy_staged_files_repaired`, by kind); it should find nothing, and a count is a bug
to look into. `S3ContentBackend.sweep_unrecorded` skips keys outside `<kind>/<sha256>` (its
`_OBJECT` pattern), so it never touches staging.

## 7. The intake worker (no database secret)

A new Temporal worker, `python -m scadbuddy.intake_worker`, in the app image, in its own
Deployment `scadbuddy-intake` (the owner's decision), on task queue `customer-files`. It
holds no database secret and no store credential. It gets a service credential through
`exchange_source` (`auth/service_auth.py`), scoped to the CRUD area `intake` only. The pod
has a read-only root file system and a memory limit; each parse runs in a child process with
an address-space limit and a timeout, killed with its process group, as
`workflows/template_process.py` runs template Python. Its egress policy allows the CRUD
service, Temporal and the bucket host only (eh-homelab/clusters#3182 owns the policies; the
clusters PR adds this pod's).

`CustomerFileCheck(source_id)` (`intake_worker/workflow.py`; the CRUD service starts it after
a claim, `orders/storefront/intake.py` `start_checks`, workflow id
`customer-file-check:<source id>`; the backstop starts it again for a source still
`checking` an hour after its claim whose check is not running, §6.5). Its activity
(`intake_worker/activities.py`), tried three times:

1. asks the CRUD service for a presigned `GET` of the staging object
   (`POST /api/v1/internal/crud/intake/sources/{id}/read`, area `sources`, 5 minutes; 409
   `not_checking` for a source that no longer waits, which ends the run) and downloads it
   to its disk, counting bytes (at most the declared size; any other size is refused). The
   GET is of the frozen copy (§6.3) and signs `If-Match` of its ETag, which the worker
   sends, so S3 answers 412 for any other object at that key and the attempt fails. The
   import (§8, PR 5) copies the same key with `CopySourceIfMatch` and checks the SHA-256
   the worker reported;
2. checks it by content, never by name or declared type:
   - a 3MF starts `PK\x03\x04`; a binary STL is `84 + 50 × n` bytes for the uint32 `n` at
     offset 80; otherwise printable ASCII starting `solid` is an ASCII STL; the name's
     extension must agree;
   - the 3MF's zip, central directory first: at most 512 entries; each entry declares at
     most 1 GiB inflated and all at most 2 GiB; an entry over 1 MiB declares at most 200
     times its compressed size; methods stored and deflate only; no encrypted entry; no name
     that is absolute or holds `..`, a backslash or a NUL; `[Content_Types].xml` and a root
     model present; then the read itself inflates through a counting reader that stops at
     each declared size (§7.1);
   - the model read under the read budget (`render/read_budget.py`) and the mesh caps;
3. writes the facts (format, each plate's index, triangles, size in mm and colours, the
   triangles in all; at most 64 plates) and the SHA-256, and uploads a preview PNG of plate 1
   (`render/thumbnail.py` `render_view`, the iso view) through a presigned `PUT` of exactly
   its size the CRUD service mints for `<prefix>/staging/<id>.png`
   (`POST …/intake/sources/{id}/preview`). The plate list the native library keeps is the
   import's (§8.3, PR 5);
4. posts the result: `PUT /api/v1/internal/crud/intake/sources/{id}/check` (an
   `Idempotency-Key` that is the same on each retry). The CRUD service checks the result
   with Pydantic, reads the preview as opaque bytes (at most 4 MiB; none is 409
   `preview_missing`), stores it in the content store as `customer-preview-<sha256>`, the
   SHA-256 of the preview bytes it read, never a hash the worker reports (the file's SHA-256
   is kept as the blob's metadata only), so no verdict can point a source at another file's
   preview; it is held by the source (`blob_refs` holder `order_item_source`, dropped by a trigger with the source),
   deletes the staging preview, and signals the watcher `checked`. A refusal writes
   `refused` and the reason; a check whose attempts all fail (a timeout, a crash) writes
   `failed`, which the back office can run again (PR 4). A verdict for a source that
   stopped waiting meanwhile records nothing (`recorded` false).

The worker (`intake_worker/worker.py`) refuses to start with `SCADBUDDY_DATABASE_URL`,
`PGPASSWORD`, `PGPASSFILE` or `SCADBUDDY_SECRET_KEY_FILE` set, and imports no database code;
only its child process (`intake_worker/child.py`) reads the file, under its own
`RLIMIT_AS` (4 GiB) and a 5-minute timeout, with none of the worker's environment. The
worker is not versioned (no worker deployment): a change to the workflow's commands goes
behind `workflow.patched`. It serves `/healthz` on 9090. Its child gets no inherited file
descriptor but its three standard streams, all `/dev/null` (`close_fds`), so it cannot
reach the worker's sockets.

**The child's sandbox.** Three layers keep a parser exploit away from the worker's
credential (`intake_worker/hardening.py`, `runner.py`, `sandbox.py`):

- The worker makes itself non-dumpable at start (`prctl(PR_SET_DUMPABLE, 0)`), before it
  reads its proof, so no process of its user can `ptrace` it or read its
  `/proc/<pid>/mem` or `environ`
  ([prctl(2)](https://man7.org/linux/man-pages/man2/prctl.2.html)); the child does the same.
- The child calls `unshare(CLONE_NEWUSER | CLONE_NEWNET)` before it reads the file: a new
  network namespace has only a loopback device that is down, so the parse reaches no socket
  ([network_namespaces(7)](https://man7.org/linux/man-pages/man7/network_namespaces.7.html)).
  A container runtime's default seccomp profile may refuse it; the check then goes on, and
  the worker logs the refusal once (the verdict's `isolation`). Measured on WSL2
  (Linux 6.6.87.2) on 2026-11-14 by `tests/intake_worker/test_runner.py`
  `test_an_isolated_child_reaches_no_socket`: allowed, and a connect to a host loopback
  listener fails.
- The deployment runs the child in a second container of the pod, `sandbox` (`python -m
  scadbuddy.intake_worker.sandbox`): another `runAsUser`, no ServiceAccount token, no proof
  volume, `readOnlyRootFilesystem`, sharing with the worker only an `emptyDir` (the spool)
  through which the worker hands it the file and gets the verdict and the preview back
  (`SCADBUDDY_INTAKE_SANDBOX=true` on the worker; the protocol is `runner.py`'s docstring).
  The variable has no default: the worker refuses to start without it, so a missing value
  never falls back to the weaker mode; `unsafe-local` (development only) starts the child
  in the worker's own container.
  The sandbox refuses to start with a database secret, the KEK or a proof. The worker reads
  what comes back as untrusted: the verdict capped at 1 MiB and validated, the preview capped
  at 4 MiB. With `unsafe-local` (development, tests) the worker starts the child itself. The
  two containers share the pod's network namespace, so where the runtime refuses the child's
  own namespace, the pod's NetworkPolicy (DNS, the CRUD port, Temporal and the bucket host
  only) bounds what an exploited parse can reach; it holds no credential for the CRUD port.

**Serving the preview (PR 4).** The preview is bytes the untrusted check made. ScadBuddy
never decodes, resizes or re-encodes it, and serves it only as `Content-Type: image/png`,
`Content-Disposition: inline` and `X-Content-Type-Options: nosniff`, whatever the bytes
are. A presigned `GET` can fix the first two (`ResponseContentType`,
`ResponseContentDisposition`) but cannot add `nosniff`, because S3 overrides only six
response headers ([GetObject](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObject.html)).
So the back office and the edge stream it through a route of their own that sets all
three, never a presigned URL.

### 7.1 The zip-bomb limits, measured

`zipfile.ZipFile(...).infolist()` on the six largest fixtures (`python3 -I`, scratchpad
script, 2026-11-13; central directories only):

| Fixture | Bytes | Entries | Declared inflated | Largest entry | Highest ratio |
| --- | --- | --- | --- | --- | --- |
| 381-olive-oil-bottle-passthrough | 98,325,251 | 44 | 492,337,262 | 241,772,118 | 11.1 |
| 2464-ghost-campfire-light | 71,803,767 | 72 | 427,992,648 | 51,237,776 | 12.0 |
| 2602-tiny-flexi-frog… | 54,747,288 | 31 | 326,059,993 | 238,535,310 | 6.7 |
| 2178-find-100-mini-frogs… | 33,698,429 | 19 | 157,379,992 | 155,578,145 | 40.6 |
| 448-articulated-pumpkin | 10,287,542 | 23 | 64,097,076 | 63,012,386 | 6.8 |
| 1943-mini-articulated-ankylosaurs… | 6,555,948 | 30 | 32,126,907 | 30,918,539 | 6.8 |

Every entry used deflate. The limits leave four times or more on each axis; a zip bomb's
ratio is in the hundreds to millions. The library's `// file` caps (`library/meshes.py`: 64
entries, 64 MiB an entry, 128 MiB in all) would refuse four of these six, so the customer
check has its own limits and leaves those alone.

## 8. Imports go through a flow

### 8.1 The trigger and the flow

The import does not hang on the event bus (the owner's question, 2026-11-13: "How do we
have a strong guarantee that the workflow triggers to import it into regular storage?").
`flow_triggers/events.py` fires nothing for an event lost while the bus is disconnected, and
its catch-up reads status changes only; a submit is none (the order stays `draft`, storefront
spec §5.5). So each staged file's watcher (§6.5) owns the import: once its step reads the
file as submitted, it runs the import itself, as a child workflow with a deterministic id
(`staged-file-import:<upload id>`) that runs the flow's import step for that file, retried
with backoff and no limit. A watcher whose import has not finished one hour after the submit
marks the source "Import stuck" (`status_reason`, shown on the order page and the orders
list, with "Retry import", which sends the watcher `retry`), logs it and counts it
(`scadbuddy_staged_files_stuck`), and keeps waiting. A paused intake trigger or a revoked
grant shows the same way. The watcher ends only when the file is imported or declined.

A new built-in flow `customer-model-intake` (`orders/builtin_intake_flow.py`), registered as
`order-fulfilment` is (`orders/builtin_flow.py`: `register_new`, the upgrade rules, a trigger
the shop makes and a standing grant the shop approves). For each own-model request item of
the order:

- each `print` file: wait (bounded, as `order-fulfilment` polls `order_get`) for its check;
  when `ready`, call `import_staged_model(source_id)`; when `refused`, leave it;
- each `makerworld` link: call #6128's `import_makerworld_model` with the link's model id,
  following a `running` answer with `get_operation` as #6128's body shows; then
  `order_item_source_resolve(source_id, library_file_id)`. A refusal (#6128's 409, 422 or
  502, or a sign-in-needed answer) calls `order_item_source_manual(source_id, reason)`;
- every other link type stays `manual` (registry-typed for the shop, §9).

The run's grant names `import_staged_model` and `order_item_source_resolve`,
`order_item_source_manual` (write), `import_makerworld_model` (outward, #6128) and
`get_operation`, `order_get` (read).

The shop's manual path is the same calls from the order page: "Import again", "Use library
file" (after the shop downloads a manual link's file and puts it in the library), and
"Decline".

### 8.2 Extend #6128, or a sibling step

**A sibling.** #6128's `makerworld_import` operation and its flow step take a MakerWorld
model id, call Bambuddy's import, and copy the Bambuddy file into the native library
(`print_library/copy.py` `copy_file_to_native`). A staging object has no Bambuddy file and no
MakerWorld id, so neither the operation nor the `makerworld-import` flow's `import_one` can
take it without a second source type in each. The sibling reuses #6128's pieces instead of
copying them:

- a new operation `staged_model_import` on the `library` queue
  (`orders/storefront/intake_operations.py`), route `POST /api/v1/orders/{id}/sources/{source}/import`,
  agent tool `import_staged_model` (write: it files a file in the shop's own library and
  tells no one);
- in `print_library/copy.py`, a sibling of #6128's `copy_file_to_native`,
  `copy_staged_to_native`, through the same `_NativeTarget`, `TargetLocks` and SHA-256
  dedupe;
- the flow is a sibling of `makerworld-import` (`customer-model-intake`), and calls #6128's
  tool for links.

This PR waits for #6128 to merge and does not rebase onto its branch.

### 8.3 What the import does

`staged_model_import(source_id)`, under the order's lock for the writes:

1. refuses a source that is not `ready` (409 `source_not_ready`), and a `library_backend`
   other than `native` (409 `native_library_required`; the file stays staged, and the shop
   downloads it by hand);
2. streams the staging object into a spool and checks its SHA-256 against the worker's
   (a mismatch fails: the object changed);
3. `copy_staged_to_native`: skipped for a live native file of the same SHA-256, else
   `NativeLibrary.upload` into the folder `Customer models/<order number>` **(call)**, with
   the worker's plates passed in (`NativeLibrary.stage` gains `plates=` and `has_cover=`), so
   the API does not open the customer's zip (today `stage` reads every uploaded 3MF's plates
   in the API process, `print_library/native.py` `_read_upload`);
4. in one transaction: the source gets `library_file_id`, `status` `imported`, `upload_id`
   null; the `library_file` item is added (§5.4); the upload row is deleted;
5. after commit, signals the file's watcher `imported`; its next step finds no row and
   deletes the staging object (§6.5).

Known residue: the native library's cover and plate image routes still read the 3MF in the
API process when served. That is the same as for every library file today, and #5296 owns
it.

## 9. Links

### 9.1 What the CRUD service accepts

Any link that passes these text rules, at the edge and again at the CRUD service: at most
2048 characters; no whitespace or control character; scheme `http` or `https`; a host; no
user name or password; the host in IDNA ASCII form, lower-cased. The CRUD service stores the
text. It never fetches, resolves or follows it.

### 9.2 The handler registry

`orders/customer_links.py`: a list of `LinkHandler` values, each an allowed scheme
(`https`), an exact host set, a full-match path pattern and a canonical-id function. The
first match gives `link_type` and `canonical`; no match is `manual`; a `purchase` link is
always `manual`.

| Type | Hosts (exact) | Path pattern | Canonical | What happens |
| --- | --- | --- | --- | --- |
| `makerworld` | `makerworld.com`, `www.makerworld.com` | `^/(?:[a-z]{2}(?:-[a-z]{2,4})?/)?models/(\d{1,12})(?:-[^/]*)?/?$` | the model id | imported through #6128 |
| `printables` | `www.printables.com`, `printables.com` | `^/(?:[a-z]{2}/)?model/(\d{1,12})(?:-[^/]*)?/?$` | the model id | typed only; the shop downloads |
| `thingiverse` | `www.thingiverse.com`, `thingiverse.com` | `^/thing:(\d{1,12})/?$` | the thing id | typed only; the shop downloads |

A port other than 443 never matches. The query and fragment are ignored for the match and
kept in the text. #6128's resolve (`POST /api/v1/makerworld/resolve`, Bambuddy's own parser)
is the authority for what MakerWorld accepts; the registry only decides which links go
there.

### 9.3 Manual links

A `manual` link shows in the back office as "To process by hand", with the reason when an
import fell back (Bambuddy's words through #6128, such as a sign-in requirement), and the
link as text. It opens only on a person's click: an anchor with `target="_blank"`,
`rel="noopener noreferrer"` and `referrerpolicy="no-referrer"`, rendered only after the
scheme is checked again. Nothing fetches, previews, unfurls or prefetches it. On the
storefront every customer link is escaped text in a React text node, never an anchor.

## 10. Purchase links and cost pass-through

### 10.1 What the customer gives

A link's purpose is `print` or `purchase`, chosen per link, with an optional note. A
`purchase` link is always `manual`; the shop buys it by hand.

### 10.2 What the shop records, in one write

`Lifecycle.record_purchase(source_id, amount_minor, cost_kind, note, by)`, one transaction
under the order's lock:

1. a cost entry (the store's `insert_cost_entry`, as `Lifecycle.add_cost_entry` uses it): the
   source's item, `kind` `material` (parts) or `other` (a paid model file), `source`
   `purchase:<source id>` (it matches `ENTRY_SOURCE`, `^[a-z_]+:\S{1,200}$`, so a repeat
   writes once);
2. a `purchase` charge of the same `amount_minor`, `purchase_link_id` the source, label
   "Bought for your project: <the customer's note, or the link's host>" (at most 200
   characters);
3. the source `recorded` with both ids;
4. the handling fee recomputed (§10.3) and the auto tax recomputed (`_retax`);
5. the audit row `purchase.recorded`; the bus is told after commit.

The charges have no pass-through link today (`ChargeInput`, `orders/models.py:234`), so:

- `update_purchase` changes the cost entry and the charge together (the store gets a cost
  entry update used only here); `decline_purchase(shop_note)` removes both and sets
  `not_needed`;
- a charges replace (`replace_charges`, `replace_charges_after_acceptance`) keeps `purchase`
  and `handling` rows, as `_typed` keeps the auto tax row, and ignores them in a body;
- a cost entry remove refuses `purchase:` entries (409 `purchase_cost_linked`);
- the CHECK and UNIQUE of §5.5 refuse an unlinked `purchase` charge and two per link;
- once the order is accepted, `update_purchase` and `decline_purchase` are 409
  `purchase_locked` **(call)**; a later price change goes through the after-acceptance
  charge route (#5351) as its own charge.

### 10.3 The handling fee

Settings → Orders gets `orders_purchase_fee_flat_minor` (per recorded purchase, in the
shop's `orders_currency`, default 0) and `orders_purchase_fee_percent` (0 to 100, two
decimals, default 0). The default is none.

The fee is **its own customer-visible charge line** **(call)**, kind `handling`, label
"Handling for bought items", one per order: flat × the recorded purchases + percent × their
sum, rounded half up once. It is shown apart so the customer sees what they pay for the
things and what for the shop's work. The lifecycle recomputes it whenever a purchase is
recorded, changed or declined, and removes the row at 0. It is a charge only: it never
becomes a cost entry, so job cost never counts it as a cost (`orders/costs.py` counts
charges as revenue). An order not in the shop's currency takes the percentage only (the flat
amount is in the shop's currency).

Tax: the auto tax counts the items and, with `orders_tax_shipping`, shipping
(`backend/CLAUDE.md`, #5281), so purchases and the fee are not taxed by it. A typed `tax`
charge still can.

### 10.4 The gate: `purchase_pending`

While an order has a `purchase` source with `status = pending`,
`Lifecycle._no_pending_purchase(conn, order)` refuses, with
`LifecycleError("purchase_pending", …)`, HTTP 409, problem type
`https://scadbuddy.dev/problems/orders/purchase_pending`, naming the pending sources:

- `_accept_quote` (the customer's accept, a `together` revision accept that runs it, and the
  shop's "Accept for the customer");
- `_accept_order` (so `_accepted_by_the_last` skips the order accept with its audit row
  `order.accept_skipped`);
- `open_payment_request` for kind `balance`.

A `deposit` request is not gated (the owner's decision): submit opens it before the shop can
price a purchase. The customer's view sets `can_accept` false while the gate is on and shows
"Purchase pending: the shop is pricing <n> thing(s) to buy for you."

### 10.5 What the customer sees

`CustomerOrderView` and `ORDER_VIEW_FIELDS` gain:

- `charges`: `kind`, `label`, `amount_minor` (so shipping and tax show too, which is
  intended: the customer must see what they pay);
- `sources`: per request item, `kind`, `purpose`, `filename`, `url` (text), `note`, `status`,
  `amount_minor` (a recorded purchase's), `shop_note`, `preview_url` (the edge's route that
  streams it, §7, never a presigned `GET`).

No cost entry, margin, staff name or import reason reaches the customer.

## 11. The back office

The order page's request item gets a "Customer's models" panel: each file (name, size, check
state, facts, preview, "Download" as a presigned `GET` while staged or the library file once
imported, "Check again", "Import"), each link (type, the link as text, "Open link", "Import
again" for MakerWorld, "Use library file", "Decline"), and each purchase ("Open link", the
note, "Record cost", "Change", "Not needed"). The order header shows "Purchase pending (n)".
The orders list filters by "own model", "purchase pending" and "import stuck" (§8.1); a
stuck source shows "Import stuck", its reason and "Retry import". Settings → Orders gets the two
fee fields.

Agent tools: `order_get` shows the sources; `import_staged_model`,
`order_item_source_resolve`, `order_item_source_manual`, `order_item_source_decline` (write);
`order_purchase_record`, `order_purchase_update`, `order_purchase_decline` (write). No tool
fetches a link.

## 12. eh-homelab/clusters

One PR: the `scadbuddy-intake` Deployment (the app image, `python -m scadbuddy.intake_worker`,
a ServiceAccount whose projected token the CRUD service exchanges, no database secret, no
store credential, read-only root, a memory limit, its egress policy), and the bucket's CORS
rule (§6.4).

## 13. Phases, one PR each

1. Data and staging: migrations, the registry, presign, claim, each file's watcher and its
   clean-up (§6.5), the backstop, the CRUD and edge routes, CORS config read.
2. The intake worker and its check.
3. Purchases, the fee, the gate.
4. The storefront form, cart and order page; the back office panel and fee settings;
   "Import stuck" and "Retry import".
5. After #6128 merges: `staged_model_import`, `copy_staged_to_native`, the watcher's
   import child and its stuck state (§8.1), the `customer-model-intake` flow and the source
   tools.
6. eh-homelab/clusters: the Deployment and the CORS rule.

## 14. Out of scope

Automatic purchase; files over 256 MiB; STEP, OBJ or gcode; an instant price for an upload;
fetchers for Printables or Thingiverse; moving the native library's cover reads out of the
API (#5296); a tax rule for purchases.

## Sources

- AWS, presigned URLs (expiry checked at request time):
  https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html
- AWS, S3 quick facts (5 GB per `PUT`): https://docs.aws.amazon.com/AmazonS3/latest/userguide/qfacts.html
- DigitalOcean Spaces, configure CORS: https://docs.digitalocean.com/products/spaces/how-to/configure-cors/
- Cloudflare, 413 limits per plan (why uploads skip the proxied host):
  https://developers.cloudflare.com/support/troubleshooting/http-status-codes/4xx-client-error/error-413/
- PR #6128 (MakerWorld import through ScadBuddy's API), its body and code at its head on
  2026-11-13.
- Measured on 2026-11-13: the storefront's DNS and headers (`dig`, `curl -sI`: Cloudflare),
  the fixtures' sizes (`ls -la`) and zip directories (`zipfile.ZipFile.infolist`), and the
  signed headers of an aiobotocore presigned `PUT`.
