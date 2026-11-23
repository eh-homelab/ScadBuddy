# Hosted plugins and marketplace feeds

Date: 2026-11-21. Status: draft for the owner's review.

## 1. Intent

Skills are files in a git repository today. A change to a skill means a commit, a push and
a re-pin (or a deploy, for the built-in `scadbuddy` plugin). The owner wants two things:

1. Create and edit skills (with any files beside `SKILL.md`) and subagents inside
   ScadBuddy, and have the assistant load them with no deploy.
2. Share the same plugins with Claude Code sessions outside ScadBuddy, through a
   marketplace feed that ScadBuddy serves from its API.

ScadBuddy also installs plugins from any URL that Claude Code accepts, not only git, and
the feed can list plugins from outside sources, pinned.

Everything fits the existing plugin package model (#297): a hosted plugin is one more
package source, its published versions are pins, and the feed lists packages. There is
no second lifecycle (owner, 2026-11-21: "we should be able to fit into the existing model
for plugins").

## 2. Goals, non-goals, anti-goals

Goals:

- Postgres and the blob store are the record of every hosted plugin. ScadBuddy builds each
  plugin package and the feed's `marketplace.json` from them.
- A hosted plugin is a file tree: `skills/<skill>/SKILL.md` plus any files under
  `skills/<skill>/`, `agents/<agent>.md`, and `commands/<command>.md`.
- Edits are drafts. A publish freezes an immutable, numbered version.
- A publish puts the version into the package list as an install or a re-pin. The
  assistant loads it once it is approved, as for any package.
- Claude Code installs the feed with `/plugin marketplace add <feed URL>`.
- The installer accepts every pinnable source that Claude Code accepts (section 6.6).
- The feed can list outside plugins, pinned to the bytes ScadBuddy reviewed (section 6.7).
- Every action works from the UI, the REST API and the assistant's MCP tools
  (memory: every tool works in every mode).

Non-goals (first version):

- Hooks, `.mcp.json`, LSP servers or `bin/` in a hosted plugin. These run code on every
  client. External packages keep today's vet rules (`vet.ts`) for them.
- `command` plugin sources, anywhere. They run a local command.
- More than one feed in the UI. The tables carry a feed id, so more feeds later need no
  migration.
- Auto-update of outside plugins. A re-pin stays explicit, with a diff, as today.

Anti-goals (a failure even if it works):

- A turn sees a half-edited skill.
- A client installs bytes that nobody reviewed or pinned.
- A new database client, or SQL access from the agent for this data (`CLAUDE.md`,
  "Database access (#5295)").
- An image or file sent as base64 inside JSON (#2479). Bytes move as bytes or blob
  pointers.

## 3. How it works today

- Plugin packages (#297, `docs/ai/operating.md` §9): `ai_plugin_packages`
  (`backend/scadbuddy/migrations_agent/20260928T0750Z_plugin_packages.sql`) holds the
  source, the commit pin, the content hash (sha256 of the file list), the review, the
  approval and the enabled flag. The agent reads and writes it over CRUD routes
  (`backend/scadbuddy/api/crud_agent_packages.py`, `agent/src/plugins/packages/store.ts`).
- Lifecycle (`store.ts` header): install, then vet and store unapproved; approve the exact
  (commit, hash); enable; re-pin into `pending_*` with a file diff, approved again.
- Sources (`agent/src/plugins/packages/source.ts`): `git` (repo, ref, path) and
  `marketplace` (a git repo with `.claude-plugin/marketplace.json`, plus an entry name).
  `marketplaceTarget()` in `install.ts` accepts entry sources `github`, `url`,
  `git-subdir` and relative paths. It refuses `archive`, `npm` and `command` ("only git
  sources can be pinned").
- Materialise (`install.ts` `materialise()`): files are cached under
  `<state dir>/plugins/<name>/<commit>-<hash prefix>`, hashed against the pin before
  every load and vetted again (`vetPackage`). The Agent SDK loads plugins by local path
  only ([Agent SDK plugins](https://code.claude.com/docs/en/agent-sdk/plugins)).
- Access (#4757): package skills and subagents can be turned on and off one by one.
- Personal access tokens (`backend/scadbuddy/auth/personal_tokens.py`): scopes `read` and
  `write`, prefix `sbpat_`, at most 365 days.

## 4. What Claude Code accepts (external facts)

From [host-marketplace](https://code.claude.com/docs/en/plugins/host-marketplace.md) and
[marketplace-reference](https://code.claude.com/docs/en/plugins/marketplace-reference.md),
read 2026-11-21. The reference page was read in part as a summary. Re-check the key names
marked (S) before you build on them.

- `/plugin marketplace add https://plugins.example.com/marketplace.json` takes a bare
  HTTPS URL. "Users don't need `git` for the catalog itself."
- With a bare URL, "Claude Code downloads only that file", so relative plugin paths do not
  resolve. Give each entry "a source that can be fetched on its own, such as a `github`
  repository or an `archive` URL".
- `archive` source: a zip over `https://`, with an optional `sha256`. At most 256 MiB,
  120 s and five redirects.
- Auth: a `url` marketplace source takes `headers` and `headersHelper` (S). Archive entries
  take `headers` and `headersHelper` from Claude Code v2.1.238 (S). A `headersHelper`
  needs `"strict": false`, and credential-like environment variables are removed from it.
- Pinning: `sha` on `github`, `url` and `git-subdir` entries. A `version` field pins the
  plugin until it changes. With no `version`, users track the source.
- Updates: background auto-update is off by default. Users run
  `/plugin marketplace update <name>`.

## 5. Approaches

- **A. Database rows, with immutable published versions (chosen by the owner).** Drafts and
  published versions live in Postgres plus the blob store. A publish builds one zip. The
  feed is generated from the rows, and each entry is an `archive` with its sha256.
  Trade-off: an edit needs a publish before it is live. In return, nothing ever loads a
  half-edited plugin, and every client pins exact bytes.
- **B. Database edits committed to a git repository.** This reuses the git installer
  unchanged, and history comes for free. It needs a git server or a push from a pod
  (credentials, egress), and outside clients need git access. Rejected: many moving parts
  just to avoid writing a zip.
- **C. A zip built live on each fetch, with no versions.** Rejected: nothing could be
  pinned, the content-hash check breaks, and edits would change a skill during a turn.

## 6. Design

### 6.1 Data (beside `ai_plugin_packages`, `backend/scadbuddy/migrations_agent/`)

The tables sit beside `ai_plugin_packages`, in the same schema, behind the same CRUD area
(`crud_agent_packages.py`, extended). They take the schema's `ai_` prefix. Only the API
holds SQL access: its public `/api/v1/hosted-plugins` routes (6.2, 6.3) write the
`ai_hosted_plugin*` tables, and the agent reads them, and writes the feed list, only through
the CRUD area. Each new agent migration also gets its `expected-migrations.json` entry.

- `ai_plugin_feeds`: `id`, `slug` (in the URL), `name` (the marketplace `name`), `owner`
  text, `created_at`. One row, `default`, is created by the migration.
- `ai_plugin_feed_packages`: (`feed_id`, `package_name`), referencing `ai_plugin_packages`.
  This is how a package (hosted or installed from outside) is listed in a feed.
- `ai_hosted_plugins`: `name` (the same alphabet as `ai_plugin_packages.name`; it is the
  package's name), `description`, `category`, `tags`, `latest_version` (null until the
  first publish), timestamps.
- `ai_hosted_plugin_files` (the draft): `plugin_id`, `path`, `sha256` (a blob pointer),
  `size`, `executable`, `updated_at`. Primary key (`plugin_id`, `path`).
- `ai_hosted_plugin_versions`: `plugin_id`, `version` (an integer, from 1), `zip_sha256`
  (a blob pointer to the zip), `content_hash` (the `hash.ts` file-list hash, so the agent
  checks it as it checks git pins), `files` jsonb (as `ai_plugin_packages.files`),
  `published_at`, `published_by`. Never updated or deleted, except by deleting the
  plugin. The review lives where it lives for every package: on the
  `ai_plugin_packages` pin or its `pending_*` columns.

File bytes and zips go to the ContentStore, never `bytea` (memory: Postgres for state, the
blob store for bytes).

### 6.2 Editing a draft

Routes under `/api/v1/hosted-plugins` (the API). They are `api` routes
(`auth/routes.py`), not `person` routes: a person, an access token with `write` for a
write, or the agent's service credential may call them. The `person` class refuses every
access token, and the agent's credential is what the MCP tools of #6938 call them with.

- `POST /` creates a plugin. `GET /`, `GET /{name}`, `PATCH /{name}` and `DELETE /{name}`
  list, read, change and delete it. `PATCH` changes `description`, `category` and `tags`,
  checked as at create. The name never changes.
- `GET /{name}/files` lists the draft. `GET`, `PUT` and `DELETE /{name}/files/{path}`
  read, write and delete one file. `PUT` takes the raw bytes as the body.
- `POST /{name}/files/moves` renames a path.
- `GET /{name}/draft-diff` shows the draft against the latest version. Its
  `manifest_changed` is true when only the generated `plugin.json` differs (a `PATCH`
  changed the description). Such a draft still publishes: a new description reaches the
  next version, and `publish` answers `nothing-to-publish` (409) only when the files and
  the description are both the same as the latest version's.

Path rules, checked at write: UTF-8, relative, no `..`, no segment that starts with `.`,
no symlinks. Allowed top-level folders are `skills/`, `agents/` and
`commands/`. `.claude-plugin/plugin.json` is generated at publish and refused as a write.
Caps (my call, change them in review): 2 MiB per file, 500 files and 20 MiB per plugin.

The assistant gets matching MCP tools: `hosted_plugin_create`, `hosted_plugin_list`,
`hosted_plugin_update` (the `PATCH`), `hosted_plugin_file_read`, `hosted_plugin_file_write`, `hosted_plugin_file_delete` and
`hosted_plugin_publish`. A draft write is not outward: it changes nothing that loads.
`hosted_plugin_publish` is outward, because it also installs or re-pins the package
(6.3 step 5), and installing is an outward settings write (`agent/src/routes/pluginPackages.ts`,
spec §8.1). It returns a pending action. Once a human approves it in the UI,
`confirm_action` publishes the version and runs the install or re-pin once. That happens
in-process, through `PackageInstaller`, not through the UI-guarded package routes, which
refuse a call that does not come from the UI's origin. After that, the package approval
(6.5) is still needed before anything loads, as for every package.

The UI is an editor under Settings → Tools & plugins → Hosted plugins: a file tree, a
Monaco editor (the one the template editor uses), "New skill" (it writes a `SKILL.md`
with frontmatter), the draft diff and Publish. Load `frontend-design` for this work.

### 6.3 Publish

`POST /{name}/publish`:

1. Validate the draft. Each `skills/<s>/` has a `SKILL.md` with `name` and `description`
   frontmatter. Each `agents/*.md` and `commands/*.md` parses. Apply the same structural
   rules as `vet.ts`. One rule is stricter than `vet.ts`: dynamic context injection (the
   `!` followed by a backtick command) is refused in every `.md` file of the draft, not
   only in a skill or command. The agent still runs `vetPackage` at every load, so a gap
   here fails closed there.
2. Generate `.claude-plugin/plugin.json`: `name`, `description`, `version` `"<N>.0.0"`.
3. Build the zip deterministically: sorted paths, a fixed mtime, mode 0644 or 0755, no
   extra fields. The same draft gives the same bytes, and the same `zip_sha256`.
4. Store the zip blob, then write the version row and set `latest_version` in one
   transaction. The blob goes first and outside the transaction: the store cannot join a
   database transaction, and the row must name a blob that exists. A transaction that
   fails (for example `draft-changed`, 409) leaves an unreferenced blob. No row holds it
   in `blob_refs`, so the store's `sweep_blobs` removes it after its grace period. The
   same draft gives the same zip, so a publish that is tried again reuses the blob.
5. Answer the new version number. The API does not vet and does not touch
   `ai_plugin_packages`. The version is then offered to the package model with source
   `{ kind: 'hosted', name }`, which always takes the plugin's `latest_version`:
   - from the UI, through the agent's existing UI-guarded install and re-pin routes
     (an outward settings write, confirmed in the UI as every install is);
   - from the `hosted_plugin_publish` tool, in-process after its pending action is
     approved (6.2).

   Install or re-pin is chosen by whether an `ai_plugin_packages` row for the name
   exists, never by the version number. The agent's installer fetches, vets and stores
   it as it does every package. So there is one vetter (`vet.ts`), the review always has
   the `PackageReview` shape, and there is no API-to-agent call (my call).

   A caller that stops between the publish and the install leaves a version that no pin
   names. Nothing is lost: the Hosted plugins editor marks a plugin whose
   `latest_version` is neither its pin nor its pending pin ("version N not offered"), with
   an Offer button that runs the same install or re-pin. The next publish offers the new
   latest version anyway.

There is no "unpublish" of one version. Deleting the plugin deletes its package, which
removes it from the feed and the assistant.

### 6.4 The feed

`GET /api/v1/ai/plugin-feeds/{feed}/marketplace.json` (the agent) lists each package in
`ai_plugin_feed_packages` at its current approved pin. A pending re-pin is not listed until
it is approved, and a package with no approved pin is not listed. A hosted package's
entry is:

```json
{
  "name": "<feed name>",
  "owner": { "name": "<feed owner>" },
  "plugins": [
    {
      "name": "<plugin>",
      "description": "…",
      "version": "<N>.0.0",
      "source": {
        "source": "archive",
        "url": "<request origin>/api/v1/ai/plugin-feeds/<feed>/plugins/<plugin>/versions/<N>.zip",
        "sha256": "<zip_sha256>"
      }
    }
  ]
}
```

An outside package's entry is in section 6.7.

- `GET …/plugins/{name}/versions/{N}.zip` serves the zip bytes. A version never changes,
  so it is served with a long `Cache-Control: immutable`.
- Each zip URL uses the origin the `marketplace.json` request came on: its `Host`, or a
  trusted proxy's `X-Forwarded-Proto` and `X-Forwarded-Host`, read as the agent's origin
  checks read them (`agent/src/http/origins.ts` `effectiveRequest`). It does not use
  `public_url`. Claude Code sends a marketplace `url` source's `headers` only to archive
  downloads on the marketplace URL's own origin (same scheme, host and port)
  ([host-marketplace, "Authenticate archive downloads"](https://code.claude.com/docs/en/plugins/host-marketplace.md#authenticate-archive-downloads),
  read 2026-11-22). Prod's `public_url` is the Pomerium SSO host, which a bearer token cannot
  pass. The feed has no public prefix on that host (owner decision, eh-homelab/clusters#4017,
  closed; §9). It is LAN-only, as `/mcp` is, and off-LAN clients get no feed. The zip URLs and
  Settings' `marketplace_url` follow the request's origin, so a person on the LAN host gets
  LAN URLs.
- Readers are outside callers, not only the owner (owner, 2026-11-21: "outward calls").
- Auth is the `/mcp` auth, unchanged (owner, 2026-11-21: "same auth as mcp"; operating.md
  §4.1, §6a, §10):
  - The agent serves the feed, the zips and the mirrors, at
    `/api/v1/ai/plugin-feeds/{feed}/…`. Each request goes through the same
    `authenticate.ts` path as `/mcp`: `sbmcp_` bearer tokens from `ai_mcp_tokens`, OIDC
    access tokens when OIDC is on, and the `mcp_auth_mode` / `mcp_anonymous_cap` settings
    (in `disabled` mode, the anonymous caller reads the feed if the cap allows `read`).
  - A token of any tier (`read`, `write`, `outward`) reads the feed. A read uses the
    token as every MCP call does, including `last_used_at`, and revoking it stops reading
    at once.
  - The feed's settings sit in Settings → MCP access, beside the tokens and the auth mode
    (owner: "settings also"): which packages are in the feed, and a copyable setup snippet.
    In `bearer` and `oidc` mode the snippet is an `extraKnownMarketplaces` entry for
    `~/.claude/settings.json` with the literal `Authorization: Bearer sbmcp_…` in `headers`,
    then the `/plugin install` line. It has no `/plugin marketplace add` line, because that
    command sends no headers and would get 401. In `disabled` mode the snippet is the
    `/plugin marketplace add` line, then the `/plugin install` line (controller's ruling on
    PR #7042).
  - OIDC: Claude Code's marketplace fetch sends only static `headers` or a
    `headersHelper` (section 4), and it cannot run an OAuth sign-in. An OIDC user
    therefore needs a `headersHelper` that prints a current token, or an `sbmcp_` token.
    The setup snippet offers the `sbmcp_` form.
  - The agent reads the feed's data (packages, versions, zip and mirror bytes) over its
    CRUD routes, with its service credential, as it reads packages today. The API never
    serves the feed itself.
- A pending pin, a draft, or a package not in the feed answers 404 on the feed's paths.
- The feed's `Cache-Control` is short (60 s), so `/plugin marketplace update` sees a new
  version soon.

### 6.5 The assistant loads hosted plugins

- `ai_plugin_packages` gets a third `source_kind`, `hosted` (a new agent migration plus
  its `expected-migrations.json` entry). The pin is (version, `content_hash`).
  `commit_sha` is null for this kind (the CHECK changes to allow that).
- The lifecycle is the `store.ts` one, unchanged. The first publish installs the package
  unapproved and disabled. The admin approves the exact (version, `content_hash`) and
  enables it. Each later publish is a re-pin into `pending_*`. The current version keeps
  loading until the admin approves the pending one, after seeing the diff. "Plugins are
  never auto-updated" holds for hosted plugins too. The per-skill switches (#4757) work as
  for any package.
- Install and re-pin are the agent's existing installer (`PackageInstaller`), with the
  new source kind: reached from the UI through the existing UI-guarded routes, and from
  the `hosted_plugin_publish` tool in-process after its approval (6.2, 6.3). They store
  the pin and notify as they do today.
- Approval is the existing package approval, bound to the exact (version,
  `content_hash`). Whoever may approve a package today may approve a hosted one; this
  design does not change who that is. Edits made to the draft after a publish never
  reach that pin, because a version is immutable.
- Materialise: the agent fetches the zip over a new CRUD route,
  `GET /api/v1/internal/crud/agent/hosted-plugins/{name}/versions/{N}.zip`, with its
  service credential. It unzips into the cache folder, checks the file-list hash against
  `content_hash`, and runs `vetPackage`, as for git pins.

### 6.6 The installer accepts any URL Claude Code accepts

New `PackageSource` kinds, beside `git` and `marketplace` (git repo):

- `marketplace-url`: a bare HTTPS URL to a `marketplace.json`, plus an entry name. The
  file's sha256 goes into the review.
- `archive`: a zip URL (https only), directly or as a marketplace entry.
- `npm`: a package name and version from the public registry, or an https tarball link.
  Claude Code's refusals apply: no tarball links on github.com, gitlab.com, bitbucket.org
  or git.sr.ht.

Entry sources `github`, `url` and `git-subdir` keep working from every marketplace kind.
`command` stays refused.

Pinning non-git sources:

- At install, ScadBuddy fetches the bytes once through the egress check
  (`assertEndpointAllowed`), records their sha256 as the pin, and stores a copy in the
  blob store. A materialise reads that copy, not the upstream URL. If upstream deletes or
  changes the file, the approved pin still loads, and a re-pin shows the diff.
- `ai_plugin_packages` gets `pin_kind` (`commit`, `bytes` or `version`) and
  `bytes_sha256`.
- Unpacking: zip and tar only. Refuse symlinks, hard links, absolute paths, `..`, and
  device files. Use the caps from 6.2 for the unpacked tree. Cap the download at the
  unpacked total cap plus 1 MiB (21 MiB), not Claude Code's 256 MiB: an archive
  compresses its files, so one larger than that cannot unpack within the cap. It fails
  early, and the agent pod never holds 256 MiB (ruling on #6935's review). An archive
  that does not compress and is near 20 MiB then fails with the download's message, not
  the unpacking one.

### 6.7 Outside plugins in the feed

An outside plugin is an ordinary installed package (6.6), and it is listed in a feed
like a hosted one:

- `PUT` and `DELETE /api/v1/ai/plugin-feeds/{feed}/packages/{name}` (UI-guarded, as the package routes) add a package to the feed
  and remove it. The package is installed, reviewed, approved and re-pinned through the
  existing package routes. Nothing about the feed has a lifecycle of its own.
- The feed emits a git pin as its source with `sha` set to the pinned commit. A bytes pin
  (`archive`, `npm`) is emitted as an `archive` that ScadBuddy serves from its own mirrored
  copy (6.6), with the served zip's `sha256`. A zip pin is served as its exact approved
  bytes. An npm tarball is served as a deterministic zip of its approved files, because
  Claude Code's `archive` takes a zip only. The archive's URL names the served zip's
  `sha256`, so an immutable URL never serves other bytes.
- A package approved with `allow_refused` (hooks, MCP servers) can be listed. The Add to
  feed control says that every client of the feed will run them.

### 6.8 Security notes

- The feed and the zips use the `/mcp` auth (6.4): no anonymous read unless the owner set `/mcp` to allow it.
- Every client of a feed runs what it lists. Only approved pins are listed, and a
  revoked `sbmcp_` token stops reading at once.
- Hosted plugins cannot hold hooks, MCP servers or executables outside `skills/`. A skill
  may hold scripts (`executable` true). The assistant runs them only through its own
  tools, and the tool access settings (#2171) govern those.
- Nothing loads, and nothing is listed in a feed, without the existing package approval.
  Adding a package to a feed is a settings write, and it is outward like other settings
  writes.
- No new process gets SQL. The agent reaches all of this through CRUD routes with its
  service credential.

## 7. Tests

- Backend: path rules and caps; the publish builds the same zip bytes twice; the feed
  JSON matches the marketplace schema; an `sbmcp_` token of each tier reads the feed
  through the same auth path as `/mcp` (bearer, OIDC, `disabled` with each cap); nothing once it is
  revoked or expired; a version is immutable.
- Agent: `marketplaceTarget()` accepts `archive` and `npm` and still refuses `command`; a
  bytes pin loads from the mirror when upstream is gone; a zip with a symlink or `..` is
  refused; a publish creates an unapproved install, then a pending re-pin with a diff,
  and the old version loads until the new one is approved.
- E2E: in a scratch Claude Code, `/plugin marketplace add <feed URL>` with the token
  header, then `/plugin install <plugin>@<feed>`, lists the skill. Record the Claude Code
  version this was measured with.

## 8. Order of work (one issue and PR each)

1. Hosted plugin tables, draft file routes, publish and the deterministic zip.
2. The agent's feed and zip routes on the `/mcp` auth, and the feed section in Settings → MCP access.
3. The assistant loads hosted plugins (`source_kind` `hosted`, the CRUD zip route,
   publish as install or re-pin).
4. The installer takes `marketplace-url`, `archive` and `npm` with bytes pins and
   mirrors.
5. Outside packages in the feed (`ai_plugin_feed_packages`).
6. The UI editor.
7. The MCP tools.

Steps 4 and 5 do not depend on 1 to 3 and can run in parallel with them.

## 9. Left to the builder

- Front door (found 2026-11-22, evidence in the PR #7042 and issue #6933 comments): `/mcp` is
  reached only on the LAN HTTPRoute. On the Pomerium SSO host `/api/v1/ai` is behind the
  sign-in. Outcome: the feed gets no public prefix on the SSO host (owner decision on
  eh-homelab/clusters#4017, closed). It is LAN-only like `/mcp`, with the `sbmcp_` bearer
  token, and off-LAN clients get no feed. The zip URLs and Settings' `marketplace_url` follow
  the request's origin, not `public_url`. Claude Code sends `headers` only to the marketplace
  URL's origin
  ([authenticate archive downloads](https://code.claude.com/docs/en/plugins/host-marketplace.md#authenticate-archive-downloads)).
- Check whether `headers` values in `extraKnownMarketplaces` expand environment variables.
  Pick the snippet form (literal token, `headersHelper`, or env var) from what works.
- The exact Monaco and file-tree components.
