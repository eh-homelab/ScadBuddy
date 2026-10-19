# Settings by area Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the one-page Settings (`frontend/src/pages/SettingsPage.tsx`, 1,789 lines) into `/settings/:area` sub-pages built from one self-contained component per section, with no behaviour change beyond spec §3.5.

**Architecture:** A shell (`SettingsShell`) under a set of providers (fold state, a dirty registry, one shared `GET /settings` cache, the Bambuddy targets/projects/status, the in-page agent's `settings` scope) renders every area at once and hides the inactive ones, so drafts kept in each section's own state survive area switches. Sections are files under `pages/settings/sections/` found by `import.meta.glob`; the ones that save through `PUT /settings` use `useBackendSettings`, which owns that section's draft, seed queue (#767), dirty state, errors, save, discard and reset. The new code is built beside the old page and routed only in Task 13, so the app and the old tests keep working until then.

**Tech Stack:** React 19, react-router 8, Vite 8 (`import.meta.glob`), vitest 5 + Testing Library + msw, Playwright, shadcn/Radix primitives in `src/components/ui`.

**Spec:** `docs/superpowers/specs/2026-10-18-settings-and-tool-access-design.md` (§2 "Settings page", §3, §5 item 2, §6 "Frontend"). Issue #2171.

## Global Constraints

- Node 24; pnpm via corepack (`cd frontend && corepack enable && pnpm install --frozen-lockfile`).
- The full check, run before every commit that touches more than one test file: `cd frontend && pnpm lint && pnpm typecheck && pnpm test && pnpm build`.
- Playwright: `cd frontend && pnpm exec playwright test` (msw-mocked, against `pnpm preview`).
- Run `cd frontend && pnpm gen:api` once before running single test files with `pnpm exec vitest run <file>` (it needs uv and the backend tree; `src/api/schema.d.ts` is gitignored and never committed).
- vitest under load can time out; re-run a timed-out test on an idle machine before debugging it.
- msw mocks go under `src/mocks/features/` (auto-discovered, #508); this plan adds none and edits none.
- Conventional-commit titles (`feat(frontend):`, `refactor(frontend):`, `test(frontend):`, `fix(frontend):`, `docs:`); each commit message ends with the session's attribution trailer.
- No new files beyond those the tasks name. No edits outside `frontend/` except Task 15's listed agent strings and docs.
- Section nav and area nav are plain `<a>`/`<Link>` links styled as tabs (#3671), never Radix Tabs.
- No section id may equal an element id used anywhere on the page, and no section title may equal a heading inside it (two regions with one name break `getByRole` and Playwright's strict mode). This is why some titles below differ from the spec table (see Task 11, 12).
- Areas and order (spec §3.2): `general`, `printing`, `library`, `assistant`, `tools`, `mcp`, `audits`, `system`. `/settings` → `/settings/general`; `/settings#<section>` → `/settings/<its area>#<section>`; an id no area holds → General.
- Saving stays the user's: no agent handler saves (AI spec §8.1).

## Review Focus

1. A deep link to a folded section in another area (`/settings#fonts` with `fonts` folded, typed or from an old bookmark): expected to land on `/settings/library#fonts` with Fonts unfolded and scrolled to. Test: Task 7 `opens the area that holds a /settings#<section> link, unfolded`.
2. A dirty draft through area switches and redirects: edit in General, click Library, follow an old `/settings#<id>` link: the draft survives every step, General's area link shows the unsaved dot, no dialog appears until a link leaves `/settings`. Tests: Task 7 `keeps a draft across area switches, dots the area, and asks only when leaving Settings` and `keeps drafts through a redirect`.
3. The agent's `set_field` on a field in another area (`display_unit` while on General): expected to navigate to `/settings/library`, unfold Preview, set the draft, keep General's draft, and report `unsaved: true` in `get_form`. Tests: Task 5 `set_field in another area navigates there, unfolds it and keeps other drafts`; Task 13 `settings tools` additions.
4. An old bookmark naming a removed anchor (`/settings#assistant-plugins`, or `#assistant-model`, which is also `ModelSetting`'s input id): expected to land on the section that now holds it (`/settings/tools#packages`, `/settings/assistant#model`), never on the input. Tests: Task 2 `maps every old anchor to its new section`; Task 7 `sends an old anchor to its new section`.
5. Settings changed elsewhere (`useSubscription('settings')`) while one section in another area is dirty: expected the banner, no section reseeded, the dirty draft kept, and "Load the latest" reseeding every section. Test: Task 3 `says changed elsewhere while any section is dirty, and Load the latest seeds every seeder`.

---

## File map

New (all under `frontend/src/pages/settings/`):

| File | Responsibility |
|---|---|
| `values.ts` | Pure field logic moved out of `SettingsPage.tsx`: secrets, hand-laid fields, `fieldsOf`, `baseline`, `toPatchValue`, `storeChoice`, `ofLimit`, constants. |
| `registry.ts` | `AreaId`, `AREAS`, `SettingsSection`, `loadSections`, `SECTION_REGISTRY`, `foldIdsOf`, `LEGACY_ANCHORS`, `settingsTarget`, `resolveLocation`. |
| `dirty.tsx` | The dirty registry: which sections are dirty, and the drafts a section publishes for another (`usePeerDraft`). |
| `cache.tsx` | One `GET /settings` per page, seeders, own writes, the changed-elsewhere state. |
| `bambuddyResources.tsx` | Targets, projects and Bambuddy status, fetched once for the page. |
| `useBackendSettings.ts` | One `PUT /settings` section's form state. |
| `formParts.tsx` | `SavedSection`, `FieldBadge`, `RuntimeRows`, `Advanced`. |
| `agentFields.tsx` | The in-page agent's `settings` scope, dispatching to the section that owns a field. |
| `SettingsShell.tsx` | `SettingsProviders`, `SettingsShell` (area nav, sub-nav, redirects, keep-alive areas, empty-section hiding, banners, leave dialog). |
| `testing.tsx` | Test helpers: `renderSettingsSection`, `renderSettingsShell`, `region`, `enter`. |
| `sections/<id>.tsx` | One per section (Tasks 8-12), each with `sections/<id>.test.tsx` where tests move. |

Modified: `pages/SettingsPage.tsx` (shrinks to the providers plus the shell in Task 13), `pages/settings/fields.ts`, `pages/settings/collapse.tsx`, `lib/useLeaveGuard.ts`, `App.tsx`, the components in Task 12 and Task 15, tests and e2e specs named per task.

---

### Task 1: Pure field logic in `values.ts`

**Files:**
- Create: `frontend/src/pages/settings/values.ts`
- Create: `frontend/src/pages/settings/values.test.ts`
- Modify: `frontend/src/pages/SettingsPage.tsx:83-245` (delete the moved code, import it), `:366-381` (use `storeChoice`)

**Interfaces:**
- Consumes: `FieldName`, `FieldSpec`, `SectionId`, `RUNTIME_FIELDS`, `envVar`, `serverValue` from `fields.ts`; `Settings` from `api/types`.
- Produces:
  - `SECRETS: readonly ['bambuddy_api_key', 'bambuddy_render_api_key', 'google_fonts_api_key', 'store_s3_secret_access_key']`, `type Secret`, `isSecret(name: FieldName): name is Secret`
  - `HAND_LAID: Partial<Record<SectionId, readonly FieldName[]>>` (today's `:103-124`), `SPECS: Record<string, FieldSpec>`
  - `extraSpecs(settings: Settings | undefined): FieldSpec[]`, `fieldsOf(section: SectionId, settings: Settings | undefined): FieldName[]`
  - `DAY_SECONDS = 86400`, `RETENTIONS: readonly FieldName[]`, `RETENTION_MAX_DAYS = 3650`, `ACTIVITY_TTL`, `ACTIVITY_REPAIR`, `FOLLOW_NOBODY = 'none'` (today's values)
  - `baseline(settings: Settings, name: FieldName): string`, `type Problem = { problem: string }`, `toPatchValue(name: FieldName, raw: string, settings: Settings): unknown`, `isProblem(value: unknown): value is Problem`
  - `ofLimit(used: string, limit: number, format: (n: number) => string): string`
  - `type StoreChoice = { backend: 'local' | 'bambuddy' | 's3'; savedBambuddyUrl: boolean; bambuddyReady: boolean; s3Ready: boolean }`
  - `storeChoice(settings: Settings, value: (name: FieldName) => string): StoreChoice` (the computation at `:370-381`)

- [ ] **Step 1: Write the failing tests** in `values.test.ts`, building `settings` from `mockSettings` (`src/mocks/handlers`):

```ts
it('turns retention days into seconds, empty into null, and refuses under a day', () => {
  expect(toPatchValue('print_run_retention_seconds', '2', settings)).toBe(172800)
  expect(toPatchValue('print_run_retention_seconds', '', settings)).toBeNull()
  expect(toPatchValue('print_run_retention_seconds', '0.5', settings)).toEqual({
    problem: 'Enter 1 to 3650 days, or leave it empty to keep every one.',
  })
})
it('reads an empty follow list as "none" and a missing one as empty', () => {
  expect(baseline({ ...settings, activity_follow: [] }, 'activity_follow')).toBe('none')
  expect(baseline({ ...settings, activity_follow: null }, 'activity_follow')).toBe('')
})
it('lists a section’s hand-laid fields first, then its runtime ones', () => {
  expect(fieldsOf('connection', settings)).toEqual(['bambuddy_url', 'bambuddy_web_urls', 'bambuddy_api_key', 'bambuddy_render_api_key', 'public_url'])
  expect(fieldsOf('fonts', settings)).toEqual(['google_fonts_api_key', 'fonts_catalogue_ttl'])
})
it('puts an env-seeded field with no control in Diagnostics (#322)', () => {
  const extra = { ...settings, sources: { ...settings.sources, made_up_knob: 'env' } } as Settings
  expect(fieldsOf('diagnostics', extra)).toContain('made_up_knob')
})
it('chooses the Bambuddy store only with a saved URL and an inbox in the draft', () => {
  const saved = { ...settings, bambuddy_url: 'https://b.test', library_folder_id: 3 }
  expect(storeChoice(saved, (n) => (n === 'store_backend' ? 'bambuddy' : n === 'library_folder_id' ? '3' : '')).backend).toBe('bambuddy')
  expect(storeChoice(saved, (n) => (n === 'store_backend' ? 'bambuddy' : '')).backend).toBe('local')
  expect(storeChoice({ ...saved, bambuddy_url: null }, (n) => (n === 'store_backend' ? 'bambuddy' : '3')).savedBambuddyUrl).toBe(false)
})
it('says a limit of 0 is none', () => {
  expect(ofLimit('3', 0, String)).toBe('3 (no limit)')
  expect(ofLimit('3', 10, String)).toBe('3 of 10')
})
```

- [ ] **Step 2: Run** `cd frontend && pnpm exec vitest run src/pages/settings/values.test.ts`. Expected: FAIL, `Failed to resolve import "./values"`.

- [ ] **Step 3: Move** `SettingsPage.tsx:83-245` (minus `SettingsPage`) into `values.ts` with the signatures above, exporting each; add `storeChoice` from `:370-381`. In `SettingsPage.tsx` import them and compute `const store = storeChoice(settings, value)` where `chosenBackend`, `bambuddyStoreReady`, `s3Ready`, `savedBambuddyUrl` were. No other change.

- [ ] **Step 4: Run** the new test, then `pnpm exec vitest run src/pages/SettingsPage` . Expected: PASS for both (the old page is unchanged in behaviour).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/settings/values.ts frontend/src/pages/settings/values.test.ts frontend/src/pages/SettingsPage.tsx
git commit -m "refactor(frontend): move Settings' field logic into settings/values.ts"
```

---

### Task 2: The section registry and the address rules

**Files:**
- Create: `frontend/src/pages/settings/registry.ts`
- Create: `frontend/src/pages/settings/registry.test.ts`
- Create: `frontend/src/pages/settings/sections/` (empty until Task 8; the glob tolerates no files)

**Interfaces:**
- Consumes: `sectionId(hash)` from `sectionNav.ts`.
- Produces:
  - `type AreaId = 'general' | 'printing' | 'library' | 'assistant' | 'tools' | 'mcp' | 'audits' | 'system'`
  - `AREAS: readonly { id: AreaId; title: string; servedByAgent: boolean }[]`, in that order, titles `General`, `Printing`, `Library`, `Assistant`, `Tools & plugins`, `MCP access`, `Audits`, `System`; `servedByAgent` true for `assistant`, `tools`, `mcp`, `audits`.
  - `isAreaId(value: string | undefined): value is AreaId`
  - `type SettingsSection = { id: string; area: AreaId; title: string; order: number; Component: ComponentType; hideWhenEmpty?: boolean }` (a module exports each as a named export: `export const id`, `area`, `title`, `order`, optional `hideWhenEmpty`, `export function Component`)
  - `loadSections(modules: Record<string, unknown>): SettingsSection[]`: checks each module, throws `settings section <path> must export id, area, title, order and Component` or `duplicate settings section id "<id>"`, sorts by area order then `order`.
  - `SECTION_REGISTRY: readonly SettingsSection[] = loadSections(import.meta.glob(['./sections/*.tsx', '!./sections/*.test.tsx'], { eager: true }))`
  - `foldIdsOf(sections: readonly SettingsSection[]): string[]`, `FOLD_IDS = foldIdsOf(SECTION_REGISTRY)`
  - `LEGACY_ANCHORS: Readonly<Record<string, { area: AreaId; section?: string }>>` = `assistant` → assistant/`assistant-credentials`; `assistant-browser`, `assistant-tools` → tools/`builtin`; `assistant-model` → assistant/`model`; `assistant-sessions` → assistant/`default-session-mode`; `assistant-mcp` → mcp/`mcp-auth-mode`; `assistant-plugins`, `plugins` → tools/`packages`; `assistant-audit` → audits/`audit-log`.
  - `type SettingsLocation = { area: AreaId; hash: string }` (`hash` is `''` or `'#…'`)
  - `settingsTarget(hash: string, sections?: readonly SettingsSection[]): SettingsLocation | null`: a legacy anchor first, then a section id; never the DOM.
  - `resolveLocation(pathname: string, hash: string, sections?: readonly SettingsSection[]): SettingsLocation`: `settingsTarget(hash)` when it names something, else the area in the path (`/settings/<area>`) with the hash kept, else `general` with the hash kept.

- [ ] **Step 1: Write the failing tests** with a fake module map (`./sections/connection.tsx` → general/10, `./sections/fonts.tsx` → library/30, `./sections/packages.tsx` → tools/20, `./sections/model.tsx` → assistant/20):

```ts
it('names the file that forgets an export', () => {
  expect(() => loadSections({ './sections/x.tsx': { id: 'x', area: 'general', title: 'X', order: 1 } }))
    .toThrow('settings section ./sections/x.tsx must export id, area, title, order and Component')
})
it('refuses an area it does not know', () => { /* area: 'nowhere' → same message */ })
it('refuses two sections with one id', () => { expect(...).toThrow('duplicate settings section id "fonts"') })
it('orders by area, then by order', () => { expect(sections.map((s) => s.id)).toEqual(['connection', 'fonts', 'model', 'packages']) })
it('opens the area that holds a section', () => {
  expect(settingsTarget('#fonts', sections)).toEqual({ area: 'library', hash: '#fonts' })
})
it('maps every old anchor to its new section', () => {
  expect(settingsTarget('#assistant-plugins', sections)).toEqual({ area: 'tools', hash: '#packages' })
  expect(settingsTarget('#assistant-model', sections)).toEqual({ area: 'assistant', hash: '#model' })
  expect(settingsTarget('#assistant', sections)).toEqual({ area: 'assistant', hash: '#assistant-credentials' })
})
it('knows nothing of an id no area holds, or a malformed escape (#2113)', () => {
  expect(settingsTarget('#nope', sections)).toBeNull()
  expect(settingsTarget('#%E0%A4%A', sections)).toBeNull()
})
it('resolves the address', () => {
  expect(resolveLocation('/settings', '', sections)).toEqual({ area: 'general', hash: '' })
  expect(resolveLocation('/settings', '#fonts', sections)).toEqual({ area: 'library', hash: '#fonts' })
  expect(resolveLocation('/settings/library', '#connection', sections)).toEqual({ area: 'general', hash: '#connection' })
  expect(resolveLocation('/settings/bogus', '', sections)).toEqual({ area: 'general', hash: '' })
  expect(resolveLocation('/settings/tools', '#nope', sections)).toEqual({ area: 'tools', hash: '#nope' })
  expect(resolveLocation('/settings/', '', sections)).toEqual({ area: 'general', hash: '' })
})
it('folds every section it holds', () => { expect(foldIdsOf(sections)).toEqual(['connection', 'fonts', 'model', 'packages']) })
```

- [ ] **Step 2: Run** `pnpm exec vitest run src/pages/settings/registry.test.ts`. Expected: FAIL, `Failed to resolve import "./registry"`.
- [ ] **Step 3: Implement** `registry.ts` with the Produces signatures. `settingsTarget` decodes with `sectionId`, looks up `LEGACY_ANCHORS` first, then `sections.find`.
- [ ] **Step 4: Run** the test. Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/settings/registry.ts frontend/src/pages/settings/registry.test.ts
git commit -m "feat(frontend): a registry of Settings sections and the /settings/:area address rules"
```

---

### Task 3: The dirty registry, the shared settings cache, and the Bambuddy resources

**Files:**
- Create: `frontend/src/pages/settings/dirty.tsx`, `frontend/src/pages/settings/cache.tsx`, `frontend/src/pages/settings/bambuddyResources.tsx`
- Create: `frontend/src/pages/settings/cache.test.tsx` (tests for all three)

**Interfaces:**
- Consumes: `useAsync`, `AsyncState` (`lib/useAsync.ts`), `useSubscription` (`lib/realtime.ts`), `useLatest` (`lib/useLatest.ts`), `setBambuddyLinks` (`lib/bambuddyLinks.ts`), `Edits` (`seed.ts`), `api.getSettings`, `api.getBambuddyTargets`, `api.getProjects`, `api.getBambuddyStatus`.
- Produces (`dirty.tsx`):
  - `SettingsDirtyProvider({ children }: { children: ReactNode })`
  - `useReportDirty(id: string, dirty: boolean, drafts?: Partial<Record<FieldName, string>>): void` (cleared on unmount)
  - `useDirtySections(): readonly string[]` (ids dirty now, in report order)
  - `useAnyDirty(): () => boolean` (reads the latest, for handlers and async callbacks)
  - `usePeerDraft(name: FieldName): string | undefined` (another section's published draft of `name`)
- Produces (`cache.tsx`):
  - `type Seeder = { begin: () => Edits; seed: (names: 'all' | readonly FieldName[], since: Edits) => void }`
  - `type SettingsCache = { settings: Settings | undefined; error: Error | undefined; loading: boolean; reload: () => void; register: (seeder: Seeder) => () => void; write: (names: 'all' | readonly FieldName[] | ((next: Settings) => readonly FieldName[]), request: () => Promise<Settings>) => Promise<Settings>; own: <T>(work: () => Promise<T>) => Promise<T>; changedElsewhere: boolean; loadLatest: () => void; keepMine: () => void }`
  - `SettingsCacheProvider({ children })` (inside `SettingsDirtyProvider`), `useSettingsCache(): SettingsCache`, `useLoadedSettings(): Settings` (throws `useLoadedSettings outside a loaded settings cache`), `SettingsLoaded({ children }: { children: ReactNode })` (renders children only once `settings` is defined)
  - `write`: takes every seeder's `begin()` before `request()`, then `setData(next)`, `setBambuddyLinks(next)`, and calls each seeder's `seed(names, itsSince)`; runs inside `own`.
  - The subscription (today's `:395-421`, moved): ignores `'resync'` and any signal while an `own` is in flight; while `useAnyDirty()()` is true sets `changedElsewhere`; else `refresh` and, on landing, seeds every seeder `'all'` with its `begin()` taken then, or sets `changedElsewhere` if a section became dirty meanwhile. `loadLatest` seeds every seeder `'all'` with `begin()` now, clears the flag and refreshes; `keepMine` clears the flag.
- Produces (`bambuddyResources.tsx`):
  - `type BambuddyResources = { connected: boolean; targets: AsyncState<BambuddyTargets | null>; projects: AsyncState<ProjectChoices | null>; status: AsyncState<BambuddyStatus | null> }`
  - `BambuddyResourcesProvider({ children })` (inside the cache; `connected = Boolean(settings?.bambuddy_url)`, deps as today's `:302-314`), `useBambuddyResources(): BambuddyResources`

- [ ] **Step 1: Write the failing tests** in `cache.test.tsx` (wrap in `SettingsDirtyProvider > SettingsCacheProvider > BambuddyResourcesProvider`, rendered with `renderPage`):

```ts
it('makes one GET /settings for every section that reads it', async () => {
  const get = vi.spyOn(api, 'getSettings')
  // three consumers calling useSettingsCache()
  await waitFor(() => expect(screen.getAllByTestId('loaded')).toHaveLength(3))
  expect(get).toHaveBeenCalledTimes(1)
})
it('seeds every seeder with the fields a write names, each since taken before the request', async () => {
  // two fake seeders; order[] records 'begin-a', 'begin-b', 'request', 'seed-a', 'seed-b'
  await cache.write(['store_backend'], async () => { order.push('request'); return api.putSettings({ store_backend: 'local' }) })
  expect(order).toEqual(['begin-a', 'begin-b', 'request', 'seed-a', 'seed-b'])
  expect(seedA).toHaveBeenCalledWith(['store_backend'], sinceA)
})
it("ignores its own write's settings event", async () => {
  const get = vi.spyOn(api, 'getSettings')
  // while cache.own(pending) runs:
  emitRealtime('settings.changed', ['settings'], { section: 'connection' })
  await new Promise((r) => setTimeout(r, 50))
  expect(get).toHaveBeenCalledTimes(1)
  expect(cache.changedElsewhere).toBe(false)
})
it('follows an event while nothing is dirty: every seeder seeds all', async () => { /* seed called with 'all' */ })
it('says changed elsewhere while any section is dirty, and Load the latest seeds every seeder', async () => {
  // reporter 'a' dirty via useReportDirty('a', true); reporter 'b' clean; both register seeders
  emitRealtime('settings.changed', ['settings'], { section: 'connection' })
  await waitFor(() => expect(cache.changedElsewhere).toBe(true))
  expect(seedA).not.toHaveBeenCalledWith('all', expect.anything())
  expect(seedB).not.toHaveBeenCalledWith('all', expect.anything())
  act(() => cache.loadLatest())
  expect(seedA).toHaveBeenCalledWith('all', expect.any(Map))
  expect(seedB).toHaveBeenCalledWith('all', expect.any(Map))
  expect(cache.changedElsewhere).toBe(false)
})
it('lists the dirty sections and drops one that unmounts', async () => { /* useDirtySections() → ['a'], then [] */ })
it('hands one section’s published draft to another', async () => {
  // useReportDirty('connection', true, { bambuddy_url: 'https://new.test' }) → usePeerDraft('bambuddy_url') === 'https://new.test'
})
it('fetches no targets until a Bambuddy URL is saved, and one for every reader after', async () => {
  // setMockSettings({ bambuddy_url: null }) → getBambuddyTargets not called; default mock → called once for two readers
})
```

- [ ] **Step 2: Run** `pnpm exec vitest run src/pages/settings/cache.test.tsx`. Expected: FAIL, unresolved imports.
- [ ] **Step 3: Implement** the three files with the signatures above. Subscription logic is today's `SettingsPage.tsx:395-421`; resources are today's `:302-314`.
- [ ] **Step 4: Run** the test. Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/settings/dirty.tsx frontend/src/pages/settings/cache.tsx frontend/src/pages/settings/bambuddyResources.tsx frontend/src/pages/settings/cache.test.tsx
git commit -m "feat(frontend): one settings cache, a dirty registry and Bambuddy resources for Settings"
```

---

### Task 4: `useBackendSettings` and the shared form parts

**Files:**
- Create: `frontend/src/pages/settings/useBackendSettings.ts`, `frontend/src/pages/settings/formParts.tsx`
- Create: `frontend/src/pages/settings/useBackendSettings.test.tsx`

**Interfaces:**
- Consumes: Task 1 (`fieldsOf`, `baseline`, `toPatchValue`, `isProblem`, `isSecret`, `Secret`, `extraSpecs`, `SPECS`), Task 3 (`useSettingsCache`, `useLoadedSettings`, `useReportDirty`), `seed.ts` (`pendingFields`, `editedSince`, `seedDraft`, `Draft`, `Edits`, `Seed`), `controls.tsx` (`Section`, `FieldRow`, `RuntimeInput`, `SourceBadge`).
- Produces (`useBackendSettings.ts`):
  - `type FieldsOf = readonly FieldName[] | ((settings: Settings) => readonly FieldName[])`
  - `type FormContext = { value: (name: FieldName) => string; changed: (name: FieldName) => boolean; settings: Settings }`
  - `type BackendSettingsOptions = { effective?: (name: FieldName, raw: string, form: Omit<FormContext, 'changed'>) => string; alsoSend?: (form: FormContext) => Partial<SettingsUpdate>; alsoReset?: (name: FieldName, settings: Settings) => Partial<SettingsUpdate>; publish?: readonly FieldName[]; onSaved?: (next: Settings) => void; onReset?: (name: FieldName, next: Settings) => void }`
  - `type BackendSettings = { settings: Settings; value: (name: FieldName) => string; setField: (name: FieldName, next: string) => void; changed: (name: FieldName) => boolean; dirty: boolean; errors: Partial<Record<FieldName, string>>; sectionError: string | undefined; saving: boolean; savedAt: string | undefined; clearing: readonly Secret[]; clearSecret: (name: Secret) => void; save: () => Promise<boolean>; discard: () => void; reset: (name: FieldName) => Promise<void>; resetting: FieldName | null; resetError: string | null }`
  - `useBackendSettings(id: SectionId, fields: FieldsOf, options?: BackendSettingsOptions): BackendSettings`
  - Behaviour, all moved from `SettingsPage.tsx`: the seed queue and effect (`:316-338`, scoped to this section's fields, starting with `{ names: 'all', since: new Map() }`; registers a `Seeder` with the cache), `value`/`setField` (`:357-364`), `changed` (`:383-388`, comparing `effective(...)` when given), `patchFor` (`:425-463`, the Connection-only branch replaced by `alsoSend`), `save` through `cache.write(next => [...fields(next), ...keys of alsoSend's body])` (`:466-504`), `discard` (`:506-516`), `reset` through `cache.write` with `{ reset: [name], ...alsoReset(name, settings) }` (`:518-542`). Reports `useReportDirty(id, dirty, <draft of options.publish>)`.
- Produces (`formParts.tsx`):
  - `SavedSection({ form, id, title, description, children }: { form: BackendSettings; id: string; title: string; description?: ReactNode; children: ReactNode })`: today's `saved()` (`:747-765`) plus `form.resetError` as a `role="alert"` paragraph.
  - `FieldBadge({ form, name }: { form: BackendSettings; name: FieldName })`: today's `badge()` (`:698-705`).
  - `RuntimeRows({ form, section }: { form: BackendSettings; section: SectionId })`: today's `runtimeRow`/`runtimeRows` (`:712-745`).
  - `Advanced({ label, invalid, children }: { label: string; invalid: boolean; children: ReactNode })`: moved from `:1726-1748` (the old page imports it from here).

- [ ] **Step 1: Write the failing tests.** The test file defines two harness sections: `Timing` (`useBackendSettings('rendering', ['render_timeout', 'job_ttl'])` with `RuntimeRows`) and `Store` (`useBackendSettings('projects', ['store_backend'], { effective: (n, raw, f) => n === 'store_backend' ? storeChoice(f.settings, f.value).backend : raw })`), plus a third, `Url`, on `['bambuddy_url']` with `alsoReset: (name, s) => (name === 'bambuddy_url' && s.store_backend === 'bambuddy' ? { store_backend: 'local' } : {})`. Render them under the Task 3 providers inside `SettingsLoaded`.

```ts
it('seeds its draft from the shared settings and reports clean', async () => {
  expect(await screen.findByLabelText('Render timeout')).toHaveValue(300)
  expect(dirtySections()).toEqual([])
})
it('reports dirty on an edit, and clean again after Discard', async () => { /* useDirtySections() → ['rendering'], then [] */ })
it('saves only its own changed fields', async () => {
  const put = vi.spyOn(api, 'putSettings')
  // edit Render timeout to 45 and the Url harness's field, then Save Rendering
  expect(put).toHaveBeenCalledWith({ render_timeout: 45 })
})
it('keeps an edit typed while its own save is in flight (#767)', async () => { /* as SettingsPage.sections.test.tsx:455 */ })
it("keeps another section's edit when this one saves (#767)", async () => { /* as :483 */ })
it('refuses a negative number before sending, tied to the field', async () => {
  // Render timeout '-1' → Save → no PUT, error 'Enter 0 or more.' with aria-describedby on the input
})
it("shows a 422's field error beside the field, and a detail-only error on the section", async () => { /* server.use(...) */ })
it('resets with alsoReset and reseeds those fields in every section', async () => {
  // setMockSettings({ store_backend: 'bambuddy', ... }); click Reset on bambuddy_url
  expect(put).toHaveBeenCalledWith({ reset: ['bambuddy_url'], store_backend: 'local' })
  await waitFor(() => expect(screen.getByLabelText('Blob store')).toHaveTextContent('This server’s volume'))
})
it('compares and sends the effective value', async () => { /* chosen 'bambuddy' without inbox → not dirty */ })
```

- [ ] **Step 2: Run** `pnpm exec vitest run src/pages/settings/useBackendSettings.test.tsx`. Expected: FAIL, unresolved imports.
- [ ] **Step 3: Implement** both files with the signatures above; in `SettingsPage.tsx` replace the local `Advanced` with the import from `formParts.tsx`.
- [ ] **Step 4: Run** the test and `pnpm exec vitest run src/pages/SettingsPage`. Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/settings/useBackendSettings.ts frontend/src/pages/settings/formParts.tsx frontend/src/pages/settings/useBackendSettings.test.tsx frontend/src/pages/SettingsPage.tsx
git commit -m "feat(frontend): useBackendSettings, one section's PUT /settings form state"
```

---

### Task 5: The agent's `settings` scope in the shell

**Files:**
- Create: `frontend/src/pages/settings/agentFields.tsx`
- Create: `frontend/src/pages/settings/agentFields.test.tsx`

**Interfaces:**
- Consumes: `useAgentHandlers`, `useLatest` (`agent/useAgentHandlers.ts`), `AgentToolError` (`agent/types.ts`), `committed`, `touchAfterRender` (`agent/highlight.ts`), `SETTINGS_FIELDS` (`agent/catalog.ts`), `useReveal` (`collapseState.ts`), Task 2 (`resolveLocation`, `SettingsSection`), Task 3 (`useAnyDirty`, `useSettingsCache`), `baseline` (Task 1).
- Produces:
  - `type AgentFormField = (typeof SETTINGS_FIELDS)[number]`
  - `AGENT_FIELD_HOME: Record<AgentFormField, { section: string; domId: string }>` = `bambuddy_url` connection/`bambuddy-url`, `public_url` connection/`public-url`, `library_folder_id` projects/`library-folder`, `printer_id` printing/`printer`, `default_plate` preview/`default-plate`, `display_unit` preview/`display-unit` (today's `form`, `:547-554`)
  - `type AgentFieldBinding = { value: () => string; set: (next: string) => void; allowed?: () => readonly string[]; listed?: () => unknown }`
  - `type ConnectionAgent = { testStored: () => Promise<ConnectionTest>; lastTest: () => ConnectionTest | null; error: () => string | null; apiKeyTyped: () => boolean }`
  - `SettingsAgentProvider({ sections, children }: { sections: readonly SettingsSection[]; children: ReactNode })`: registers `useAgentHandlers('settings', { get_form, set_field, test_connection }, describe)`.
  - `useSettingsAgentField(field: AgentFormField, binding: AgentFieldBinding): void`, `useSettingsConnectionAgent(agent: ConnectionAgent): void`
  - `get_form`: today's shape (`:576-589`): `values` (each field's binding, else `baseline` from the cache), `unsaved: useAnyDirty()()`, `has_api_key`, `api_key_typed`, `choices` `{ library_folder_id, printer_id, default_plate, display_unit }` from each binding's `listed()`, `last_test`, `error`.
  - `set_field`: refuses a value outside `allowed()` with today's `invalid_args` message (`:591-597`); when the field's area (via `AGENT_FIELD_HOME[field].section` in `sections`) is not the current one (`resolveLocation(location.pathname, location.hash, sections).area`), `navigate('/settings/<area>')`; then `binding.set`, `reveal(domId)`, `touchAfterRender`, `await committed(() => binding.value() === next)`; returns `{ field, value, saved: false, note: 'The user saves each section with its own Save button.' }`.
  - `test_connection`: refused with today's message (`:606-611`) while any section is dirty; else `agent.testStored()` → `{ ok, detail }`; a thrown error becomes `AgentToolError('failed', message)`.

- [ ] **Step 1: Write the failing tests** with two fake sections in the test file, each rendering a `Section` (`controls.tsx`) with one input and binding it: `Here` (general, binds `bambuddy_url`, `domId` `bambuddy-url`, reports dirty) and `There` (library, `id` `preview`, binds `display_unit` with `allowed: () => ['mm', 'in']`, input id `display-unit`). Render under `SettingsCollapseProvider`, Task 3's providers and `SettingsAgentProvider sections={fakes}` at `/settings/general`, with a `Where` that prints the pathname.

```ts
it('set_field in another area navigates there, unfolds it and keeps other drafts', async () => {
  localStorage.setItem('scadbuddy.settings.collapsed', JSON.stringify(['preview']))
  await user.type(screen.getByLabelText('Bambuddy URL'), '/x')
  expect(await bridge.call('set_field', { field: 'display_unit', value: 'in' })).toMatchObject({ ok: true, result: { saved: false } })
  expect(screen.getByTestId('where')).toHaveTextContent('/settings/library')
  expect(screen.getByLabelText('Show dimensions in')).toBeVisible()
  expect(screen.getByLabelText('Bambuddy URL')).toHaveValue('https://bambuddy.internal.nullreference.io/x')
  expect(await bridge.call('get_form', {})).toMatchObject({ ok: true, result: { unsaved: true, values: { display_unit: 'in' } } })
})
it('refuses a value that is not a choice', async () => { /* 'cubits' → error.code 'invalid_args' */ })
it('reads a field with no binding from the saved settings', async () => { /* public_url in values equals mockSettings.public_url */ })
it('refuses test_connection while any section is unsaved', async () => { /* error.code 'refused', testStored not called */ })
```

- [ ] **Step 2: Run** `pnpm exec vitest run src/pages/settings/agentFields.test.tsx`. Expected: FAIL, unresolved import.
- [ ] **Step 3: Implement** `agentFields.tsx` with the signatures above.
- [ ] **Step 4: Run** the test. Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/settings/agentFields.tsx frontend/src/pages/settings/agentFields.test.tsx
git commit -m "feat(frontend): the settings agent scope dispatches to the section that owns a field"
```

---

### Task 6: The leave guard lets `/settings/*` through

**Files:**
- Modify: `frontend/src/lib/useLeaveGuard.ts:49`, `:69`, `:83`
- Test: `frontend/src/lib/useLeaveGuard.test.tsx`

**Interfaces:**
- Produces: `useLeaveGuard(dirty: boolean, options?: { within?: string })`. A link or navigation passes when its pathname equals the current one, or, with `within`, equals `within` or starts with `${within}/`. Without `options`, unchanged (`SourceWorkbench.tsx:77` keeps today's rule).

- [ ] **Step 1: Write the failing tests** (a harness page at `/settings/general` with `useLeaveGuard(true, { within: '/settings' })`, links to `/settings/library`, `/settings#fonts` and `/`):

```ts
it('with within, lets a link to another path under it through while dirty', async () => {
  await user.click(screen.getByRole('link', { name: 'Library' }))
  expect(screen.getByTestId('where')).toHaveTextContent('/settings/library')
  expect(guardPending()).toBeNull()
})
it('with within, lets a programmatic navigation under it through', async () => { /* navigate('/settings#fonts') passes */ })
it('with within, still holds a link out of it', async () => { /* '/' → pending '/' */ })
it('Back from a second Settings path returns to the first without asking', async () => {
  // BrowserRouter at /settings/general, dirty, click Library, act(() => history.back())
  expect(window.location.pathname).toBe('/settings/general')
  expect(guardPending()).toBeNull()
})
```

- [ ] **Step 2: Run** `pnpm exec vitest run src/lib/useLeaveGuard.test.tsx`. Expected: the new tests FAIL (the link is held).
- [ ] **Step 3: Implement** one `inside(pathname: string): boolean` closure used at `:69` and `:83`, from `location.pathname` and `options?.within`.
- [ ] **Step 4: Run** the test file. Expected: PASS, the existing tests too.
- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/useLeaveGuard.ts frontend/src/lib/useLeaveGuard.test.tsx
git commit -m "feat(frontend): useLeaveGuard can let every path under a prefix through"
```

---

### Task 7: The Settings shell

**Files:**
- Create: `frontend/src/pages/settings/SettingsShell.tsx`
- Create: `frontend/src/pages/settings/SettingsShell.test.tsx`
- Create: `frontend/src/pages/settings/testing.tsx`

**Interfaces:**
- Consumes: Tasks 2-6; `SettingsCollapseProvider`, `CollapseContext`, `useReveal`; `useSectionNav`, `sectionId`; `TipProvider`; `useAiAvailability`; `McpAuthBanner`; shadcn `SidebarMenuSub`, `SidebarMenuSubItem`, `SidebarMenuSubButton`, `Badge`, `Card`, `AlertDialog*`, `Skeleton`, `Button`.
- Produces:
  - `SettingsProviders({ sections, children }: { sections?: readonly SettingsSection[]; children: ReactNode })`: `SettingsCollapseProvider known={foldIdsOf(sections)}` > `TipProvider` > `SettingsDirtyProvider` > `SettingsCacheProvider` > `BambuddyResourcesProvider` > `SettingsAgentProvider sections`. Default `sections` is `SECTION_REGISTRY`.
  - `SettingsShell({ sections }: { sections?: readonly SettingsSection[] })`:
    - `const target = resolveLocation(pathname, hash, sections)`; when the address differs from `/settings/<target.area><target.hash>`, `navigate(..., { replace: true })` from an effect. It never returns early on a redirect: the areas stay mounted, so drafts survive.
    - Cache error and loading render today's `:662-696` cards; otherwise the page: `h1` "Settings" and today's intro (`:810-815`), `McpAuthBanner` while `ai.available` (`:818`), the changed-elsewhere card (`:820-834`, wired to `loadLatest`/`keepMine`).
    - One `<nav aria-label="Settings areas">` holding one `SidebarMenuSub` list: a column from `md` up, a sideways-scrolling strip below it (responsive classes on the one list, so each link exists once). Each item is `SidebarMenuSubButton asChild` around a `Link` to `/settings/<area>`, `aria-current="page"` on the current one, and `<Badge aria-label="unsaved" …/>` when any of its sections is in `useDirtySections()`.
    - The pane (`ref`, class `sb-settings relative h-full overflow-y-auto`, as today `:776`) holds the sticky `<nav aria-label="Settings sections">` (today's `:777-807` markup, listing the current area's shown sections, `go(id, { inPlace: anyDirty })`), then one `<div data-area={id} hidden={id !== target.area}>` per area holding its sections, each in a `SectionSlot`.
    - `SectionSlot` (local): for a `hideWhenEmpty` section, observes `#<id>-body` with a `MutationObserver` (childList) and calls `CollapseContext.setEmpty(id, childElementCount === 0)`; the slot is `hidden` and its sub-nav link dropped while empty. A `hideWhenEmpty` section passes no `description` to `Section`.
    - For an area with `servedByAgent` while `!ai.available`: one `<p role="status">` "The assistant service is not answering: the settings it serves are hidden until it does." above its sections.
    - On an area change with no hash, the pane scrolls to the top.
    - The `useSectionNav` ids are the current area's shown section ids (memoised); the reveal effect is today's `:351-354`.
    - `useLeaveGuard(anyDirty, { within: '/settings' })` and today's dialog (`:1704-1721`), its list built from the dirty ids' registry titles.
  - `testing.tsx`: `renderSettingsSection(ui: ReactElement, options?: { route?: string; sections?: readonly SettingsSection[] })` (renders `<SettingsProviders><SettingsLoaded>{ui}</SettingsLoaded></SettingsProviders>` with `renderPage`, default route `/settings/general`); `renderSettingsShell(sections: readonly SettingsSection[], options?: { route?: string })`; `region(name: string): HTMLElement` and `enter(user: UserEvent, field: HTMLElement, text: string)` as in today's `SettingsPage.sections.test.tsx:43-57`.

- [ ] **Step 1: Write the failing tests** in `SettingsShell.test.tsx` with fake sections: `alpha` (general, a `Section` with a text input labelled "Alpha" that reports `useReportDirty('alpha', value !== '')`), `beta` (library, input "Beta"), `packages` (tools), `ghost` (tools, `hideWhenEmpty`, renders `Section` with no children), and a route `/` beside the shell for leaving.

```ts
it('redirects /settings to /settings/general', async () => { /* Where shows '/settings/general' */ })
it('opens the area that holds a /settings#<section> link, unfolded', async () => {
  localStorage.setItem('scadbuddy.settings.collapsed', JSON.stringify(['beta']))
  renderSettingsShell(fakes, { route: '/settings#beta' })
  await waitFor(() => expect(where()).toBe('/settings/library#beta'))
  expect(screen.getByRole('button', { name: 'Beta' })).toHaveAttribute('aria-expanded', 'true')
})
it('sends an old anchor to its new section', async () => { /* '/settings#assistant-plugins' → '/settings/tools#packages' */ })
it('lands on General for a hash no area holds, and for a malformed escape', async () => { /* '#nope', '#%E0%A4%A' */ })
it("lists every area, and only the current area's sections", async () => {
  const areas = screen.getByRole('navigation', { name: 'Settings areas' })
  expect(within(areas).getAllByRole('link').map((l) => l.textContent)).toEqual(['General', 'Printing', 'Library', 'Assistant', 'Tools & plugins', 'MCP access', 'Audits', 'System'])
  expect(within(screen.getByRole('navigation', { name: 'Settings sections' })).getAllByRole('link').map((l) => l.textContent)).toEqual(['Alpha'])
})
it('keeps a draft across area switches, dots the area, and asks only when leaving Settings', async () => {
  await enter(user, screen.getByLabelText('Alpha'), 'draft')
  await user.click(within(areas()).getByRole('link', { name: 'Library' }))
  expect(where()).toBe('/settings/library')
  expect(screen.queryByRole('alertdialog')).toBeNull()
  expect(within(areas()).getByRole('link', { name: /General/ })).toContainElement(screen.getByLabelText('unsaved'))
  await user.click(within(areas()).getByRole('link', { name: /General/ }))
  expect(screen.getByLabelText('Alpha')).toHaveValue('draft')
  await user.click(screen.getByRole('link', { name: 'Leave' }))
  expect(await screen.findByRole('alertdialog', { name: 'Leave without saving?' })).toHaveTextContent('Alpha')
})
it('keeps drafts through a redirect', async () => { /* dirty alpha, click a Link to '/settings#beta' → library, Alpha still 'draft' */ })
it('hides an empty hideWhenEmpty section and its sub-nav link', async () => { /* at /settings/tools: no 'Ghost' link, #ghost hidden */ })
it('shows one notice on an agent-served area while the assistant is unavailable', async () => {
  // vi.mock('../../agent/chat/availability') → available false; at /settings/tools one role=status with the text; at /settings/general none
})
it('scrolls the pane to the top on an area switch', async () => { /* stub scrollTo on .sb-settings; switching areas records 0 */ })
```

- [ ] **Step 2: Run** `pnpm exec vitest run src/pages/settings/SettingsShell.test.tsx`. Expected: FAIL, unresolved import.
- [ ] **Step 3: Implement** `SettingsShell.tsx` and `testing.tsx` with the signatures above.
- [ ] **Step 4: Run** the test. Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/settings/SettingsShell.tsx frontend/src/pages/settings/SettingsShell.test.tsx frontend/src/pages/settings/testing.tsx
git commit -m "feat(frontend): the Settings shell: areas, sub-nav, redirects and the leave rule"
```

---

### Tasks 8-12: the sections

Each section is `frontend/src/pages/settings/sections/<id>.tsx` exporting `id`, `area`, `title`, `order` (10, 20, 30… within its area), optional `hideWhenEmpty = true`, and `Component`. Its JSX is the old page's for that section, moved as it is: `value`/`setField`/`errors`/`badge`/`runtimeRows`/`saved` become `form.value`, `form.setField`, `form.errors`, `<FieldBadge>`, `<RuntimeRows>` and `<SavedSection>`. A section that reads targets, projects or status takes them from `useBambuddyResources()`. Tests move by copying each named old test into the section's test file, rendered with `renderSettingsSection(<Component />)` (several sections together where a test spans them), and with `region`/`enter` from `testing.tsx`; the old copy stays until Task 13 deletes the old files, so both pass meanwhile. Each task's steps:

- Step 1: copy the tests listed into the new test files. Step 2: run `pnpm exec vitest run src/pages/settings/sections/`; expected FAIL, unresolved imports. Step 3: create the section files. Step 4: run again, then `pnpm exec vitest run src/pages/SettingsPage`; expected PASS for both. Step 5: commit with the given command.

### Task 8: General and System (Connection, Diagnostics, Administration, About)

**Files:**
- Create: `sections/connection.tsx` (+ `.test.tsx`), `sections/diagnostics.tsx`, `sections/administration.tsx` (+ `.test.tsx`), `sections/about.tsx` (+ `.test.tsx`)

**Interfaces:**
- Consumes: Tasks 1, 3, 4, 5; `UpstreamAnswerDetails` moves into `connection.tsx` (from `SettingsPage.tsx:1750-1789`).
- Produces:
  - `connection` (general, 10, "Connection"): JSX `:843-1104`. `useBackendSettings('connection', (s) => fieldsOf('connection', s), { publish: ['bambuddy_url'], alsoSend: <today's :451-461 rule>, alsoReset: (name, s) => name === 'bambuddy_url' && s.store_backend === 'bambuddy' && s.library_backend !== 'native' ? { store_backend: 'local' } : {}, onSaved: () => { targets.reload(); projects.reload() }, onReset: (name) => <reload both for bambuddy_url, bambuddy_api_key> })`. Its own `testing`, `test`, `registering`, `sidebar`, `error` state; `runTest` (today's `:634-648`, the whole run inside `cache.own`, then `status.reload()`) and `addSidebar` (`:650-660`); `error` shows as `role="alert"` at the section's top. Binds `bambuddy_url` and `public_url` with `useSettingsAgentField`, and `useSettingsConnectionAgent({ testStored, lastTest, error, apiKeyTyped })` where `testStored` is today's `:612-628` minus the dirty check.
  - `diagnostics` (system, 10, "Diagnostics"): `SavedSection` + `RuntimeRows section="diagnostics"`, fields `(s) => fieldsOf('diagnostics', s)` (its env-only extras included).
  - `administration` (system, 20, "Administration"): JSX `:1587-1657`.
  - `about` (system, 30, "About"): JSX `:1659-1700`; Bambuddy's version from `useBambuddyResources().status`.
- Tests moved:
  - to `connection.test.tsx`: all of `SettingsPage.test.tsx` `describe('SettingsPage')` from 'loads the stored connection…' to 'shows no answer when the test passed', 'sends the key only when one was typed', 'clears the key field once it is saved', 'registers the Bambuddy sidebar entry'; `SettingsPage.sections.test.tsx` 'lists every scope…', 'says Bambuddy takes no finish photo…', 'typing a key after Remove key…', and from `describe('SettingsPage blob store (#426)')` 'warns while render workers…', 'drops the warning once a render key…', 'sends a typed render key with Connection…', 'removes a stored render key when Connection is saved'.
  - to `administration.test.tsx`: all of `SettingsPage.activity.test.tsx`, and `describe('SettingsPage Administration (#668)')`.
  - to `about.test.tsx`: 'shows the build and the values only the deployment sets'.

```bash
git add frontend/src/pages/settings/sections/connection.tsx frontend/src/pages/settings/sections/connection.test.tsx frontend/src/pages/settings/sections/diagnostics.tsx frontend/src/pages/settings/sections/administration.tsx frontend/src/pages/settings/sections/administration.test.tsx frontend/src/pages/settings/sections/about.tsx frontend/src/pages/settings/sections/about.test.tsx
git commit -m "refactor(frontend): Connection, Diagnostics, Administration and About as section components"
```

### Task 9: Printing (Printing defaults, Spool inventory, Remembered choices, Hotend usage)

**Files:**
- Create: `sections/printing.tsx` (+ `.test.tsx`), `sections/inventory.tsx`, `sections/remembered.tsx` (+ `.test.tsx`), `sections/hotend-usage.tsx`

**Interfaces:**
- Produces:
  - `printing` (printing, 10, "Printing defaults"): JSX `:1106-1161`, fields `fieldsOf('printing', s)`; binds `printer_id` (`allowed` = `['', ...printer ids]`, `listed` = `{ value, name }[]`, as `:559-583`).
  - `inventory` (printing, 20, "Spool inventory"): `:1163-1169`.
  - `remembered` (printing, 30, "Remembered choices"): `:1482-1489` without `HotendUsagePanel`, description unchanged.
  - `hotend-usage` (printing, 40, "Hotend usage", `hideWhenEmpty`): `<Section id title><HotendUsagePanel targets={targets.data} /></Section>` (the panel renders nothing without targets).
- Tests moved: to `printing.test.tsx`: 'keeps print runs forever…(#1052)', 'keeps finished operations…(#1053)', "shows the server refusing a retention below Temporal's…", 'refuses a print run retention under a day…', 'refuses a retention over ten years…'; to `remembered.test.tsx`: `describe('SettingsPage remembered choices (#322)')`.

```bash
git add frontend/src/pages/settings/sections/printing.tsx frontend/src/pages/settings/sections/printing.test.tsx frontend/src/pages/settings/sections/inventory.tsx frontend/src/pages/settings/sections/remembered.tsx frontend/src/pages/settings/sections/remembered.test.tsx frontend/src/pages/settings/sections/hotend-usage.tsx
git commit -m "refactor(frontend): the Printing area's sections as components"
```

### Task 10: Library (Projects & files, Uploads, Fonts, Libraries, Rendering, Preview)

**Files:**
- Create: `sections/projects.tsx` (+ `.test.tsx`), `sections/uploads.tsx` (+ `.test.tsx`), `sections/fonts.tsx`, `sections/libraries.tsx`, `sections/rendering.tsx` (+ `.test.tsx`), `sections/preview.tsx` (+ `.test.tsx`)

**Interfaces:**
- Produces:
  - `projects` (library, 10, "Projects & files"): JSX `:1171-1401`; `useBackendSettings('projects', (s) => fieldsOf('projects', s), { effective: (n, raw, f) => n === 'store_backend' ? storeChoice(f.settings, f.value).backend : raw, alsoReset: (name, s) => name === 'library_folder_id' && s.store_backend === 'bambuddy' && s.library_backend !== 'native' ? { store_backend: 'local' } : {} })`; the "not saved yet" hint reads `usePeerDraft('bambuddy_url')` in place of `value('bambuddy_url')` (`:1257`); `storeUsage` from its own `useAsync(() => api.getStoreUsage(), [])`; binds `library_folder_id`.
  - `uploads` (library, 20, "Uploads"): `:1403-1424`, its own `getAssetUsage`.
  - `fonts` (library, 30, "Fonts"), `libraries` (library, 40, "Libraries", `:1430-1436`), `rendering` (library, 50, "Rendering").
  - `preview` (library, 60, "Preview"): `:1438-1480`, its own `listPlates`; `onSaved: (next) => setDisplayUnit(next.display_unit)`; binds `default_plate` (`allowed` `['', ...names, stray]`, `listed` names) and `display_unit` (`['mm', 'in']`).
- Tests moved:
  - to `projects.test.tsx`: the rest of `describe('SettingsPage blob store (#426)')` (from 'seeds the Blob store choice…' to 'says a store cap of zero is no limit'), and `describe('SettingsPage S3 store')`. Tests that save Connection (the fall-backs, 'keeps the Bambuddy store on a Connection save…', 'offers the Bambuddy store only once its URL is saved…') render `<><Connection /><Projects /></>`.
  - to `uploads.test.tsx`: 'shows how much the upload store holds…(#296)', 'says a cap of zero is no limit', 'edits the upload limit in MB or GB and stores bytes'.
  - to `rendering.test.tsx`: `describe('SettingsPage sources (#322)')`, 'refuses a negative or fractional number…', and from `describe('SettingsPage sections (#322)')` 'saves one section and leaves another one’s edits unsaved', 'keeps an edit typed while its section is saving (#767)', 'keeps that edit when another section saves…', 'discards one section’s edits', 'shows a refused value beside its field' (each rendering `<><Connection /><Rendering /></>` or the sections they name).
  - to `preview.test.tsx`: 'saves the plate the preview falls back to (#81)', 'saves the display unit and switches every open view to it', 'offers the folders and printers Bambuddy reports…' and 'never sends a pipeline' (rendered with `<><Printing /><Projects /><Preview /></>`).

```bash
git add frontend/src/pages/settings/sections/projects.tsx frontend/src/pages/settings/sections/projects.test.tsx frontend/src/pages/settings/sections/uploads.tsx frontend/src/pages/settings/sections/uploads.test.tsx frontend/src/pages/settings/sections/fonts.tsx frontend/src/pages/settings/sections/libraries.tsx frontend/src/pages/settings/sections/rendering.tsx frontend/src/pages/settings/sections/rendering.test.tsx frontend/src/pages/settings/sections/preview.tsx frontend/src/pages/settings/sections/preview.test.tsx
git commit -m "refactor(frontend): the Library area's sections as components"
```

### Task 11: Assistant

**Files:**
- Create: `sections/assistant-credentials.tsx` (+ `.test.tsx`), `sections/model.tsx`, `sections/session-limits.tsx`, `sections/session-runtime.tsx`, `sections/render-limits.tsx`, `sections/images.tsx`, `sections/workspace.tsx`, `sections/default-session-mode.tsx`, `sections/durable-subagents.tsx`, `sections/memory.tsx`

**Interfaces:**
- Produces, each a `Section` around today's component(s), all `hideWhenEmpty` except the first (ids avoid the components' own `assistant-model`, `session-mode`, `*-heading` ids; titles avoid their cards' own titles):

| id | order | title | content (today's line) |
|---|---|---|---|
| `assistant-credentials` | 10 | Status & credentials | `AiStatusSection`, `AiCredentialSection` (`:1494-1496`; never behind `ai.available`, #1000) |
| `model` | 20 | Model | `ModelSetting` |
| `session-limits` | 30 | Session limits | `SessionLimitsSetting` |
| `session-runtime` | 40 | Runtime | `SessionRuntimeSetting` |
| `render-limits` | 50 | Render limits | `RenderLimitsSetting` |
| `images` | 60 | Images | `ImageSettingsSetting` |
| `workspace` | 70 | Workspace | `WorkspaceSettingsSetting` |
| `default-session-mode` | 80 | Session mode | `SessionModeSetting` |
| `durable-subagents` | 90 | Subagents | `DurableSubagentsSetting` (its card is titled "Durable subagents") |
| `memory` | 100 | Memory | `ai.available && <MemorySettingsSetting />` (`:1575`) |

- Tests moved: to `assistant-credentials.test.tsx`, "names one landmark Assistant…" rewritten as `it('names its region Status & credentials, and the status inside it "Assistant status"')`: `document.getElementById('assistant-credentials')` has accessible name `Status & credentials`; `document.getElementById('ai-status-heading')!.closest('[role="region"]')` has `Assistant status`.

```bash
git add frontend/src/pages/settings/sections/assistant-credentials.tsx frontend/src/pages/settings/sections/assistant-credentials.test.tsx frontend/src/pages/settings/sections/model.tsx frontend/src/pages/settings/sections/session-limits.tsx frontend/src/pages/settings/sections/session-runtime.tsx frontend/src/pages/settings/sections/render-limits.tsx frontend/src/pages/settings/sections/images.tsx frontend/src/pages/settings/sections/workspace.tsx frontend/src/pages/settings/sections/default-session-mode.tsx frontend/src/pages/settings/sections/durable-subagents.tsx frontend/src/pages/settings/sections/memory.tsx
git commit -m "refactor(frontend): the Assistant area's sections as components"
```

### Task 12: Tools & plugins, MCP access, Audits; McpTokensSection reads the auth mode itself

**Files:**
- Create: `sections/builtin.tsx`, `sections/packages.tsx`, `sections/remote.tsx`, `sections/mcp-auth-mode.tsx`, `sections/mcp-access-tokens.tsx` (+ `.test.tsx`), `sections/mcp-oidc.tsx`, `sections/audit-log.tsx`
- Modify: `frontend/src/components/McpTokensSection.tsx:132-159`
- Test: `frontend/src/components/McpTokensSection.test.tsx`

**Interfaces:**
- Produces:

| id | area | order | title | content (today's line) |
|---|---|---|---|---|
| `builtin` | tools | 10 | Built-in | the WebMCP switch (`:1501-1513`), `HttpRequestSetting`, `BrowserOriginsSetting`, `PrinterCameraSetting` |
| `packages` | tools | 20 | Packages (`hideWhenEmpty`) | `ai.available &&` the "Assistant plugins" paragraph (`:1568-1571`) and `PluginPackagesPanel` (its card is "Plugin packages") |
| `remote` | tools | 30 | Remote MCP plugins (`hideWhenEmpty`) | `ai.available && <RemotePluginsPanel />` |
| `mcp-auth-mode` | mcp | 10 | Auth mode (`hideWhenEmpty`) | `ai.available && <McpAuthSection onSaved={() => mcpAuthChanged()} />` |
| `mcp-access-tokens` | mcp | 20 | Tokens (`hideWhenEmpty`) | `ai.available && <McpTokensSection publicUrl={settings.public_url} />` (the connect snippet stays inside it) |
| `mcp-oidc` | mcp | 30 | OIDC sign-in (`hideWhenEmpty`) | `ai.available &&` `:1557-1561` |
| `audit-log` | audits | 10 | Audit log (`hideWhenEmpty`) | `AiAuditSection` (its card is "AI activity") |

- `McpTokensSection` (§3.5): the `authMode` prop goes; it reads `useMcpAuthVersion()` and `useAsync(() => api.getMcpAuth(), [version])`, and `authMode = auth.data?.mode ?? list?.auth_mode ?? null`.

- [ ] **Step 1: Write the failing tests.** In `McpTokensSection.test.tsx`: `it('reads the auth mode itself, and again when it is saved elsewhere on the page')`: mock `getMcpAuth` answering `disabled`, render `<McpTokensSection />` alone, expect `findByTestId('mcp-auth-note')` to contain "MCP authentication is turned off"; switch the mock to `bearer`, `act(() => mcpAuthChanged())`, expect the note gone. Copy `SettingsPage.ai.test.tsx`'s first three tests into `mcp-access-tokens.test.tsx`, rendering `<><McpAuthMode /><McpAccessTokens /></>`.
- [ ] **Step 2: Run** `pnpm exec vitest run src/components/McpTokensSection.test.tsx src/pages/settings/sections/`. Expected: FAIL (the note waits for a save; imports unresolved).
- [ ] **Step 3: Implement** the McpTokensSection change, then the seven section files; in the old `SettingsPage.tsx` drop the `mcpAuthMode` state (`:293`) and the `authMode` prop (`:1553`).
- [ ] **Step 4: Run** the same command and `pnpm exec vitest run src/pages/SettingsPage`. Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/settings/sections/builtin.tsx frontend/src/pages/settings/sections/packages.tsx frontend/src/pages/settings/sections/remote.tsx frontend/src/pages/settings/sections/mcp-auth-mode.tsx frontend/src/pages/settings/sections/mcp-access-tokens.tsx frontend/src/pages/settings/sections/mcp-access-tokens.test.tsx frontend/src/pages/settings/sections/mcp-oidc.tsx frontend/src/pages/settings/sections/audit-log.tsx frontend/src/components/McpTokensSection.tsx frontend/src/components/McpTokensSection.test.tsx frontend/src/pages/SettingsPage.tsx
git commit -m "refactor(frontend): Tools & plugins, MCP access and Audits sections; McpTokensSection reads the auth mode itself"
```

---

### Task 13: Route the shell, delete the one-page Settings, move the page tests

**Files:**
- Modify: `frontend/src/App.tsx:107`
- Modify: `frontend/src/pages/SettingsPage.tsx` (all of it: becomes the providers plus the shell)
- Modify: `frontend/src/pages/settings/fields.ts:3-27`, `frontend/src/pages/settings/collapse.tsx:61-139`, `frontend/src/pages/settings/collapse.test.tsx`
- Modify: `frontend/src/pages/SettingsPage.test.tsx`, `frontend/src/pages/SettingsPage.sections.test.tsx`, `frontend/src/pages/SettingsPage.ai.test.tsx`, `frontend/src/pages/agentTools.test.tsx:191-260`, `frontend/src/pages/settings/registry.test.ts`
- Delete: `frontend/src/pages/SettingsPage.activity.test.tsx`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `App.tsx`: `<Route path="settings/*" element={<SettingsPage />} />` (one route, so the shell stays mounted across areas).
  - `SettingsPage(): JSX.Element` = `<SettingsProviders><SettingsShell /></SettingsProviders>`.
  - `fields.ts`: `SECTIONS` and `sectionTitle` removed; `export type SectionId = 'connection' | 'printing' | 'projects' | 'uploads' | 'rendering' | 'fonts' | 'preview' | 'diagnostics' | 'administration'`.
  - `collapse.tsx`: `SubSection` and `SectionContents` removed (their empty-hiding is the shell's `SectionSlot`); `SettingsCollapseProvider` unchanged.

- [ ] **Step 1: Write the failing tests.**
  - `registry.test.ts`: `it('holds the spec §3.2 table')`: `AREAS.map((a) => [a.id, SECTION_REGISTRY.filter((s) => s.area === a.id).map((s) => s.id)])` equals `[['general', ['connection']], ['printing', ['printing', 'inventory', 'remembered', 'hotend-usage']], ['library', ['projects', 'uploads', 'fonts', 'libraries', 'rendering', 'preview']], ['assistant', ['assistant-credentials', 'model', 'session-limits', 'session-runtime', 'render-limits', 'images', 'workspace', 'default-session-mode', 'durable-subagents', 'memory']], ['tools', ['builtin', 'packages', 'remote']], ['mcp', ['mcp-auth-mode', 'mcp-access-tokens', 'mcp-oidc']], ['audits', ['audit-log']], ['system', ['diagnostics', 'administration', 'about']]]`; `it('sends every old anchor to a section that exists in its area')`.
  - `SettingsPage.test.tsx`: `it('gives every section an id, heading and body no other element on the page has')` (render `<SettingsPage />` at `/settings/general` with AI available, wait for seeding; for each registry id, `document.querySelectorAll('[id="<id>"]')`, `…-heading` and `…-body` each have length 1); `it('names no two regions alike')` (accessible names of every `section[aria-labelledby], [role="region"]` are distinct).
  - `agentTools.test.tsx` `describe('settings tools')`: `it('sets a field in another area, and goes there')`: render `<><Shell /><Where /></>`, `set_field display_unit` → `where` shows `/settings/library`, `getByLabelText('Show dimensions in')` visible.
- [ ] **Step 2: Run** `pnpm exec vitest run src/pages/settings/registry.test.ts src/pages/SettingsPage.test.tsx src/pages/agentTools.test.tsx`. Expected: FAIL (old page routed, sections missing from a single page, new tests unmet).
- [ ] **Step 3: Implement** the Produces above. Then the tests:
  - Delete from `SettingsPage.test.tsx`, `SettingsPage.sections.test.tsx` and `SettingsPage.ai.test.tsx` every test copied in Tasks 8-12; delete `SettingsPage.activity.test.tsx`.
  - Keep in `SettingsPage.test.tsx`: `describe('SettingsPage, live (#269)')` (render at `/settings/general`; "shows the assistant plugin sections only while…" at `/settings/tools`, asserting the `Packages` section and the "Plugin packages" heading), and the failed-load test. Move into it from `SettingsPage.sections.test.tsx`: `describe('SettingsPage section links…')` with `stubLayout`, `stubResizeObserver` and `Where` (routes stay `/settings#libraries`, which now redirects to `/settings/library#libraries`; `<Route path="/settings/*">` where a `Routes` is used; the #3414 test becomes `it('unfolds a deep-linked section folded on another area, and scrolls to it once laid out')` at `/settings#assistant-mcp` with `['mcp-auth-mode']` folded, landing on `/settings/mcp#mcp-auth-mode`), the four leave-guard tests from `describe('SettingsPage sections (#322)')` (`Route path="/settings/*"`), and `describe('SettingsPage folded sections (#2173)')`. Delete the emptied `SettingsPage.sections.test.tsx`.
  - Keep in `SettingsPage.ai.test.tsx` the banner test (#1921) only, at `/settings/mcp`.
  - `collapse.test.tsx`: the harness uses `Section` from `controls.tsx` (ids `g-model`, `g-audit`); keep the three #3415/readability tests, drop the two `SubSection`/`SectionContents` tests.
- [ ] **Step 4: Run** the full check `cd frontend && pnpm lint && pnpm typecheck && pnpm test && pnpm build`. Expected: PASS; `grep -rn "SubSection\|SectionContents\|sectionTitle" frontend/src` prints nothing.
- [ ] **Step 5: Commit**

```bash
git add -A frontend/src/App.tsx frontend/src/pages
git commit -m "feat(frontend): Settings by area at /settings/:area (#2171)"
```

---

### Task 14: Playwright specs on the area URLs

**Files:**
- Modify: `frontend/e2e/settings.spec.ts`, `hotend-usage.spec.ts:9`, `mcp-auth.spec.ts:15`, `mcp-tokens.spec.ts:35,61,67`, `plugin-packages.spec.ts:30,95,126`, `ai-audit.spec.ts:9`, `settings-tap-targets.spec.ts:39-47`, `agent-bridge.spec.ts:104-122`, `app-shell.spec.ts:18`, `page-titles.spec.ts:26`, `real-agent.spec.ts:65`

**Interfaces:** none.

- [ ] **Step 1: Edit the specs.**
  - `settings.spec.ts`: the round-trip at `/settings/library`; the leave test at `/settings/library` with `toHaveURL(/\/settings\/library$/)`; replace "shows every section link at desktop width" with `test('shows every area link, and each area’s section links, at desktop width')` looping the eight areas, the same box and overflow checks on both navs; `/settings#about` stays (it now asserts `toHaveURL(/\/settings\/system#about$/)` first); add `test('moves between areas with a draft, and asks only when leaving Settings')`: edit Render timeout on Library, click area "General", `expect(page.getByRole('alertdialog')).toHaveCount(0)`, back to Library, the value is kept, click "Models", the dialog lists Rendering.
  - `hotend-usage.spec.ts:9`: `/settings/printing#hotend-usage`.
  - `mcp-auth.spec.ts:15`, `mcp-tokens.spec.ts:35,61` and the frame match at `:67` (`endsWith('/settings/mcp')`): `/settings/mcp`.
  - `plugin-packages.spec.ts:30,95,126`: `/settings/tools`.
  - `ai-audit.spec.ts:9`: `/settings/audits`.
  - `settings-tap-targets.spec.ts`: loop the eight area URLs; on `/settings/system` expect the Deployment values table, on `/settings/tools` the "Built-in plugin scadbuddy" item and the files list; the small-target check runs on each.
  - `agent-bridge.spec.ts`: after `navigate /settings`, `set_field display_unit` then `await expect(page).toHaveURL(/\/settings\/library$/)`.
  - `app-shell.spec.ts:18`: `toHaveURL(/\/settings\/general$/)`.
  - `page-titles.spec.ts`: keep `/settings`, add `/settings/tools`.
  - `real-agent.spec.ts:65`: `/settings/assistant`.
- [ ] **Step 2: Run** `cd frontend && pnpm build && pnpm exec playwright test e2e/settings.spec.ts e2e/hotend-usage.spec.ts e2e/mcp-auth.spec.ts e2e/mcp-tokens.spec.ts e2e/plugin-packages.spec.ts e2e/ai-audit.spec.ts e2e/settings-tap-targets.spec.ts e2e/agent-bridge.spec.ts e2e/app-shell.spec.ts e2e/page-titles.spec.ts`. Expected: PASS (`real-agent.spec.ts` skips without `E2E_BASE_URL`).
- [ ] **Step 3: Commit**

```bash
git add frontend/e2e
git commit -m "test(frontend): Playwright specs open Settings at its area URLs"
```

---

### Task 15: Links and text pointers (§3.5)

**Files:**
- Modify: `frontend/src/components/activity/ActivityDrawer.tsx:281-285`, test `ActivityDrawer.test.tsx:63`
- Modify: `frontend/src/agent/chat/pageContext.ts:37`, test `frontend/src/agent/chat/Markdown.test.tsx:172,187`
- Modify: `frontend/src/components/McpAuthBanner.tsx:43,49`, tests `McpAuthBanner.test.tsx:41`, `assistant/AssistantPanel.test.tsx:149`, `frontend/e2e/mcp-auth.spec.ts:46`
- Modify: `frontend/src/components/assistant/SessionTouched.tsx:129`, test `SessionTouched.test.tsx:76`
- Modify: `frontend/src/agent/catalog.ts:42,265`
- Modify (strings only): `agent/src/tools/registry.ts:540,543`, `agent/src/tools/approvals.ts:71`, `agent/src/routes/subagentMcp.ts:127`, `agent/src/temporal/toolActivities.ts:473`, `agent/src/tools/browser.ts:273,460`
- Modify (docs): `plugins/scadbuddy/README.md:62` (the `PostToolUse` hook bumps `version` in `plugins/scadbuddy/.claude-plugin/plugin.json` and `agent/plugins/scadbuddy/.claude-plugin/plugin.json`, 0.1.24 → 0.1.25; bump both by hand if the edit did not go through the hook), `docs/ai/agent-sessions.md:258`, `docs/ai/browser-bridge.md:59`

**Interfaces:**
- Produces: `toolSettingsPath(viaTool: string): string` in `ActivityDrawer.tsx`: `/settings/tools#remote` for a `<server>__<tool>` name, else `/settings/tools#builtin`.

- [ ] **Step 1: Write the failing tests.**
  - `ActivityDrawer.test.tsx:63`: the `import_model` link has `href` `/settings/tools#builtin`; add `it('links a remote plugin tool to Remote MCP plugins')` with `via_tool: 'hindsight__recall'` → `/settings/tools#remote`.
  - `Markdown.test.tsx`: `expect(suggestedPrompts('/settings/tools')).toContain('Help me connect Bambuddy')` and the same for `/settings`.
  - `McpAuthBanner.test.tsx:41`, `AssistantPanel.test.tsx:149`: `href` `/settings/mcp#mcp-auth-mode`; the unlinked text reads "Turn it back on under Settings → MCP access."
  - `SessionTouched.test.tsx:76`: `href` `/settings/library#libraries`.
- [ ] **Step 2: Run** `cd frontend && pnpm exec vitest run src/components/activity/ActivityDrawer.test.tsx src/agent/chat/Markdown.test.tsx src/components/McpAuthBanner.test.tsx src/components/assistant/AssistantPanel.test.tsx src/components/assistant/SessionTouched.test.tsx`. Expected: FAIL on the new hrefs and prompts.
- [ ] **Step 3: Implement.**
  - `pageContext.ts:37`: pattern `/settings/*` (matches `/settings` too).
  - `McpAuthBanner.tsx:43` link `/settings/mcp#mcp-auth-mode`; `:49` text "Turn it back on under Settings → MCP access."
  - `SessionTouched.tsx:129`: `/settings/library#libraries`.
  - `catalog.ts:42` example route `"/settings/general"`; `:265` `settings: 'settings ("/settings/…")'`.
  - Agent strings: every "Settings → Plugins" in the five listed messages becomes "Settings → Tools & plugins"; `browser.ts:273` example route `"/settings/general"`, `:460` comment `Settings ("/settings/…")`.
  - Docs: `README.md:62` "Settings → MCP access → **Tokens**"; `agent-sessions.md:258` `/settings/library#libraries`; `browser-bridge.md:59` ``settings (`/settings/…`)``.
- [ ] **Step 4: Run** the Step 2 command; `cd frontend && pnpm exec playwright test e2e/mcp-auth.spec.ts`; `cd agent && pnpm lint && pnpm typecheck && pnpm exec vitest run test/builtInTools.test.ts test/subagentMcp.test.ts test/toolSwitches.test.ts`; `.github/scripts/lint-plugin.sh`. Expected: PASS (the agent tests match `/disabled in Settings/`, which the new text keeps).
- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/activity frontend/src/agent frontend/src/components/McpAuthBanner.tsx frontend/src/components/McpAuthBanner.test.tsx frontend/src/components/assistant frontend/e2e/mcp-auth.spec.ts agent/src/tools/registry.ts agent/src/tools/approvals.ts agent/src/routes/subagentMcp.ts agent/src/temporal/toolActivities.ts agent/src/tools/browser.ts plugins/scadbuddy agent/plugins/scadbuddy/.claude-plugin/plugin.json docs/ai/agent-sessions.md docs/ai/browser-bridge.md
git commit -m "fix(frontend): links and pointers name the Settings areas (#2171)"
```
