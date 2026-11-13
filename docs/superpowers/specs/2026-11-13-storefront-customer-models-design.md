# Storefront: print the customer's own model, and buy things for their project

Status: **draft, waiting for the owner's approval.** Nothing here is built. It amends the
storefront spec (`2026-11-02-storefront-design.md`, "the storefront spec") and uses the
mesh models spec (`2026-10-11-mesh-models-design.md`, #2146).

The owner asked for three things:

1. "Customers should be able to specify a MakerWorld link or upload a STL or 3MF" to have
   it printed (the request that opened this spec).
2. "Allow all links, but process them based on the link type; an unknown handler = left for
   backoffice to process manually" (2026-11-13, through the coordinator). This replaces a
   makerworld.com-only allowlist.
3. "Users can specify a link is to be purchased for their project (pass along costs)"
   (2026-11-13, through the coordinator).

Every other choice is marked **(call)**: the author made it, and the owner may overturn it.
§15 lists the open questions. Licensing is the owner's call and this spec does not rule on
it.

Every fact about the code was read at `origin/main` `f7a7f39d2` on 2026-11-13. A measured
fact says where and how it was measured.

## 1. Why

Today a storefront customer can buy a listed product or send a design request
(storefront spec §5.4). The design request takes photos, videos and PDFs only
(`frontend/src/storefront/components/RequestForm.tsx:113`,
`accept="image/*,video/*,application/pdf"`; `backend/scadbuddy/storefront/wire.py`
`REQUEST_KINDS`). Many customers already have a model: a file they made, or a page on
MakerWorld, Printables or Thingiverse. They also often need parts that the shop does not
print, such as magnets or screws, or a paid model file. Today they must send these by
message.

## 2. Goals, non-goals, anti-goals

Goals:

- A "Print my own model" entry on the storefront. The customer gives one model source (a
  file upload or a link), a quantity, colour preferences and a note.
- A customer may add links to things the shop must buy for the project. The shop records
  what it paid, and the customer sees that amount on the quote before they pay.
- The item goes through the existing flow: revision, slice quote, accept, pay (storefront
  spec §4, §5.6, §7).
- The back office sees the file or link, a preview of a file, and can turn the file into a
  mesh model and quote it.

Non-goals:

- Automatic download from MakerWorld, Printables or Thingiverse in the first phases (§8.4).
- Automatic purchase of anything. A person always buys.
- An instant price on the storefront for an uploaded model. The shop quotes it.

Anti-goals (never):

- The CRUD service parses, renders or slices a customer's file (#5295). It stores bytes.
- The CRUD service fetches a customer's link.
- The storefront or the back office makes a customer's link into a preview, an unfurl or a
  prefetch.
- A file or a model goes through JSON or text as base64. Files are blobs plus pointers.

## 3. The rule this design follows (#5295)

Only the CRUD service (today's API process) has SQL access. No untrusted input is parsed or
run in a pod that holds a database secret (root `CLAUDE.md`, "Database access";
`docs/security/trust-boundaries.md`). A customer's file and link come from an anonymous
person on the internet, so they are untrusted. This design puts each step in a zone:

| Step | Process | Database secret | What it does with the customer's bytes |
| --- | --- | --- | --- |
| Receive the upload | storefront edge (`python -m scadbuddy.storefront`) | none (it refuses to start with one, `backend/CLAUDE.md`) | caps, magic bytes, the 3MF zip's central directory (§6.2) |
| Store the bytes | CRUD service | yes | caps, a fixed-offset magic-byte compare of the first 84 bytes, a sha256, a write to the blob store (§6.3). It never opens the zip and never reads a triangle. |
| Check, preview, convert | intake worker (new, §7) | none | the full check, the preview, the mesh model conversion |
| Fetch a link (later, §8.4) | intake worker | none | SSRF-guarded fetch |

The CRUD service's magic-byte compare is the same kind of check that it already does for
images and PDFs (`orders/attachments.py` `sniff`, 16 bytes). It reads bytes at fixed
offsets and compares them with constants. It does not decode a format.

## 4. The customer's flow

### 4.1 The entry

The storefront's header and its design-request page get a second entry, "Print my own
model", at `/print-my-model` **(call)**. The page has one form:

1. **Your model**, one of two choices:
   - "Upload a file": one `.stl` or `.3mf` file, at most 95 MiB (§6.1).
   - "Paste a link": any `http` or `https` link (§8).
2. **How many**: a quantity, 1 to the item's existing quantity cap.
3. **Colours**: "Let the shop choose" (the default), or up to 8 colours picked from the
   price book's offered filaments, as a listing's colour slots offer them. The customer
   may say which part gets which colour in the note.
4. **Anything else**: a note, plain text, 0 to 5000 characters (the design request's cap,
   `MAX_REQUEST_TEXT`).
5. **Things to buy for this project** (optional): 0 to 5 purchase links **(call)**, each a
   link and a note of up to 500 characters, for example "the 6 mm magnets, 20 of them"
   (§9).

The bot check (Turnstile) shows under the form. "Add to my order" sends it.

The design-request form gets the same "Things to buy for this project" list, because a
designed part often needs magnets too **(call)**.

### 4.2 What the customer sees after the send

- The cart shows the item as "Your model: <file name>" or "Your model: <link as text>",
  with the quantity, the colours and the note.
- For a file, the cart shows the check's state: "Checking your file", then a preview
  picture and "Ready for the shop", or a reason in plain words (for example "This file is
  not a 3MF: it is not a readable zip"). A refused file blocks submit for that item until
  the customer removes it (`own_model_refused`, 409) **(call)**. A file still being checked
  does not block submit: the shop sees the result later.
- The customer's link is shown back as escaped text only. It is never an anchor, never a
  preview and never fetched by the storefront.
- Each purchase link is shown as escaped text, with its note and "The shop will price this".

### 4.3 After submit

The order goes into the shop's queue (storefront spec §5.5). The shop makes a mesh model
from the file (§10), makes revision 1 from it, slices a quote, and sends the revision with
the quote. The default approval mode for an own-model item is `together` **(call)**: the
customer already chose the design, so one accept takes the design and the price. The
shop records each purchase cost (§9). The customer accepts on the order page and pays, as
for any other item. Until every purchase link has a recorded cost or is marked not needed,
the order shows "Purchase pending" and cannot be accepted or paid (§9.3).

## 5. The data

All changes are new migration files, one per change set, named
`$(date -u +%Y%m%dT%H%MZ)_<slug>.sql` (root `CLAUDE.md`, "Migrations").

### 5.1 The item

An own-model item is an `ItemKind` `request` item (storefront spec §4.2). The spec already
says: "An imported mesh, from MakerWorld or elsewhere, becomes a library mesh model (#2146)
with a slug and a commit, so it fits the same reference. Nothing references a raw file"
(storefront spec §4.1). So no new item kind is needed. A request item has no reference
until its first revision, and `quote` and `order_item_print` refuse it with
`item_has_no_design` until then (storefront spec §4.2).

New column `order_items.request_kind`: `design` (every existing request row) or
`own_model`, null for other kinds, with a CHECK. The back office and the customer's view
use it for the title and the panel. New column `order_items.colour_preferences` (jsonb,
nullable): `{"shop_choice": true}` or `{"filaments": ["<price book id>", …]}`, at most 8.
It records what the customer asked for. The revision's colours (storefront spec §4.3)
stay the colours that the customer accepts.

### 5.2 The file

The file is an order attachment (`order_attachments`), on the subject `order_item:<id>`,
with:

- a new `AttachmentRole` `customer_model`;
- a new `AttachmentKind` `model` (the bytes say STL or 3MF, §6.3), never in `INLINE`, so
  the back office serves it only as a download (`api/order_attachments.py`).

The bytes are the blob `attachment-<sha256>`, held by `blob_refs` as every attachment's is
(`orders/attachments.py` `keep_file`). Nothing stores the file in a row.

### 5.3 The check

New table `order_attachment_checks`: `attachment_id` (PK, FK), `status` (`checking`,
`ready`, `refused`, `failed`), `reason` (a slug and a plain sentence when `refused`),
`facts` (jsonb: `format` `stl`/`3mf`, `objects`, `plates`, `triangles`, the bounding box
in mm, the 3MF's filament colours), `preview_key` (the blob of the preview PNG, held by
`blob_refs (order_attachment_check, <attachment id>)`), `checked_at`. `failed` means the
worker could not finish (a timeout, a crash); the back office can run the check again.

### 5.4 The links

New table `order_item_links`:

| Column | Meaning |
| --- | --- |
| `id`, `order_id`, `item_id` | the item the link belongs to |
| `url` | the customer's link, text, at most 2048 characters (§8.1) |
| `purpose` | `print` (a model to print) or `purchase` (something the shop buys) |
| `link_type` | the handler that matched (`makerworld`, …), or `manual`; always `manual` for `purchase` |
| `canonical` | the handler's canonical id, for example the MakerWorld model id; null for `manual` |
| `note` | the customer's note, at most 500 characters |
| `status` | `print`: `open`, `attached` (the shop attached the model file), `declined`. `purchase`: `pending`, `recorded`, `not_needed` |
| `cost_entry_id`, `charge_id` | for a `recorded` purchase: its cost entry and its charge (§9.2), each UNIQUE |
| `shop_note` | the shop's note, shown to the customer (for example why it is not needed) |
| `created_at`, `updated_at` | |

An own-model item has exactly one model source: a `customer_model` attachment or one link
with `purpose = print`. The CRUD route checks this; a partial unique index keeps at most
one `print` link per item.

### 5.5 Charges

`ChargeKind` (`shipping`, `discount`, `fee`, `tax`; `orders/models.py:67`) gains
`purchase`. `order_charges` gains `purchase_link_id` (nullable, FK, UNIQUE), with a CHECK:
`kind = 'purchase'` exactly when `purchase_link_id` is set. §9.2 says how the cost entry
and the charge stay in step.

## 6. The upload and its limits

### 6.1 The size cap: 95 MiB

The storefront's host is behind Cloudflare: `dig +short michaels-workshop.nullreference.io`
answers 104.26.0.126, 104.26.1.126 and 172.67.74.114, and `curl -sI` shows
`server: cloudflare` and a `cf-ray` header (measured 2026-11-13 from the owner's WSL
machine). Cloudflare refuses a proxied request body over 100 MB on the Free and Pro plans
with a 413 ([Cloudflare, error 413](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/4xx-client-error/error-413/)).

Real project files reach that size. The 16 MakerWorld 3MFs in `test-fixtures/makerworld/`
(copied unchanged from the maintainer's Bambuddy library, `test-fixtures/makerworld/README.md`)
measure, by `ls -la` in the main checkout on 2026-11-13: 98,325,251 bytes
(`381-olive-oil-bottle-passthrough.3mf`), 71,803,767, 54,747,288 and 33,698,429 bytes for
the next three, and under 11 MB for the other twelve.

So the cap is **95 MiB (99,614,720 bytes) for one file** **(call)**, and one file per item.
That is under 100,000,000 bytes with room for the form's text and the multipart framing,
and it takes every fixture (the largest is 93.8 MiB). A larger file needs Cloudflare's
Business plan (200 MB) or a direct upload to the blob store, which is out of scope (§14).

Time: the edge's `MAX_BODY_SECONDS` (300 s) needs 332 KB/s for 95 MiB. A model upload gets
its own `MAX_MODEL_BODY_SECONDS` of 900 s (110 KB/s, a slow home uplink) **(call)**; the
gap and rate rules stay. Cloudflare's proxy read timeout is 125 s from the end of the
request to the origin's first byte of the answer, and only Enterprise can raise it
([Cloudflare, error 524](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-5xx-errors/error-524/)).
So the answer must not wait for the check: the CRUD service stores the bytes, starts the
check (§7) and answers at once. (#6120 is the same limit hit by a long render.)

The process budget: `UploadGate` `MAX_SPOOL_BYTES` (600 MiB) holds six model uploads at
once; the seventh is 503 `uploads_busy`, as today. No change.

### 6.2 At the edge (no database secret)

A new route `POST /api/store/v1/draft/models` (multipart: one `details` part, JSON, at most
16 KiB; at most one `file` part). In order, before its body is read:

1. 404 `requests_disabled` while `storefront_requests_enabled` is off (the same switch as
   design requests) **(call)**;
2. 428 `idempotency_key_required`;
3. `UploadAllowance` (`storefront/ratelimit.py`): the same allowance as design requests,
   6 an hour and one at a time per client, so a client cannot double its uploads by using
   both forms;
4. Turnstile, a new action `model` (`require_turnstile(request, "model")`), checked after
   the allowance so that a client out of allowance costs no Siteverify call, as
   `add_request` does;
5. `UploadGate` (503 `uploads_busy`).

Then the body is read with `uploads.py`'s caps (a new `MAX_MODEL_FILE_BYTES`, the part
header cap, the rate rules) and spooled. Then the edge checks the file's content, never
its name or its declared type:

- **Sniffing.** A 3MF starts with `PK\x03\x04` (a zip local file header). A binary STL is
  `84 + 50 × n` bytes long for the `n` (uint32, little-endian) at offset 80. Anything else
  that starts with `solid` and is printable ASCII to its end is an ASCII STL. Anything else
  is 415 `file_type`. The file name's extension must agree with what the bytes say, or it
  is 415 `file_type` too (a `.stl` that is a zip is refused).
- **The 3MF's zip, central directory only** (Python `zipfile.ZipFile(...).infolist()`;
  it reads the directory and inflates nothing). The limits, from the fixtures measured in
  §6.4: at most 512 entries; each entry declares at most 1 GiB inflated and all together
  at most 2 GiB; an entry over 1 MiB inflated declares at most 200 times its compressed
  size; methods `0` (stored) and `8` (deflate) only; no encrypted entry; no name that is
  absolute, holds `..`, a backslash or a NUL; `[Content_Types].xml` and a root model part
  are present. A refusal is 422 `model_unreadable` with the limit's name.

The edge then forwards the details and the file to the CRUD service as today's
`add_request` does.

### 6.3 At the CRUD service (stores bytes only)

New CRUD route `POST /api/v1/crud/storefront/draft/models` (area `draft`, service
credential required from its first PR, root `CLAUDE.md`). It does not trust the edge's
checks and repeats the ones that need no parsing:

- the body caps (the multipart reader of `uploads.py`, as `add_request` does);
- the sniff of §6.2, by fixed offsets only: `PK\x03\x04` at 0, or the binary STL length
  identity (the uint32 at offset 80), or the ASCII prefix `solid`. It does **not** open the
  zip or read its directory: that is the worker's job (§7);
- the details by Pydantic: quantity, colours against the price book's offered filaments,
  the note's plain-text rule (`checked_text`), each link (§8.1), at most 5 purchase links,
  exactly one model source;
- the stored reply for the same idempotency key, as `add_request` does.

Then, in the request's path: the sha256, the blob write (`keep_file`), and one transaction
that adds the item, the attachment, the links and a `checking` check row to the visitor's
draft (`ensure_draft`, `add_items`, as `add_request` does). After commit it starts the
check workflow (§7). The answer is the draft view; the check's result reaches the cart by
the existing poll (`frontend/src/storefront/poll.ts`).

A link-only entry has no file: the edge takes the same route with no `file` part, and the
CRUD service stores the link as text. It never fetches it in the request path.

### 6.4 The zip-bomb limits, measured

`zipfile.ZipFile(...).infolist()` on the six largest fixtures (script run with
`python3 -I` from the scratchpad on 2026-11-13; it reads central directories only):

| Fixture | Bytes | Entries | Declared inflated | Largest entry | Highest ratio |
| --- | --- | --- | --- | --- | --- |
| 381-olive-oil-bottle-passthrough | 98,325,251 | 44 | 492,337,262 | 241,772,118 | 11.1 |
| 2464-ghost-campfire-light | 71,803,767 | 72 | 427,992,648 | 51,237,776 | 12.0 |
| 2602-tiny-flexi-frog… | 54,747,288 | 31 | 326,059,993 | 238,535,310 | 6.7 |
| 2178-find-100-mini-frogs… | 33,698,429 | 19 | 157,379,992 | 155,578,145 | 40.6 |
| 448-articulated-pumpkin | 10,287,542 | 23 | 64,097,076 | 63,012,386 | 6.8 |
| 1943-mini-articulated-ankylosaurs… | 6,555,948 | 30 | 32,126,907 | 30,918,539 | 6.8 |

Every entry used method 8 (deflate). So the limits in §6.2 leave room of four times or more
on each axis. A zip bomb's ratio is in the hundreds to millions, so the ratio cap is the
main bound, and the declared totals bound the rest.

The library's existing 3MF caps (`library/meshes.py`: 64 entries, 64 MiB an entry, 128 MiB
in all) would refuse four of these six. They are for `// file` assets of at most 8 MB, so
the customer check has its own limits and does not change them.

A declared size can lie. The worker (§7) inflates each entry through a reader that stops
at the declared size and counts what it gets, and the edge's and worker's limits apply to
the declared values first. Python's zip reader stops an entry at the size it declares
(`library/meshes.py` docstring; the `zipfile` reader raises on a mismatch). A 3MF inside a
3MF is never opened.

## 7. The intake worker (no database secret)

A new Temporal worker, `python -m scadbuddy.intake_worker`, in the app image, in its own
Deployment, on its own task queue `customer-files` **(call; §15 Q6)**. It holds no database
secret: it takes a service credential with `exchange_source`
(`auth/service_auth.py`, as the render and projects workers do, `backend/CLAUDE.md`), scoped
to one CRUD area `intake`. The pod has no other secret, a read-only root file system, and a
memory limit. Each parse runs in a child process with an address-space limit and a
timeout, killed with its process group, as `workflows/template_process.py` runs template
Python.

Why not the render worker's queue: the render worker runs template Python and holds the
rights of many CRUD areas, and its Deployment still sets `PGPASSWORD` until #5298 Task 21
(`docs/security/trust-boundaries.md`, the `pipeline/` row). A worker that only reads
customer files, with one narrow area, keeps a hostile file away from both.

The workflow `CustomerModelCheck(attachment_id)`:

1. reads the blob through the internal blob route (`GET /api/v1/internal/blobs/{key}`,
   service credential);
2. checks it: the §6.2 rules again (the CRUD service did not run them), then the read
   itself under the read budget (`render/read_budget.py`) and the mesh caps of
   `library/meshes.py` (`MAX_MESH_TRIANGLES` raised for this use to what the fixtures need;
   the builder measures it on all 16 fixtures with `tests/test_makerworld_fixtures.py`'s
   loader);
3. writes the facts and a preview PNG (`render/thumbnail.py` `render_view`, the plate
   thumbnail's iso view, as `library/meshes.py` `preview_png` does) through
   `PUT /api/v1/crud/intake/checks/{attachment_id}` (area `intake`) and the internal blob
   PUT;
4. on a refusal writes `refused` with the reason; on a timeout or crash the workflow writes
   `failed`.

The workflow `CustomerModelConvert(attachment_id, name)` (the back office's "Make mesh
model", §10) ingests the file into a scene (`meshmodel/ingest.py`) in the worker, writes the
scene and its canonical meshes (re-written by trimesh, so none of the customer's bytes
survive, as `library/meshes.py` does for assets) as one archive blob, and asks the CRUD
service to create the mesh model from it. The CRUD service checks the scene with Pydantic
(`meshmodel/scene.py` `Scene`) and the file names with `SCENE_FILE`
(`meshmodel/operations.py`), and commits the files. It never reads the customer's original
file.

Today the back office's own "create mesh model from upload" reads the file in the API
process (`api/mesh_models.py`, `meshmodel/operations.py`). That is a back-office user's
file, so this spec leaves it as it is (#5296 owns it). A customer's file never takes that
path.

## 8. Links

### 8.1 What the CRUD service accepts

Any link that passes these text rules, at the edge and again at the CRUD service: at most
2048 characters; no whitespace or control character; scheme `http` or `https`; a host; no
user name or password (`user@host`); a host in IDNA ASCII form, lower-cased. The CRUD
service stores the text. It never fetches it, resolves it or follows it.

### 8.2 The handler registry

`orders/customer_links.py` holds the registry: a list of `LinkHandler` values, each with a
`type`, the allowed scheme (`https`), an exact set of hosts, a full-match path pattern, and
a function that gives the canonical id from a match. The first handler that matches gives
`link_type` and `canonical`. A link that no handler matches is `manual`. A `purchase` link
is always `manual`, whatever it matches (§9).

| Type | Hosts (exact) | Path pattern | Canonical | Phase |
| --- | --- | --- | --- | --- |
| `makerworld` | `makerworld.com`, `www.makerworld.com` | `^/(?:[a-z]{2}(?:-[a-z]{2,4})?/)?models/(\d{1,12})(?:-[^/]*)?/?$` | the model id | built first (phase 1) |
| `printables` | `www.printables.com`, `printables.com` | `^/(?:[a-z]{2}/)?model/(\d{1,12})(?:-[^/]*)?/?$` | the model id | described only |
| `thingiverse` | `www.thingiverse.com`, `thingiverse.com` | `^/thing:(\d{1,12})/?$` | the thing id | described only |

The query string and fragment are ignored for the match and kept in the stored text. A port
other than 443 never matches. The path patterns are from the sites' public model links as
people share them; the builder confirms each against sample links before it ships, because
none of the three sites answered an automated fetch (§8.4). `makerworld.com.cn` and other
hosts are `manual` until a handler names them.

What a handler does in phase 1: it gives the link a type and a label ("MakerWorld model
123456") for the back office. It does not fetch.

### 8.3 Manual links

A `manual` link is shown in the back office as "To process by hand", with the link as text.
It is opened only by a person's click on an anchor with `target="_blank"`,
`rel="noopener noreferrer"` and `referrerpolicy="no-referrer"`. The back office never
fetches, previews, unfurls or prefetches it, and it checks the scheme again before it
renders the `href`, so only `http` and `https` can be an anchor.

On the storefront, every customer link (print or purchase, any type) is shown back as
escaped text in a React text node, never as an anchor (the owner's ruling). Nothing a
customer writes is rendered as HTML (storefront spec §5.8).

### 8.4 Fetch automatically, or download by hand

**Recommendation: download by hand** for every handler in phases 1 and 2. The shop opens
the MakerWorld page, downloads the 3MF with its own account, and attaches it to the item in
the back office ("Attach the model file"). The attached file then takes the same check as a
customer's upload (§7).

The reasons:

- **Terms.** MakerWorld's Terms of Use page answered HTTP 403 to an automated fetch
  (WebFetch of `https://makerworld.com/en/terms-of-use`, 2026-11-13), so this spec could
  not read what it says about automated access. Printables' (`https://www.printables.com/terms`)
  and Thingiverse's (`https://www.thingiverse.com/legal`) terms pages answered 403 too, the
  same day. A search found no MakerWorld text that permits automated downloads and no
  official public download API ([Bambu Lab forum, "Scrape website data / api?"](https://forum.bambulab.com/t/scrape-website-data-api/113401)).
  MakerWorld's `robots.txt` allows crawling of most paths
  ([makerworld.com/robots.txt](https://makerworld.com/robots.txt), read 2026-11-13), but
  `robots.txt` is not a licence to download files.
- **Sign-in.** Some MakerWorld models need a signed-in account to download
  ([stacksheriff, MakerWorld guide](https://stacksheriff.com/3d-printing/makerworld-guide/),
  a secondary source). An automatic fetch would need the shop's MakerWorld credentials in a
  pod, which is a new secret to guard.
- **SSRF.** A fetcher reached by an anonymous customer's input is the classic SSRF path.
  The hand path has none.
- **Volume.** A small shop gets few such orders; a person's click per order is cheap.

If the owner later wants a handler to fetch (phase 3, only on the owner's word and only for
a site whose terms allow it), the fetch runs in the intake worker (§7, no database secret)
with these rules:

- `https` only, to that handler's exact hosts only;
- resolve the host, and refuse when any answer is private (RFC 1918), loopback, link-local
  (169.254.0.0/16, fe80::/10), unique-local (fc00::/7), carrier-grade NAT (100.64.0.0/10),
  multicast, unspecified, `0.0.0.0/8`, or a metadata address (169.254.169.254,
  fd00:ec2::254); an IPv4-mapped IPv6 address is checked as its IPv4 address;
- connect to the address that passed the check (no second resolve), with the host name for
  SNI and the `Host` header;
- follow at most 3 redirects, by hand; check each target's scheme and host against the
  handler and resolve and check it again;
- stop at 95 MiB (the upload cap) and 60 seconds in all; ignore proxy environment variables;
- check the result's content as an upload's (§6.2, §7), never by its `Content-Type`;
- the pod's egress policy allows only port 443 to public addresses and the CRUD service
  (eh-homelab/clusters#3182 owns the network policies).

## 9. Purchase links and cost pass-through

### 9.1 What the customer gives

Each link carries a purpose, chosen per link: `print` (§8) or `purchase` (something the
shop buys for this project: a paid model file, magnets, hardware, a kit), with an optional
note. A `purchase` link is always `manual`: the shop buys it by hand. No handler buys
anything.

### 9.2 What the shop records, in one write

The shop buys the thing and records what it paid: an amount in the order's currency **(call:
the shop converts a foreign price itself)**, a cost kind (`material` for parts, `other` for
a paid model file), and an optional note. `Lifecycle.record_purchase(link_id, amount_minor,
cost_kind, note, by)` does, in one transaction under the order's lock:

1. a cost entry (`orders/costs.py`, the store's `insert_cost_entry` as
   `Lifecycle.add_cost_entry` uses it): the link's item, `kind` `material` or `other`,
   `source` `purchase:<link id>` (matches `ENTRY_SOURCE`, `^[a-z_]+:\S{1,200}$`, so a
   repeated write writes once), the note;
2. a charge (`order_charges`): `kind` `purchase`, the same `amount_minor`,
   `purchase_link_id` the link, `label` "Bought for your project: <the customer's note, or
   the link's host>" (at most 200 characters, the charge label cap);
3. the link: `status` `recorded`, `cost_entry_id`, `charge_id`;
4. the auto tax recomputed (`_retax`), as any charge change does today;
5. the audit row `purchase.recorded`, and the bus told after commit.

No markup by default: the charge equals the cost (§15 Q4).

The charges model has no pass-through link today (`ChargeInput`, `orders/models.py:234`),
so these rules keep the two in step:

- **Change.** `Lifecycle.update_purchase` changes the cost entry's amount, kind and note and
  the charge's amount and label in one transaction. (Cost entries have no update today; the
  store gets an update used only here.)
- **Not needed.** `Lifecycle.decline_purchase(link_id, shop_note)` removes the cost entry and
  the charge, if any, and sets `not_needed` with the shop's note, which the customer sees.
- **Other writers cannot split them.** A charges replace (`replace_charges`,
  `replace_charges_after_acceptance`) keeps every `purchase` row, as `_typed` keeps the auto
  tax row, and ignores a `purchase` row sent back in its body. A cost entry remove refuses
  an entry whose `source` starts with `purchase:` (409 `purchase_cost_linked`: use the link's
  "Change" or "Not needed"). The DB CHECK and UNIQUE of §5.5 refuse a `purchase` charge with
  no link and two charges for one link.
- **After acceptance.** The gate (§9.3) means every purchase is recorded before the order
  is accepted. Once it is accepted, `update_purchase` and `decline_purchase` are refused
  409 `purchase_locked` **(call)**; a later change of price goes through the existing
  after-acceptance charge route (#5349) as a separate charge.

### 9.3 The gate: `purchase_pending`

While an order has a link with `purpose = purchase` and `status = pending`, these are
refused with `LifecycleError("purchase_pending", …)`, HTTP 409, problem type
`…/problems/orders/purchase_pending`, naming the pending links:

- `_accept_quote` (the customer's accept of a quote, a `together` revision accept that runs
  it, and the shop's "Accept for the customer");
- `_accept_order` (so `_accepted_by_the_last` skips the order accept with the audit row
  `order.accept_skipped`, as it does for any refusal);
- `open_payment_request` for kind `balance`.

A `deposit` payment request is not gated **(call; §15 Q3)**: submit opens it (storefront
spec §8) before the shop can price a purchase, and a deposit is a fixed amount that
protects design work.

The check is one helper, `Lifecycle._no_pending_purchase(conn, order)`, called under the
order's lock. The customer's view sets `can_accept` false on each quote while the gate is
on, and the order page shows "Purchase pending: the shop is pricing <n> thing(s) to buy for
you."

### 9.4 What the customer sees

The customer's order view (`CustomerOrderView`) gains two lists, and `ORDER_VIEW_FIELDS`
(`storefront/wire.py`) gains them too, or `test_order_page.py` fails:

- `charges`: `kind`, `label`, `amount_minor`. Today the view shows no charges, so this also
  shows shipping and tax; that is intended, because the customer must see what they pay.
- `purchases`: `url` (text), `note`, `status`, `amount_minor`, `shop_note`.

No cost entry, margin or staff name reaches the customer: they see the charge, never the
cost entry.

## 10. The back office

The order page's item row, for a `request_kind = own_model` item, gets a "Customer's model"
panel:

- **A file:** its name, size, the first 12 characters of its sha256, the check's state, the
  facts (format, objects, plates, triangles, size in mm, colours) and the preview picture;
  "Download"; "Check again" (for `failed`); "Make mesh model" (once `ready`): it asks for a
  name, runs `CustomerModelConvert` (§7), and opens the new model. Then the shop makes
  revision 1 from it, slices a quote, and sends it, as for any request item (storefront
  spec §4).
- **A print link:** its type ("MakerWorld model 123456" or "To process by hand"), the link
  as text, "Open link" (§8.3), and "Attach the model file" (the shop's download, through
  the back office's attachment upload, role `customer_model`; it then takes the check).
  "Decline" sets `declined` with a note.
- **Purchase links** (on any request item): each link as text with "Open link", the
  customer's note, and "Record cost" (amount, cost kind, note), "Change", "Not needed".
  The order header shows "Purchase pending (n)" while the gate is on.

The orders list can filter by "own model" and by "purchase pending".

Agent tools (outward or write as the existing order tools are): `order_get` shows the
links, the check and the facts; `order_item_model_make` (write; runs the convert);
`order_item_link_attach` (write); `order_purchase_record`, `order_purchase_update`,
`order_purchase_decline` (write). The tools never fetch a link.

## 11. Retention

A swept cart (`housekeeping_sweep_storefront_drafts`) deletes its attachment rows first, as
today, so the file's and the preview's `blob_refs` go and the store's sweep removes the
blobs. Link rows go with the item. A submitted order keeps everything (the orders spec's
Ruling 6).

## 12. Tests the builder adds

- Edge and CRUD: each sniff case (binary STL by length, ASCII STL, 3MF, a `.stl` that is a
  zip, a PNG named `.3mf`), each zip limit (a crafted bomb, too many entries, an encrypted
  entry, a `..` name), the 95 MiB cap counted from the bytes, the shared allowance, the
  Turnstile action, the idempotent retry.
- The CRUD service never imports `zipfile`, `trimesh` or the mesh readers on the upload
  path: a test that the route's module closure does not import them.
- The worker: all 16 fixtures pass the check and convert; the bombs are refused in the
  worker too; a parse past its memory limit is `failed`, not a crash of the worker.
- Links: each handler's matches and near misses (another host, a port, `http`, a userinfo
  link, `makerworld.com.cn`); `purchase` is always `manual`.
- Purchases: one write adds both; change updates both; decline removes both; a charges
  replace keeps the row; a cost entry remove is refused; the gate's three refusals and the
  deposit's exemption; the customer's view's allowlist.
- Frontend: the form (both choices, the 95 MiB check before upload, the purchase list),
  the cart's check states, the escaped link text (a link with `<script>` shows as text),
  the order page's "Purchase pending" and charges; mocks under
  `src/mocks/features/storefrontPublic.ts`; e2e at 390 px.

## 13. Phases

1. **Own model, file and link.** §4 to §8.3, §10 for files and print links, the intake
   worker and its Deployment (a PR in eh-homelab/clusters), the MakerWorld handler without
   fetch, `manual` links.
2. **Purchase links.** §9 and its back office and agent parts.
3. **Only on the owner's word:** a fetching handler (§8.4), and the Printables and
   Thingiverse recognisers.

## 14. Out of scope

- Automatic download from any site, and automatic purchase.
- Files over 95 MiB, and direct-to-store (presigned) uploads that would avoid the
  Cloudflare cap.
- STEP, OBJ, gcode or zipped project folders.
- An instant storefront price for an uploaded model.
- Moving the back office's own mesh-model upload out of the API process (#5296).
- A markup on purchases (§15 Q4).

## 15. Open questions for the owner

1. **Size cap.** 95 MiB per file, set by Cloudflare's 100 MB limit on Free and Pro. Is that
   enough, or do you want the Business plan or direct uploads later?
2. **Approval mode.** Own-model items default to `together` (design and price in one
   accept). Agree?
3. **Deposit.** The `purchase_pending` gate blocks accepting and the balance payment, but
   not the deposit that submit opens. Agree?
4. **Markup.** Should a handling fee or percentage on purchases be configurable? (Default:
   none.)
5. **MakerWorld fetch.** Keep "download by hand" for MakerWorld and every other site
   (recommended)?
6. **Intake worker.** A new worker Deployment for customer files (recommended), instead of
   the render worker's queue. It needs a PR in eh-homelab/clusters.
7. **Purchase links.** At most 5 per item, and allowed on design requests too. Agree?

## Sources

- Cloudflare, 413 upload limits per plan:
  https://developers.cloudflare.com/support/troubleshooting/http-status-codes/4xx-client-error/error-413/
- Cloudflare, 524 proxy read timeout (125 s):
  https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-5xx-errors/error-524/
- MakerWorld robots.txt: https://makerworld.com/robots.txt
- Bambu Lab forum, "Scrape Website data / api?": https://forum.bambulab.com/t/scrape-website-data-api/113401
- stacksheriff, MakerWorld guide: https://stacksheriff.com/3d-printing/makerworld-guide/
- Measured on 2026-11-13: the storefront's DNS and headers (`dig`, `curl -sI`), the
  fixtures' sizes (`ls -la`) and zip directories (`zipfile.ZipFile.infolist`), and the 403
  answers of the three sites' terms pages (WebFetch).
