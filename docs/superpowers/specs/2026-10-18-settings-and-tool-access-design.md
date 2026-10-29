# Settings by area, and one access model for every tool

Date: 2026-10-18. Issue #2171. Supersedes the tighten-only rule of #1953 and changes
AI spec §8.1, §8.2 and §8.6 (`2026-09-27-ai-integration-design.md`, edited in the same
PR as this spec). Folds in #778, #781 and #2660.

## 1. Goal

Two problems, one piece of work:

1. **Settings is one page.** `/settings` is one route and one 1,789-line component
   (`frontend/src/pages/SettingsPage.tsx`) holding 14 sections. Its Assistant section
   alone holds about twenty unrelated settings, MCP auth, plugins and the AI audit log.
2. **What the assistant may use is configured four ways.** A built-in tool's tier can
   only be raised; a remote MCP plugin's can be anything; a plugin package's MCP servers
   are always `outward` with no per-tool control; a package's hooks, skills and subagents
   are on or off only with the whole package. Some capabilities have a switch of their
   own instead (HTTP request, printer camera), and what a call may do also depends on
   where it comes from (a subagent's outward call is refused, not parked).

Wanted:

- Settings split into sub-pages by area, each a list of self-contained section
  components, with a sub-nav and hash deep links.
- One rule for everything the assistant can use: **the user decides each item's access,
  in either direction; the declared tier is only the default; access belongs to the
  tool, never to where it is called from.**
- Friendly names and descriptions on every tool.

Out of scope: friendly names for Settings fields (#887, which needs only `FieldSpec`'s
existing `label` and `help`), OAuth for remote plugins (#780), the durable fork failure
(#4173).

## 2. Facts this rests on

Read on main at `90d8d3025`; paths relative to the repo root.

**Settings page**

- One route, `settings` (`frontend/src/App.tsx:107`). `SECTIONS`
  (`frontend/src/pages/settings/fields.ts:6-21`) lists 14 sections. The Assistant
  section has groups (`ASSISTANT_GROUPS`, `SettingsPage.tsx:258`): credentials, browser,
  tools, model, sessions, MCP, plugins, audit.
- Ten sections save through shared form state in `SettingsSections`
  (`SettingsPage.tsx:275-660`: draft, seed queue, dirty tracking, field errors,
  `saveSection` `:466`, `patchFor` `:425`, `PUT /settings`). Libraries, Remembered
  choices and every Assistant setting save on their own; Spool inventory and About are
  read-only.
- Cross-section coupling in that state, all of it:
  - the blob-store choice reads the saved Bambuddy URL and the `library_folder_id`
    draft (`:370-381`);
  - a Connection save or a field reset can force `store_backend: 'local'` (`:451`,
    `:524`);
  - a Connection save reloads the targets and projects that Printing, Projects and
    Remembered choices read (`:480`);
  - Test connection saves Connection first (`runTest`, `:634`);
  - the in-page agent's `settings` handlers (`:573-632`: `get_form`, `set_field`,
    `test_connection`) span fields in Connection, Projects, Printing and Preview.
- Folding and nav already exist and are id-driven: `collapseState.ts` (localStorage
  `scadbuddy.settings.collapsed`), `collapse.tsx` (`SettingsCollapseProvider` prunes
  unknown fold ids, #3415), `sectionNav.ts` (pane-only scrolling #2345, deep-link settle
  `SETTLE_MS`, no hash push while dirty #3247). `set_field` unfolds its section (#3837).
- `useLeaveGuard` (`frontend/src/lib/useLeaveGuard.ts:69`, `:83`) lets a navigation
  through only to the same pathname.
- Inbound links: `McpAuthBanner.tsx:43` (`/settings#assistant`),
  `SessionTouched.tsx:129` (`#libraries`), `ActivityDrawer.tsx:283` (`/settings#plugins`,
  an anchor that does not exist: broken today), `agent/chat/pageContext.ts:37,47`
  (matches `/settings` exactly), tool hints in `agent/src/tools/browser.ts:273,460-483`,
  text pointers ("Settings → Plugins") in `agent/src/tools/registry.ts:518`,
  `approvals.ts:71`, `routes/subagentMcp.ts:127`, `temporal/toolActivities.ts:434`, and
  docs (`plugins/scadbuddy/README.md:62`, `docs/ai/agent-sessions.md:258`,
  `docs/ai/browser-bridge.md:59`).
- `McpTokensSection` gets the auth mode only after `McpAuthSection` saves
  (`SettingsPage.tsx:293,1547-1553`), so until then it never has it.
- Tests: about 100 vitest page tests (most render `<SettingsPage/>` and expect every
  section) and 11 Playwright specs touch Settings.
- shadcn/Radix primitives exist in `frontend/src/components/ui/` (`sidebar`, `tabs`,
  `collapsible`, `accordion`); `ui-guard.test.ts` lints for them. The section nav is
  plain links styled as tabs on purpose (#3671): Radix Tabs would break hash links and
  pane scrolling.

**Tool access**

- Built-in tool sets: `agent/src/plugins/builtInTools.ts`. Only `scadbuddy` is live
  (`SOURCES`, `:51`); its overrides are `ai_settings` key `builtin_tools.scadbuddy`,
  tighten-only (`sanitise` `:70`, `validateOverrides` `:89`, 400 on a lowered tier
  `:104`). The headless Playwright browser and `builtin_tools.playwright` were removed
  (`agent/src/db/migrations/20261012T0454Z_drop_headless_browser.sql`); the browser tools
  now have origin allow and deny lists (`routes/browserOrigins.ts`, #3654).
- Switches in `ai_settings`: `http_request_enabled` (`harness/httpRequest.ts:79`),
  `printer_camera_enabled` (`tools/switches.ts:12`), `scadbuddy_plugin_enabled`
  (`plugins/packages/builtins.ts:16`, ScadBuddy's own skills and subagents). WebMCP is
  per browser, localStorage `scadbuddy.webmcp` (`frontend/src/agent/webmcpPreference.ts`).
- Remote MCP plugins: `ai_plugins.tool_tiers`, `disabled_tools`, `enabled`
  (`20260928T0735Z_plugins.sql`), any tier; unlisted tools are `outward`
  (`plugins/registry.ts:370-376`).
- Package MCP servers: built with `toolTiers: {}, disabledTools: []`
  (`plugins/packages/mcp.ts:125-133`), so every tool is `outward` in both runtimes
  (`temporal/turnServers.ts:351`). The forwarder already enforces both fields per
  plugin (`plugins/forwarder.ts:468-484`). Nothing lists a package server's tools
  outside a turn.
- Where access is decided:
  - TypeScript: `effectiveTool` (`tools/registry.ts:138`) and `runJudgedByResult`
    (`:504-534`, the order: unreadable override, disabled, principal tier, switch, the
    #2100 seam check, prepare/confirm); `builtInPolicy` (`builtInTools.ts:140`) and
    `harnessTierOf` (`harness/run.ts:324`, `:396-404`) for classic turns, read once per
    turn (`sessions/turnRunner.ts:289-291`); `turn_tool_tier`
    (`temporal/turnTools.ts:552-568`); `flow_tool_tier` and `/internal/flows/tools`
    (`temporal/flowTools.ts:29-59`); the `agent-tools` activities
    (`temporal/toolActivities.ts:411-453`); `/mcp` (`tools/projections.ts:160`,
    `refreshOfferedTools` `:230-247`); the subagent loopback
    (`routes/subagentMcp.ts:107-134`).
  - Python: `gate_settings` reads `builtin_tools.scadbuddy`
    (`agent-durable/.../session/activities.py:75-93`); `sanitise_overrides` and
    `apply_overrides` port the TypeScript rules (`session/tools.py:130,160`), applied
    once per turn (`session/workflow.py:356-367`).
  - Backend flows ask the agent (`backend/scadbuddy/flows/agent_tools.py:40-65`,
    `workflows/flow_tools.py:743-839`; `_live_tier`, `:807`, can only raise).
- The camera switch is checked only in `runJudgedByResult` (`:525`), not in
  `effectiveTool`, so flows, `/internal/flows/tools` and the durable gate never see it.
- Nothing caches access: every gate reads `ai_settings` per call or once per turn.
  `SettingsStore.set` NOTIFYs only to evict warm processes (`credentials.ts:643-654`,
  `sessions/warmEvict.ts:28-35`). #2100 (PR #2308) and #2088 (PR #2311) are pinned by
  `agent/test/builtInTools.test.ts:279,410,421`, `toolActivities*.test.ts` and
  `agent-durable/tests/test_tool_overrides.py`, `test_session_workflow.py:943-1046`.
- Where a call comes from changes what it may do:
  - a subagent's outward call is refused, not parked (`routes/subagentMcp.ts:130-133`,
    `ASK_THE_MAIN_AGENT`);
  - durable subagents get only ScadBuddy's own tools, not plugins or `http_request`
    (#2660, open);
  - flow runs get no browser or answer tools.
- Package contents reach a turn as a derived copy already: classic swaps each package
  for a `withoutRemoteServers` copy (`turnRunner.ts:307-330`, `packages/mcp.ts:196-230`),
  durable shares it under `<payload>/plugins/<derived hash>/` (`turnPackages.ts:54-81`),
  and both runtimes check a copy against the hash of that copy
  (`mcp.ts:216-220`, `agent-durable/.../session/runner.py:661`); the pin hash is checked
  on the untouched cache (`install.ts:311`). The classic copy's folder is named by the
  pin hash (`mcp.ts:198`) and replaced when contents differ (`:220-221`), so two
  variants of one pin would overwrite each other.
- Disabling one hook, skill or subagent:
  - no SDK option drops a single plugin hook; ours only add callbacks (`run.ts:425-441`);
  - hooks have no stable id (`ReviewHook` is `{event,type,url?}`, `vet.ts:69`);
  - in classic turns Skill and Agent reach only the PreToolUse hook (`ownPlugin.ts:21-23`),
    which could read `input.skill` / `input.subagent_type`;
  - in durable turns nothing reads a tool call's input: the Temporal plugin's only gate
    is a PreToolUse hook whose `decide` reads `tool_name` and the call id, never
    `tool_input` (`_defer_hook.py:174-221`, at the pin in `agent-durable/pyproject.toml`),
    and Skill/Agent take its "runs normally" branch. The segment runner's own
    PreToolUse hook (`SegmentCalls`, `runner.py`, added by #4184 after this was
    written) does read `tool_input`, and the plugin keeps its decision for every tool
    but the durable prefix and `tool_activities` (`_guard_hooks`, `_runner.py` at the
    `uv.lock` pin), so a deny there stands for Skill, Agent and Task.
- `allow_refused` packages' built-ins (`Bash`, `WebFetch`, …) are one set per turn,
  merged across packages (`install.ts:489,504`, `turnServers.ts:309`), each `outward`.
- No tool has a static friendly name: `defineTool` (`tools/registry.ts:216-319`) has a
  per-call `title` function and hint `annotations` only. Remote listings keep
  `description` and `annotations` only during Test connection
  (`plugins/testConnection.ts:28-100`) and store nothing. A package review records
  skills and agents as `"<pkg>:<name>"` strings with no description (`vet.ts:73-97`),
  though vetting already parses frontmatter (`:298-306`).

## 3. Settings by area

### 3.1 Routes and navigation

- `/settings` redirects to `/settings/general`; each area is `/settings/:area`.
- Inside an area, a sticky sub-nav lists its sections as hash links:
  `/settings/library#fonts` opens Library, unfolds Fonts and scrolls to it. It reuses
  `sectionNav.ts` (pane scrolling, settle, `aria-current`) and `collapseState.ts`
  unchanged.
- Old links keep working: `/settings#<section>` redirects to the area that holds that
  section, with the same hash. An id no area holds lands on General (as #2113 made the
  one-page version do).
- The area nav is the shell's left column on desktop (shadcn `sidebar` sub-menu) and a
  scrolling strip of links on mobile. An area with unsaved edits shows a dot.

### 3.2 Areas

| Area (`:area`) | Sections |
|---|---|
| General (`general`) | Connection |
| Printing (`printing`) | Printing defaults, Spool inventory, Remembered choices, Hotend usage |
| Library (`library`) | Projects & files, Uploads, Fonts, Libraries, Rendering, Preview |
| Assistant (`assistant`) | Status & credentials, Model, Session limits, Runtime, Render limits, Images, Workspace, Session mode, Durable subagents, Memory |
| Tools & plugins (`tools`) | Built-in, Plugin packages, Remote MCP plugins (§4.6) |
| MCP access (`mcp`) | Auth mode, Tokens, OIDC sign-in, Connect snippet |
| Audits (`audits`) | AI activity, with a link to `/activity` for background jobs |
| System (`system`) | Diagnostics, Administration, About |

Assistant, Tools & plugins, MCP access and Audits are served by the agent. When
`useAiAvailability` says it is down, the area shows one notice instead of the scattered
`ai.available` checks.

### 3.3 One component per section

- Each section is `frontend/src/pages/settings/sections/<id>.tsx` exporting
  `{ id, area, title, order, Component }`. The shell finds them with
  `import.meta.glob`, as the msw feature mocks are found (#508); adding a section edits
  no list. `FOLD_IDS` and the redirect table are built from the same registry, so the
  fold prune knows every area's ids.
- A section owns its loading, saving, discarding and errors, and may lay itself out as
  it needs. The shell holds no form state and fetches nothing.
- Shared pieces:
  - `Section` (`controls.tsx`), already foldable, unchanged.
  - `useBackendSettings(fields)`: for sections that save through `PUT /settings`. It
    owns that section's draft, seeding, dirty state, field errors, reset to the
    environment and Save/Discard. It reads one `GET /settings` cache shared by every
    section (a React context in the shell), so N sections make one request.
  - A dirty registry in the shell: each section reports dirty or clean; the area dots,
    the leave guard and its dialog read it.
  - A `bambuddyResources` context for targets and projects, refreshed after a
    Connection save, which Printing, Library and Remembered choices read.
- Cross-section rules move into the hook as explicit dependencies: Projects & files'
  blob-store choice depends on the saved Bambuddy URL (from the shared cache) and its
  own `library_folder_id` draft; Connection's save may also send
  `store_backend: 'local'`, as today.
- The in-page agent's `settings` scope (`get_form`, `set_field`, `test_connection`)
  moves to the shell and dispatches to the section that owns a field; `set_field` on a
  field in another area navigates there and unfolds it. Saving stays the user's.

### 3.4 Leaving and switching

`useLeaveGuard` lets any navigation inside `/settings/*` through; drafts live in the
section components' state under one shell that stays mounted across areas, so moving
between areas keeps every edit. The guard asks only when leaving Settings with edits
anywhere. Back keeps the #3247 behaviour inside an area.
Every area stays mounted on purpose (`SettingsShell.tsx` hides the others), so drafts
survive moving between areas, and every section loads on the first open of Settings; do
not unmount hidden areas.

### 3.5 Small fixes on the way

- `ActivityDrawer`'s link becomes `/settings/tools#…` for the plugin it names.
- `pageContext.ts` matches `/settings/*`, so sub-pages keep their suggested prompts.
- `McpTokensSection` reads the auth mode itself.
- Text pointers say the new names: "Settings → Tools & plugins", "Settings → MCP
  access".

## 4. One access model

### 4.1 Rule

Every item the assistant can use has an access the user sets:

- a **tool**: `off`, `read`, `write` or `outward`;
- a **hook, skill or subagent**: `off` or `on`;
- a **source** (a tool set, plugin or package) as a whole: enabled or not.

The declared tier is the default, never a floor. `read` and `write` run without asking;
`outward` waits for a person's approval (AI spec §8.2). Setting an item below its
default asks for a one-line confirmation in the UI. Changing access is a settings write
the user makes; the agent cannot change its own access.

**Access belongs to the tool, not to the caller's location.** The same item has the same
effective access from the main agent, a subagent, a classic or durable turn, a flow run
and `/mcp`. A principal's tier cap (an MCP token's tiers, AI spec §8.1) still applies on
top, as today.

Only what is technically forced stays fixed, with the reason shown: a tool Claude Code
renames cannot take a tier and stays `outward` (`harnessToolName`), and tools colliding
on one name are hidden.

### 4.2 Sources and items

| Source id | What | Items |
|---|---|---|
| `builtin:scadbuddy` | every registry tool (`ALL_TOOLS`) | tools; default = its `risk` |
| `builtin:browser` | the browser tools; its card holds the origin allow and deny lists | tools |
| `builtin:http` | `http_request` | one tool |
| `builtin:own-plugin` | ScadBuddy's own skills and subagents | skills, subagents |
| `webmcp` | this browser's built-in agent (WebMCP) | none; enabled only, kept in localStorage, labelled "this browser only" |
| `remote:<name>` | a remote MCP plugin | tools; default `outward`, the server's `readOnlyHint` shown as a suggestion |
| `package:<name>` | a plugin package | hooks (`hook:<event>:<id>`, below), skills, subagents, and its `allow_refused` built-ins (`builtin:<name>`) |
| `package:<name>/mcp:<server>` | an MCP server a package adds | tools; defaults as a remote plugin |

The printer camera becomes the access of `get_printer_camera` in `builtin:scadbuddy`
(`off` replaces the switch), so every gate sees it.

A package's `allow_refused` built-ins are one set per turn (§2), so their access applies
per tool name: when two loaded packages set `Bash` differently, the stricter wins.

Hooks have no stable id, so a hook's item id is a fingerprint of its content:
`hook:<event>:<sha256 of its canonical JSON, first 16 hex>` (#4209). An unchanged hook keeps
its override across a re-pin. A hook that changed or is new comes back at its default
(`on`), and the package card marks it "new since you last reviewed"; a re-pin is approved
by a person anyway (#297), so that is where it is seen. Overrides naming a fingerprint the
new pin no longer has are deleted with the re-pin.

### 4.3 Storage (agent migrations)

- `ai_access(source text, item text, access text, updated_at, updated_by,
  primary key (source, item))`. Only overrides are stored; `item = '*'` holds a source's
  `enabled`. `access` is checked against the item kind on write.
- `ai_tool_listings(source text primary key, tools jsonb, listed_at)`: the last
  `tools/list` of a remote plugin or package MCP server, keeping each tool's `name`,
  `title`, `description` and `annotations`, so the page can show tools without
  connecting.
- The migration copies `builtin_tools.scadbuddy`, `ai_plugins.tool_tiers` /
  `disabled_tools` / `enabled`, `http_request_enabled`, `printer_camera_enabled` and
  `scadbuddy_plugin_enabled` into `ai_access`. Nothing reads the old keys and columns
  after it; a later migration drops them.

### 4.4 Enforcement

- One `AccessResolver` (`agent/src/access/`) answers an item's effective access. It
  replaces, behind the interfaces they have now, `effectiveTool`, `builtInPolicy`,
  `pluginTierResolver`, `switches.ts` and the per-package empty `toolTiers` /
  `disabledTools`. Every gate in §2's list asks it. No new cache: reads stay per call,
  or per turn where they are per turn now, so #2088 and #2100 keep their fixes and
  their tests stay as acceptance tests.
- Python: `gate_settings` returns the resolved access for the turn's tools from
  `ai_access`, and `sanitise_overrides` / `apply_overrides` read that shape. Both
  languages share vectors (`agent/test/fixtures/access-vectors.json`, written by the
  TypeScript test, opened by `agent-durable/tests`), as the pending-input and payload
  vectors do.
- A change applies from the next call, in either direction, including to a turn or a
  flow run already under way (#4209): a lowered tier stops parking on that run's next
  call; a raised one starts.
- Backend flows: `_live_tier` (`workflows/flow_tools.py:807`) takes the tier it is given
  in either direction, behind `workflow.patched("flow-access-2171")` (the projects
  worker replays `tests/fixtures/project_workflow_histories/`).
- `off` tools are left out of every list the model or `/mcp` sees, and refused if called.
- Hooks, skills and subagents set `off` are left out of a per-turn filtered copy of the
  package, built beside `withoutRemoteServers` in both runtimes. The copy is named by
  its own derived hash (fixing the overwrite in `mcp.ts:198-221`), hashed, and sent with
  that hash, so both runtimes' existing checks hold it to "derived only from a
  pin-verified source". In classic turns the PreToolUse seam also refuses a Skill or
  Agent call naming a disabled one, as a backstop; in durable turns the segment
  runner's own PreToolUse hook does the same per call (§2), so a skill or subagent
  switched off mid-turn is refused from its next call in both runtimes. Hooks
  themselves change per turn in both: no SDK option drops one plugin hook from a
  running query (§2).
- Package MCP servers get their `toolTiers` / `disabledTools` from the resolver; the
  forwarder already enforces them. Their stdio servers (`allow_refused`) take the tier
  through `tierOf` (`turnPackages.ts:196`).

### 4.5 Every tool from every caller

- **Subagents park like the main agent.** The subagent loopback
  (`routes/subagentMcp.ts`) stops refusing outward calls: it opens the same approval
  (`approvals/service.ts`, the session's `ai_approvals` row, the badge and panel) and
  waits inside the MCP call until a person decides, the way a classic call waits inside
  `canUseTool`. The wait is bounded by the shorter of the approval's own expiry
  (`approval_expiry_seconds`) and the rest of the turn; the plan measures how long a
  durable segment and Claude Code's MCP client let a subagent's tool call wait, and caps
  the bound below that. A deny returns the main agent's denied result; an approval that
  expires, or a turn that ends first, returns the same "nobody decided; the approval
  expired" result the main agent gets, and the approval is voided (#4209).
- **Durable subagents get every configured tool** (#2660): the loopback serves the turn's
  remote plugin and package servers (through the forwarder) and `http_request` beside
  ScadBuddy's own, each resolved as in §4.4.
- **Flows**: a flow's `tool(name, args)` already takes the agent's tools and turn tools
  (#2320); with §4.4 their access is the same item's. Browser tools stay out of flows
  only because a flow has no tab, not by access.

### 4.6 API (agent)

- `GET /api/v1/ai/capabilities`: the sources and items as a tree, each item with `id`,
  `kind`, `name`, `harness_name`, `label`, `description`, `default`, `access`, and
  `fixed` (with its reason) where forced. A source has `enabled`, `switchable`, its
  options' route where it has one (browser origins), and `listed_at`
  for a listed source.
- `PUT /api/v1/ai/capabilities/{source}` `{enabled}`;
  `PUT /api/v1/ai/capabilities/{source}/items/{item}` `{access}`;
  `DELETE` on the item resets it to its default;
  `POST /api/v1/ai/capabilities/{source}/refresh` lists a remote or package server's
  tools again (reusing `testPlugin`) and stores them in `ai_tool_listings`.
- All writes are user-only settings writes (`routes/guard.ts`), audited as now, and
  NOTIFY warm eviction as `builtin_tools.*` does.
- Plugin add, remove, re-pin, URL and credential routes stay where they are. The old
  `PATCH /plugins/{name}` tier fields and the http-request and printer-camera switch
  routes answer from `ai_access` for one release, then go.

### 4.7 Names

- `defineTool` gains a required `label` (for example "Send to Bambuddy"), served as the
  MCP tool `title`; `test/coverage.test.ts` fails on a tool without one.
- Remote and package tools use the stored `title`, else `annotations.title`, else the
  name; `description` as given.
- Skills and subagents take `description` from their frontmatter, which vetting adds to
  the package review.
- Every row shows the label, the description, and the raw name small and de-emphasised.

### 4.8 The Tools & plugins page

- Three sections: Built-in, Plugin packages, Remote MCP plugins.
- Each source is a foldable card with its enable switch, a summary ("115 tools,
  3 changed") and, for tools, a filter box. A package card nests its MCP servers, hooks,
  skills, subagents and built-ins.
- One shared row component for every item: label, description, raw name, an access
  select (`off`/`read`/`write`/`outward`, or `off`/`on`), the default marked, "Reset".
  Lowering below the default opens the confirmation.
- Remote cards keep Test connection; its result panel folds to its status line (as
  #3048 made it).

## 5. Delivery

A stack of PRs, bottom-up, each with its issue:

1. **docs**: this spec, and the AI spec §8.1, §8.2, §8.6 edits.
2. **frontend**: the area shell, section registry, `useBackendSettings`, the dirty
   registry and leave rule, redirects, and every section moved into its own component
   as it is; the tests moved to areas. No behaviour change beyond §3.5.
3. **agent + agent-durable + backend flows**: `ai_access` / `ai_tool_listings` with the
   copying migration, `AccessResolver` behind today's interfaces in both runtimes, the
   shared vectors, the camera fold-in, `_live_tier` in either direction. No UI change.
4. **agent + frontend**: the capabilities API, the Tools & plugins page, lowering with
   confirmation, `label` on every tool. Closes #2171 and #778.
5. **agent**: package items: the derived-hash copy fix, the filtered copy, package MCP
   listing and access, hooks/skills/subagents on and off.
6. **agent + agent-durable**: every tool from every caller (§4.5). Closes #2660.
7. **frontend**: the MCP access and Audits areas filled, the old routes retired.
   Closes #781.

## 6. Tests

- Frontend: each section component on its own; the shell's routing, redirects
  (`/settings#fonts`, an unknown hash), sub-nav, dirty dots, and the leave rule (area to
  area keeps drafts; leaving Settings asks); the agent's `set_field` across areas; the
  Tools & plugins rows and the lowering confirmation; msw mocks under
  `src/mocks/features/`. Playwright specs move to the area URLs.
- Agent: the migration copies every old key; the resolver for each source kind, lowered
  and raised; `off` absent from the harness, `/mcp` and durable tool lists; lowering a
  built-in accepted (replacing #1953's "loosen 400"); the #2088 and #2100 tests
  unchanged and passing; the camera off refused by flows and the durable gate; a
  filtered package copy omits a disabled skill, subagent and hook in both runtimes, and
  two variants of one pin coexist; a subagent's outward call parks, runs once on
  approval, gets the denied result on deny and the expired result on expiry or turn end;
  a hook override survives a re-pin that leaves the hook unchanged and resets for a
  changed hook; a tier lowered mid-turn stops parking on the next call.
- agent-durable: the shared access vectors; `gate_settings` with lowered and raised
  items; a durable subagent sees a remote plugin's tools.
- Backend: a flow run takes a lowered tier, including one lowered while the run is under
  way (its next call), with replay of the existing histories.
