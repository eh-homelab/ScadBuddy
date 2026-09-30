import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useAiAvailability } from '../agent/chat/availability'
import { USER_ONLY } from '../agent/dom'
import { committed, touchAfterRender } from '../agent/highlight'
import { AgentToolError } from '../agent/types'
import { useAgentHandlers, useLatest } from '../agent/useAgentHandlers'
import { setWebMcpEnabled, useWebMcpEnabled } from '../agent/webmcpPreference'
import { api, ApiError } from '../api/client'
import type { ConnectionTest, Settings, SettingsUpdate, SidebarLink } from '../api/types'
import type { McpAuthMode } from '../api/mcpTokens'
import { AiStatusSection } from '../components/assistant/AiStatusSection'
import { McpAuthSection } from '../components/McpAuthSection'
import { HeadlessBrowserSetting } from '../components/HeadlessBrowserSetting'
import { HttpRequestSetting } from '../components/HttpRequestSetting'
import { SessionLimitsSetting } from '../components/SessionLimitsSetting'
import { McpOidcSettings } from '../components/McpOidcSettings'
import { PluginPackagesPanel } from '../components/settings/PluginPackages'
import { RemotePluginsPanel } from '../components/settings/RemotePlugins'
import { McpTokensSection } from '../components/McpTokensSection'
import { Button } from '../components/ui/Button'
import { Dialog } from '../components/ui/Dialog'
import { Spinner } from '../components/ui/Spinner'
import { AiAuditSection } from '../components/assistant/AiAuditSection'
import { LibraryUpgrade } from '../components/settings/LibraryUpgrade'
import { useSubscription } from '../lib/realtime'
import { formatBytes } from '../lib/format'
import { useAsync } from '../lib/useAsync'
import { setBambuddyLinks } from '../lib/bambuddyLinks'
import { plateSize, setDisplayUnit, type DisplayUnit } from '../lib/units'
import { FieldRow, RuntimeInput, Section, SourceBadge } from './settings/controls'
import {
  RUNTIME_FIELDS,
  SECTIONS,
  envVar,
  sectionTitle,
  serverValue,
  type FieldName,
  type FieldSpec,
  type SectionId,
} from './settings/fields'
import { RememberedChoicesPanel } from './settings/RememberedChoicesPanel'
import { useLeaveGuard } from './settings/useLeaveGuard'

/** #296 — `used` of `limit`, where a limit of 0 means none. */
function ofLimit(used: string, limit: number, format: (n: number) => string): string {
  return limit > 0 ? `${used} of ${format(limit)}` : `${used} (no limit)`
}

const SECRETS = ['bambuddy_api_key', 'bambuddy_render_api_key', 'google_fonts_api_key'] as const
type Secret = (typeof SECRETS)[number]

function isSecret(name: FieldName): name is Secret {
  return (SECRETS as readonly string[]).includes(name)
}

const ID_FIELDS: readonly FieldName[] = ['library_folder_id', 'printer_id', 'last_project_id']

/** The fields each section saves. The runtime ones come from `RUNTIME_FIELDS`. */
const HAND_LAID: Partial<Record<SectionId, FieldName[]>> = {
  connection: ['bambuddy_url', 'bambuddy_web_urls', 'bambuddy_api_key', 'bambuddy_render_api_key', 'public_url'],
  printing: ['printer_id'],
  // #426 — the blob store beside the inbox folder it needs.
  projects: ['library_folder_id', 'last_project_id', 'store_backend'],
  preview: ['display_unit', 'default_plate'],
}

const SPECS: Record<string, FieldSpec> = Object.fromEntries(RUNTIME_FIELDS.map((spec) => [spec.name, spec]))

/**
 * An env-seeded field this page has no control for yet is still shown and editable, in
 * Diagnostics, so no setting the server takes is out of reach (#322).
 */
function extraSpecs(settings: Settings | undefined): FieldSpec[] {
  const known = new Set<string>([...Object.values(HAND_LAID).flat(), ...Object.keys(SPECS)])
  return Object.keys(settings?.sources ?? {})
    .filter((name) => !known.has(name))
    .map((name) => {
      const current = (settings as Record<string, unknown>)[name]
      return {
        name: name as FieldName,
        section: 'diagnostics' as const,
        label: envVar(name),
        kind:
          typeof current === 'boolean'
            ? ('bool' as const)
            : typeof current === 'number'
              ? ('count' as const)
              : ('text' as const),
      }
    })
}

function fieldsOf(section: SectionId, settings: Settings | undefined): FieldName[] {
  return [
    ...(HAND_LAID[section] ?? []),
    ...[...RUNTIME_FIELDS, ...extraSpecs(settings)]
      .filter((spec) => spec.section === section)
      .map((spec) => spec.name),
  ]
}

function baseline(settings: Settings, name: FieldName): string {
  if (name === 'display_unit') return settings.display_unit ?? 'mm'
  return serverValue(settings, name)
}

type Draft = Partial<Record<FieldName, string>>
type Problem = { problem: string }

/** The form value for `name`, as `PUT /settings` takes it, or a problem to show. */
function toPatchValue(name: FieldName, raw: string, settings: Settings): unknown {
  if (ID_FIELDS.includes(name)) return raw === '' ? null : Number(raw)
  if (name === 'display_unit') return raw
  const kind = SPECS[name]?.kind ?? extraSpecs(settings).find((spec) => spec.name === name)?.kind
  if (kind === 'bool') return raw === 'true'
  if (kind === 'seconds' || kind === 'count' || kind === 'bytes') {
    if (raw.trim() === '') return { problem: 'Enter a value, or reset it to the deployment value.' } satisfies Problem
    const number = Number(raw)
    if (!Number.isFinite(number)) return { problem: 'Enter a number.' } satisfies Problem
    return number
  }
  // Text: empty clears it, which beats the deployment's value until it is reset.
  return raw === '' ? null : raw
}

function isProblem(value: unknown): value is Problem {
  return typeof value === 'object' && value !== null && 'problem' in value
}

export function SettingsPage() {
  const settingsState = useAsync(() => api.getSettings(), [])
  const settings = settingsState.data

  const [draft, setDraft] = useState<Draft>({})
  const [clearing, setClearing] = useState<Secret[]>([])
  const [saving, setSaving] = useState<SectionId | null>(null)
  const [savedAt, setSavedAt] = useState<Partial<Record<SectionId, string>>>({})
  const [errors, setErrors] = useState<Partial<Record<FieldName, string>>>({})
  const [sectionError, setSectionError] = useState<Partial<Record<SectionId, string>>>({})
  const [resetting, setResetting] = useState<FieldName | null>(null)
  const [testing, setTesting] = useState(false)
  const [test, setTest] = useState<ConnectionTest | null>(null)
  const [registering, setRegistering] = useState(false)
  const [sidebar, setSidebar] = useState<SidebarLink | null>(null)
  const [error, setError] = useState<string | null>(null)

  const webMcp = useWebMcpEnabled()
  const [mcpAuthMode, setMcpAuthMode] = useState<McpAuthMode | undefined>(undefined)
  const ai = useAiAvailability()
  const connected = Boolean(settings?.bambuddy_url)
  // #81 — needs no Bambuddy: the plates are ScadBuddy's own table.
  const platesState = useAsync(() => api.listPlates(), [])
  const usage = useAsync(() => api.getAssetUsage(), []).data
  const storeUsage = useAsync(() => api.getStoreUsage(), []).data
  const plateNames = (platesState.data?.plates ?? []).map((plate) => plate.name)

  // The pickers need a live Bambuddy, so they are only fetched once one is configured.
  const targetsState = useAsync(
    () => (connected ? api.getBambuddyTargets() : Promise.resolve(null)),
    [connected],
  )
  const projectsState = useAsync(
    () => (connected ? api.getProjects() : Promise.resolve(null)),
    [connected],
  )
  const bambuddyState = useAsync(
    () => (connected ? api.getBambuddyStatus() : Promise.resolve(null)),
    [connected, settings?.bambuddy_url],
  )

  // Which fields the next settings object seeds the form with: all of them at first
  // and after a reload, one section's after its save, one field's after a reset.
  const reseed = useRef<'all' | FieldName[] | null>('all')
  useEffect(() => {
    if (!settings || reseed.current === null) return
    const names =
      reseed.current === 'all' ? SECTIONS.flatMap((section) => fieldsOf(section.id, settings)) : reseed.current
    reseed.current = null
    setDraft((current) => ({
      ...current,
      ...Object.fromEntries(names.map((name) => [name, baseline(settings, name)])),
    }))
    setClearing((current) => current.filter((name) => !names.includes(name)))
  }, [settings])

  const value = (name: FieldName): string => draft[name] ?? (settings ? baseline(settings, name) : '')
  const setField = (name: FieldName, next: string) => {
    setDraft((current) => ({ ...current, [name]: next }))
    setErrors((current) => ({ ...current, [name]: undefined }))
    // A key typed after Remove key replaces the stored one rather than clearing it.
    if (isSecret(name) && next !== '') setClearing((current) => current.filter((secret) => secret !== name))
  }

  // #426 — the Bambuddy store needs a SAVED Bambuddy URL and an inbox folder in the form:
  // until then the Blob store choice shows, sends and compares the local store. The URL is
  // the saved one, never Connection's unsaved draft: a Projects & files save cannot commit
  // it, so what the choice shows and what that save sends always agree.
  const savedBambuddyUrl = settings ? baseline(settings, 'bambuddy_url') !== '' : false
  const bambuddyStoreReady = savedBambuddyUrl && value('library_folder_id') !== ''
  const chosenBackend = bambuddyStoreReady ? value('store_backend') : 'local'

  const changed = (name: FieldName): boolean => {
    if (!settings) return false
    if (name === 'store_backend') return chosenBackend !== baseline(settings, name)
    if (isSecret(name)) return value(name) !== '' || clearing.includes(name)
    return value(name) !== baseline(settings, name)
  }
  const sectionDirty = (id: SectionId) => fieldsOf(id, settings).some(changed)
  const dirtySections = SECTIONS.map((section) => section.id).filter(sectionDirty)
  const dirty = dirtySections.length > 0

  const guard = useLeaveGuard(dirty)

  // #269 — the settings changed elsewhere (another tab, an agent). An untouched form
  // follows them; an edited one (`dirty`) is never overwritten, and says so.
  const [changedElsewhere, setChangedElsewhere] = useState(false)
  const loadLatest = () => {
    setChangedElsewhere(false)
    reseed.current = 'all'
    settingsState.refresh()
  }

  // Read when the answer lands, not when the event came: typing may have started since.
  const isDirty = useLatest(() => dirty)
  useSubscription('settings', (signal) => {
    // Saves, resets and Test connection write the settings: that event is this tab's own.
    if (signal === 'resync' || saving !== null || testing || resetting !== null) return
    if (isDirty.current()) {
      setChangedElsewhere(true)
      return
    }
    settingsState.refresh(() => {
      if (!isDirty.current()) {
        reseed.current = 'all'
        return true
      }
      setChangedElsewhere(true)
      return false
    })
  })

  function patchFor(id: SectionId): SettingsUpdate | null {
    if (!settings) return null
    const body: Record<string, unknown> = {}
    const problems: Partial<Record<FieldName, string>> = {}
    for (const name of fieldsOf(id, settings)) {
      if (!changed(name)) continue
      if (name === 'store_backend') {
        body[name] = chosenBackend
        continue
      }
      if (isSecret(name)) {
        // Omitted when untouched, so the stored key is left alone; "" clears it.
        body[name] = value(name)
        continue
      }
      const next = toPatchValue(name, value(name), settings)
      if (isProblem(next)) problems[name] = next.problem
      else body[name] = next
    }
    if (Object.keys(problems).length > 0) {
      setErrors((current) => ({ ...current, ...problems }))
      return null
    }
    // A Connection save that loses the Bambuddy URL takes the store back to local with it,
    // or the server would refuse the save (it never keeps an unready Bambuddy store).
    // Only this save's own change counts: another section's unsaved draft does not.
    if (
      id === 'connection' &&
      changed('bambuddy_url') &&
      value('bambuddy_url') === '' &&
      settings.store_backend === 'bambuddy'
    ) {
      body.store_backend = 'local'
    }
    return body as SettingsUpdate
  }

  /** Save one section; true when it saved, or had nothing to save. */
  async function saveSection(id: SectionId): Promise<boolean> {
    const body = patchFor(id)
    if (body === null) return false
    if (Object.keys(body).length === 0) return true
    setSaving(id)
    setSectionError((current) => ({ ...current, [id]: undefined }))
    try {
      const next = await api.putSettings(body)
      reseed.current = [...fieldsOf(id, next), ...('store_backend' in body ? (['store_backend'] as const) : [])]
      settingsState.setData(next)
      if (id === 'preview') setDisplayUnit(next.display_unit)
      if (id === 'connection') setBambuddyLinks(next)
      setSavedAt((current) => ({ ...current, [id]: new Date().toLocaleTimeString() }))
      if (id === 'connection') {
        targetsState.reload()
        projectsState.reload()
      }
      return true
    } catch (cause) {
      // A 422 names each refused field (`loc: ["body", <field>]`): shown beside it.
      const fieldErrors: Partial<Record<FieldName, string>> = {}
      const reported = cause instanceof ApiError ? (cause.problem as { errors?: unknown }).errors : undefined
      for (const entry of Array.isArray(reported) ? (reported as { loc?: string[]; msg?: string }[]) : []) {
        const name = entry.loc?.[1] as FieldName | undefined
        if (name && entry.msg) fieldErrors[name] = entry.msg.replace(/^Value error, /, '')
      }
      setErrors((current) => ({ ...current, ...fieldErrors }))
      if (Object.keys(fieldErrors).length === 0) {
        setSectionError((current) => ({
          ...current,
          [id]: cause instanceof ApiError ? cause.detail : 'Could not save the settings.',
        }))
      }
      return false
    } finally {
      setSaving(null)
    }
  }

  function discardSection(id: SectionId) {
    if (!settings) return
    const names = fieldsOf(id, settings)
    setDraft((current) => ({
      ...current,
      ...Object.fromEntries(names.map((name) => [name, baseline(settings, name)])),
    }))
    setClearing((current) => current.filter((name) => !names.includes(name)))
    setErrors((current) => ({ ...current, ...Object.fromEntries(names.map((name) => [name, undefined])) }))
    setSectionError((current) => ({ ...current, [id]: undefined }))
  }

  async function resetField(name: FieldName) {
    setResetting(name)
    setError(null)
    try {
      // Resetting what the Bambuddy store needs takes the store back to local with it, as a
      // save that loses them does (patchFor): the server refuses an unready Bambuddy store.
      const fallBack =
        (name === 'bambuddy_url' || name === 'library_folder_id') && settings?.store_backend === 'bambuddy'
      const next = await api.putSettings(fallBack ? { reset: [name], store_backend: 'local' } : { reset: [name] })
      reseed.current = fallBack ? [name, 'store_backend'] : [name]
      settingsState.setData(next)
      setBambuddyLinks(next)
      if (name === 'bambuddy_url' || name === 'bambuddy_api_key') {
        targetsState.reload()
        projectsState.reload()
      }
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'Could not reset the setting.')
    } finally {
      setResetting(null)
    }
  }

  // #254 — the settings form's browser tools. They change the form, never what is stored:
  // Save stays the user's (a settings write is outward, AI design spec §8.1), and the API
  // key is neither readable nor settable here.
  const form = {
    bambuddy_url: 'bambuddy-url',
    public_url: 'public-url',
    library_folder_id: 'library-folder',
    printer_id: 'printer',
    default_plate: 'default-plate',
    display_unit: 'display-unit',
  } as const satisfies Partial<Record<FieldName, string>>
  type FormField = keyof typeof form

  const plate = value('default_plate')
  const choices = {
    library_folder_id: ['', ...(targetsState.data?.folders ?? []).map((folder) => String(folder.id))],
    printer_id: ['', ...(targetsState.data?.printers ?? []).map((printer) => String(printer.id))],
    default_plate: ['', ...plateNames, ...(plate && !plateNames.includes(plate) ? [plate] : [])],
    display_unit: ['mm', 'in'],
  } as Partial<Record<FormField, string[]>>

  function formValues(): Record<FormField, string> {
    return Object.fromEntries(
      (Object.keys(form) as FormField[]).map((field) => [field, value(field)]),
    ) as Record<FormField, string>
  }

  const live = useLatest(formValues)

  useAgentHandlers(
    'settings',
    {
      get_form: () => ({
        values: formValues(),
        unsaved: dirty,
        has_api_key: settings?.has_api_key ?? false,
        api_key_typed: value('bambuddy_api_key').length > 0,
        choices: {
          library_folder_id: (targetsState.data?.folders ?? []).map((folder) => ({ value: String(folder.id), name: folder.name })),
          printer_id: (targetsState.data?.printers ?? []).map((printer) => ({ value: String(printer.id), name: printer.name })),
          default_plate: plateNames,
          display_unit: ['mm', 'in'],
        },
        last_test: test ? { ok: test.ok, detail: test.detail } : null,
        error,
      }),
      set_field: async ({ field, value: next }) => {
        const allowed = choices[field]
        if (allowed && !allowed.includes(next)) {
          throw new AgentToolError(
            'invalid_args',
            `"${next}" is not a choice for ${field}: ${allowed.map((choice) => JSON.stringify(choice)).join(', ')}.`,
          )
        }
        setField(field, next)
        touchAfterRender(() => document.getElementById(form[field]))
        await committed(() => live.current()[field] === next)
        return { field, value: next, saved: false, note: "The user saves each section with its own Save button." }
      },
      test_connection: async () => {
        if (dirty) {
          throw new AgentToolError(
            'refused',
            'The form has unsaved changes, and the test saves the form first. Ask the user to save or test it.',
          )
        }
        // With nothing unsaved the stored settings are the form, so this is Test
        // connection minus its save — the one part that would be a settings write.
        touchAfterRender(() => document.querySelector('[data-testid="test-connection"]'))
        setTesting(true)
        setError(null)
        setTest(null)
        try {
          const result = await api.testSettings()
          setTest(result)
          return { ok: result.ok, detail: result.detail }
        } catch (cause) {
          const message = cause instanceof ApiError ? cause.detail : 'The connection test could not run.'
          setError(message)
          throw new AgentToolError('failed', message)
        } finally {
          setTesting(false)
        }
      },
    },
    () => ({ values: formValues(), unsaved: dirty, has_api_key: settings?.has_api_key ?? false }),
  )

  async function runTest() {
    setTesting(true)
    setError(null)
    setTest(null)
    try {
      // The server tests what it has stored, so save first or the test lags the form.
      if (!(await saveSection('connection'))) return
      setTest(await api.testSettings())
      bambuddyState.reload()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'The connection test could not run.')
    } finally {
      setTesting(false)
    }
  }

  async function addSidebar() {
    setRegistering(true)
    setError(null)
    try {
      setSidebar(await api.registerSidebar())
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'Could not register the sidebar entry.')
    } finally {
      setRegistering(false)
    }
  }

  if (settingsState.loading || !settings) {
    return (
      <p className="flex h-full items-center justify-center gap-2 text-[13px] text-muted">
        <Spinner /> Loading settings
      </p>
    )
  }

  const badge = (name: FieldName) => (
    <SourceBadge
      name={name}
      settings={settings}
      onReset={() => void resetField(name)}
      resetting={resetting === name}
    />
  )

  const clearSecret = (name: Secret) => {
    setClearing((current) => [...current, name])
    setField(name, '')
  }

  const runtimeRow = (spec: FieldSpec) => (
    <FieldRow
      key={spec.name}
      id={`setting-${spec.name}`}
      label={spec.label}
      badge={badge(spec.name)}
      help={spec.help}
      error={errors[spec.name]}
    >
      <RuntimeInput
        spec={spec}
        value={value(spec.name)}
        onChange={(next) => setField(spec.name, next)}
        settings={settings}
        clearing={isSecret(spec.name) && clearing.includes(spec.name)}
        onClear={isSecret(spec.name) ? () => clearSecret(spec.name as Secret) : undefined}
      />
    </FieldRow>
  )

  const runtimeRows = (section: SectionId) => {
    const specs = [...RUNTIME_FIELDS, ...extraSpecs(settings)].filter((spec) => spec.section === section)
    const advanced = specs.filter((spec) => spec.advanced)
    return (
      <>
        {specs.filter((spec) => !spec.advanced).map(runtimeRow)}
        {advanced.length > 0 && (
          <details className="rounded-[6px] border border-line px-3 py-2">
            <summary className="cursor-pointer text-[13px] text-muted">Advanced</summary>
            <div className="mt-3 space-y-4">{advanced.map(runtimeRow)}</div>
          </details>
        )}
      </>
    )
  }

  const saved = (id: SectionId, children: ReactNode, description?: ReactNode) => (
    <Section
      id={id}
      title={sectionTitle(id)}
      description={description}
      dirty={sectionDirty(id)}
      saving={saving === id}
      savedAt={savedAt[id]}
      onSave={() => void saveSection(id)}
      onDiscard={() => discardSection(id)}
    >
      {children}
      {sectionError[id] && (
        <p role="alert" className="text-[13px] text-warn">
          {sectionError[id]}
        </p>
      )}
    </Section>
  )

  const bambuddy = bambuddyState.data
  const projects = projectsState.data?.projects ?? []
  const project = value('last_project_id')
  const unit = value('display_unit') as DisplayUnit

  return (
    <div className="h-full overflow-y-auto">
      <nav aria-label="Settings sections" className="sticky top-0 z-10 border-b border-line bg-bg/95 backdrop-blur">
        <ul className="mx-auto flex max-w-2xl gap-1 overflow-x-auto px-4 py-2 text-[12px]">
          {SECTIONS.map((section) => (
            <li key={section.id} className="shrink-0">
              <a
                href={`#${section.id}`}
                onClick={(event) => {
                  event.preventDefault()
                  document.getElementById(section.id)?.scrollIntoView?.({ block: 'start', behavior: 'smooth' })
                }}
                className="inline-flex items-center gap-1 rounded-[6px] px-2 py-1 text-muted hover:bg-surface-2 hover:text-ink"
              >
                {section.title}
                {sectionDirty(section.id) && (
                  <span aria-label="unsaved" className="size-1.5 rounded-full bg-accent" />
                )}
              </a>
            </li>
          ))}
        </ul>
      </nav>

      <div className="mx-auto max-w-2xl px-4 py-6">
        <h1 className="text-lg font-semibold tracking-tight">Settings</h1>
        <p className="mt-0.5 text-[13px] text-muted">
          Each setting says where its value comes from. A value saved here beats the deployment&rsquo;s{' '}
          <span className="sb-num">SCADBUDDY_*</span> variable until it is reset. The API keys are stored on
          the server and never sent back to the browser.
        </p>

        {changedElsewhere && (
          <div
            role="status"
            className="mt-4 flex items-center gap-3 rounded-[6px] border border-accent/40 bg-accent/8 px-3 py-2 text-[12px]"
          >
            <span>Settings were changed elsewhere. Your unsaved changes here are kept until you load them.</span>
            <Button size="sm" onClick={loadLatest}>
              Load the latest
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setChangedElsewhere(false)}>
              Keep mine
            </Button>
          </div>
        )}

        {error && (
          <p role="alert" className="mt-4 text-[13px] text-warn">
            {error}
          </p>
        )}

        <div className="mt-5">
          {saved(
            'connection',
            <>
              <FieldRow id="bambuddy-url" label="Bambuddy URL" badge={badge('bambuddy_url')} error={errors.bambuddy_url}>
                <input
                  id="bambuddy-url"
                  type="url"
                  value={value('bambuddy_url')}
                  onChange={(event) => setField('bambuddy_url', event.target.value)}
                  placeholder="https://bambuddy.internal.example"
                  className="sb-field sb-num"
                />
              </FieldRow>

              <FieldRow
                id="bambuddy-web-urls"
                label="Bambuddy web URLs"
                badge={badge('bambuddy_web_urls')}
                error={errors.bambuddy_web_urls}
                help="Where browsers reach Bambuddy, when the URL above is one only ScadBuddy's server can. Comma-separated: links use the first, or whichever of them ScadBuddy is opened inside."
              >
                <input
                  id="bambuddy-web-urls"
                  type="text"
                  value={value('bambuddy_web_urls')}
                  onChange={(event) => setField('bambuddy_web_urls', event.target.value)}
                  placeholder="https://bambuddy.example, https://bambuddy.lan"
                  className="sb-field sb-num"
                />
              </FieldRow>

              <FieldRow
                id="bambuddy-key"
                label="API key"
                badge={badge('bambuddy_api_key')}
                error={errors.bambuddy_api_key}
                help="Needs Read Status, Manage Library and Manage Queue; Manage Projects for projects and Manage Archives for print photos."
              >
                <div className="flex gap-2">
                  <input
                    id="bambuddy-key"
                    type="password"
                    value={value('bambuddy_api_key')}
                    autoComplete="off"
                    onChange={(event) => setField('bambuddy_api_key', event.target.value)}
                    placeholder={
                      clearing.includes('bambuddy_api_key')
                        ? 'Cleared when you save.'
                        : settings.has_api_key
                          ? 'A key is stored. Paste a new one to replace it.'
                          : 'Paste the key'
                    }
                    className="sb-field sb-num"
                  />
                  {settings.has_api_key && (
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={clearing.includes('bambuddy_api_key')}
                      onClick={() => clearSecret('bambuddy_api_key')}
                      {...USER_ONLY}
                    >
                      Remove key
                    </Button>
                  )}
                </div>
              </FieldRow>

              <FieldRow
                id="bambuddy-render-key"
                label="Render key"
                badge={badge('bambuddy_render_api_key')}
                error={errors.bambuddy_render_api_key}
                help="A second key with Manage Library only. Render workers run template code and hold this key alone."
              >
                <div className="flex gap-2">
                  <input
                    id="bambuddy-render-key"
                    type="password"
                    value={value('bambuddy_render_api_key')}
                    autoComplete="off"
                    onChange={(event) => setField('bambuddy_render_api_key', event.target.value)}
                    placeholder={
                      clearing.includes('bambuddy_render_api_key')
                        ? 'Cleared when you save.'
                        : settings.has_render_api_key
                          ? 'A key is stored. Paste a new one to replace it.'
                          : 'Paste the key'
                    }
                    className="sb-field sb-num"
                  />
                  {settings.has_render_api_key && (
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={clearing.includes('bambuddy_render_api_key')}
                      onClick={() => clearSecret('bambuddy_render_api_key')}
                      {...USER_ONLY}
                    >
                      Remove key
                    </Button>
                  )}
                </div>
                {settings.render_key_fallback && (
                  <p
                    role="status"
                    data-testid="render-key-fallback"
                    className="mt-1.5 rounded-[6px] border border-warn px-2 py-1.5 text-[12px]"
                  >
                    Render workers hold the full Bambuddy key; template code can print. Create a key with only
                    Manage Library in Bambuddy and paste it above as the render key.
                  </p>
                )}
              </FieldRow>

              <FieldRow
                id="public-url"
                label="ScadBuddy’s own URL"
                badge={badge('public_url')}
                error={errors.public_url}
                help="What Bambuddy links to. ScadBuddy cannot infer it — it sits behind a proxy."
              >
                <input
                  id="public-url"
                  type="url"
                  value={value('public_url')}
                  onChange={(event) => setField('public_url', event.target.value)}
                  placeholder="https://scadbuddy.internal.example"
                  className="sb-field sb-num"
                />
              </FieldRow>

              <div className="space-y-2">
                <div className="flex items-center gap-2">
                  <Button
                    onClick={() => void runTest()}
                    disabled={testing}
                    aria-busy={testing}
                    data-testid="test-connection"
                    {...USER_ONLY}
                  >
                    {testing && <Spinner />}
                    Test connection
                  </Button>
                  {test && (
                    <p role="status" className={`text-[12px] ${test.ok ? 'text-ok' : 'text-warn'}`}>
                      {test.detail}
                    </p>
                  )}
                </div>
                {test && (test.scopes?.length ?? 0) > 0 && (
                  <ul aria-label="Scopes" className="space-y-1 text-[12px]">
                    {test.scopes?.map((check) => (
                      <li key={check.scope} className="flex gap-2">
                        <span
                          aria-hidden
                          className={
                            check.status === 'ok'
                              ? 'text-ok'
                              : check.required && check.status !== 'unknown'
                                ? 'text-warn'
                                : 'text-muted'
                          }
                        >
                          {check.status === 'ok' ? '✓' : check.status === 'missing' ? '✗' : '?'}
                        </span>
                        <span>
                          <span className="font-medium">{check.scope}</span>
                          {!check.required && <span className="text-muted"> (optional)</span>}
                          {': '}
                          <span className="text-muted">
                            {check.status === 'ok'
                              ? 'granted. '
                              : check.status === 'missing'
                                ? 'missing. '
                                : check.status === 'unknown'
                                  ? 'not checked. '
                                  : 'could not check. '}
                            {check.detail}
                          </span>
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>

              {connected && (
                <div className="rounded-[6px] border border-line px-3 py-2 text-[12px]" data-testid="bambuddy-status">
                  {bambuddyState.loading ? (
                    <span className="flex items-center gap-2 text-muted">
                      <Spinner /> Reading Bambuddy
                    </span>
                  ) : bambuddyState.error ? (
                    <span className="text-muted">
                      Could not read Bambuddy:{' '}
                      {bambuddyState.error instanceof ApiError ? bambuddyState.error.detail : 'no answer'}
                    </span>
                  ) : (
                    <div className="space-y-1">
                      <p>
                        Finish photo in Bambuddy:{' '}
                        <span className={bambuddy?.capture_finish_photo === false ? 'text-warn' : ''}>
                          {bambuddy?.capture_finish_photo === true
                            ? 'on'
                            : bambuddy?.capture_finish_photo === false
                              ? 'off'
                              : 'unknown'}
                        </span>
                        {bambuddy?.capture_finish_photo !== true && bambuddy?.settings_url && (
                          <>
                            {' · '}
                            <a
                              href={bambuddy.settings_url}
                              target="_blank"
                              rel="noreferrer"
                              className="text-accent underline-offset-2 hover:underline"
                            >
                              Turn it on in Bambuddy
                            </a>
                          </>
                        )}
                      </p>
                      <p className="text-muted">
                        Bambuddy photographs a print when it finishes, and print history shows the photo.
                        ScadBuddy only reads this setting.{bambuddy?.detail ? ` ${bambuddy.detail}` : ''}
                      </p>
                    </div>
                  )}
                </div>
              )}

              <div className="border-t border-line pt-4">
                <p className="text-[13px] text-muted">
                  Adds ScadBuddy to Bambuddy&rsquo;s sidebar as an External Link called &ldquo;ScadBuddy&rdquo;,
                  opening inside Bambuddy rather than a new tab, at ScadBuddy&rsquo;s own URL. Running it again
                  updates the existing entry.
                </p>
                <div className="mt-2 flex items-center gap-2">
                  <Button onClick={() => void addSidebar()} disabled={registering} aria-busy={registering} {...USER_ONLY}>
                    {registering && <Spinner />}
                    Add to Bambuddy sidebar
                  </Button>
                  {sidebar && (
                    <p role="status" className="text-[12px] text-ok">
                      {sidebar.created ? 'Added to the sidebar' : 'Updated the sidebar entry'} at{' '}
                      <span className="sb-num">{sidebar.embed_path}</span>.
                    </p>
                  )}
                </div>
              </div>
            </>,
            'How ScadBuddy reaches Bambuddy, and how Bambuddy reaches ScadBuddy.',
          )}

          {saved(
            'printing',
            <>
              <FieldRow id="printer" label="Printer">
                <select
                  id="printer"
                  value={value('printer_id')}
                  onChange={(event) => setField('printer_id', event.target.value)}
                  className="sb-field cursor-pointer"
                >
                  <option value="">None</option>
                  {(targetsState.data?.printers ?? []).map((printer) => (
                    <option key={printer.id} value={printer.id}>
                      {printer.name}
                      {printer.model ? ` (${printer.model})` : ''}
                    </option>
                  ))}
                </select>
              </FieldRow>
            </>,
          )}

          {saved(
            'projects',
            <>
              <FieldRow
                id="library-folder"
                label="Inbox folder, for sends without a project"
                help="Where a 3MF goes when it is sent without a project. A send to a project uses the project's own folder."
              >
                <select
                  id="library-folder"
                  value={value('library_folder_id')}
                  onChange={(event) => setField('library_folder_id', event.target.value)}
                  className="sb-field cursor-pointer"
                >
                  <option value="">Library root</option>
                  {(targetsState.data?.folders ?? []).map((folder) => (
                    <option key={folder.id} value={folder.id}>
                      {folder.name}
                    </option>
                  ))}
                </select>
              </FieldRow>

              <FieldRow
                id="default-project"
                label="Default project"
                help={
                  projectsState.error
                    ? `Could not list the projects: ${projectsState.error instanceof ApiError ? projectsState.error.detail : 'no answer'}`
                    : 'Where the project picker opens, and the project a send without one is filed under.'
                }
              >
                <select
                  id="default-project"
                  value={project}
                  onChange={(event) => setField('last_project_id', event.target.value)}
                  className="sb-field cursor-pointer"
                >
                  <option value="">No project</option>
                  {project && !projects.some((entry) => String(entry.id) === project) && (
                    <option value={project}>Project {project}</option>
                  )}
                  {projects.map((entry) => (
                    <option key={entry.id} value={entry.id}>
                      {entry.name}
                    </option>
                  ))}
                </select>
              </FieldRow>

              <FieldRow
                id="store-backend"
                label="Blob store"
                badge={badge('store_backend')}
                error={errors.store_backend}
                help="Takes effect when ScadBuddy and its render workers restart."
              >
                <select
                  id="store-backend"
                  value={chosenBackend}
                  onChange={(event) => setField('store_backend', event.target.value)}
                  className="sb-field cursor-pointer"
                >
                  <option value="local">This server&rsquo;s volume (one render worker)</option>
                  <option value="bambuddy" disabled={!bambuddyStoreReady}>
                    Bambuddy library (any number of render workers)
                  </option>
                </select>
                {!bambuddyStoreReady && (
                  <p className="mt-1.5 text-[12px] text-muted" data-testid="store-backend-hint">
                    {!savedBambuddyUrl && value('bambuddy_url') !== ''
                      ? 'The Bambuddy URL is not saved yet: save Connection to choose the Bambuddy library.'
                      : 'The Bambuddy library needs a saved Bambuddy URL and an inbox folder.'}
                  </p>
                )}
              </FieldRow>

              {storeUsage && (
                <div>
                  <p className="text-[13px]">Blob store usage</p>
                  <dl className="mt-1.5 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[13px]" data-testid="store-usage">
                    <dt className="text-muted">Where</dt>
                    <dd>{storeUsage.backend === 'bambuddy' ? 'Bambuddy library' : 'This server’s volume'}</dd>
                    <dt className="text-muted">Files</dt>
                    <dd className="sb-num">{ofLimit(String(storeUsage.count), storeUsage.max_count, String)}</dd>
                    <dt className="text-muted">Size</dt>
                    <dd className="sb-num">
                      {ofLimit(formatBytes(storeUsage.bytes), storeUsage.max_total_bytes, formatBytes)}
                    </dd>
                  </dl>
                  <p className="mt-1.5 text-[12px] text-muted">
                    The Where row is the store this process uses; it moves to the Blob store choice above at its
                    next restart, so the two can differ until then.
                  </p>
                  <p className="mt-1.5 text-[12px] text-muted">
                    Rendered pieces, template snapshots, uploaded SVGs and PNGs, and downloaded fonts. What no job,
                    output or preset uses is removed once unused for the sweep&rsquo;s grace period (a week by
                    default). Past either limit, new files are refused.
                  </p>
                </div>
              )}
            </>,
          )}

          {saved(
            'uploads',
            <>
              {runtimeRows('uploads')}
              {usage && (
                <div>
                  <p className="text-[13px]">Uploaded files</p>
                  <dl className="mt-1.5 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[13px]" data-testid="asset-usage">
                    <dt className="text-muted">Files</dt>
                    <dd className="sb-num">{ofLimit(String(usage.count), usage.max_count, String)}</dd>
                    <dt className="text-muted">Size</dt>
                    <dd className="sb-num">{ofLimit(formatBytes(usage.bytes), usage.max_total_bytes, formatBytes)}</dd>
                  </dl>
                  <p className="mt-1.5 text-[12px] text-muted">
                    The SVGs and PNGs attached to file parameters. Past either limit, a new upload is refused.
                  </p>
                </div>
              )}
            </>,
          )}

          {saved('rendering', runtimeRows('rendering'))}

          {saved('fonts', runtimeRows('fonts'))}

          <Section
            id="libraries"
            title={sectionTitle('libraries')}
            description="Move a library to another tag or branch across the models that pin it. Check each model against the candidate first; each ticked model is then re-pinned on its own, as one revision of that model."
          >
            <LibraryUpgrade />
          </Section>

          {saved(
            'preview',
            <>
              <FieldRow
                id="display-unit"
                label="Show dimensions in"
                help="For every model: the bounding box, plate sizes and fit warnings. Models, parameters and the files sent to Bambuddy stay in millimetres."
              >
                <select
                  id="display-unit"
                  value={value('display_unit')}
                  onChange={(event) => setField('display_unit', event.target.value)}
                  className="sb-field cursor-pointer"
                >
                  <option value="mm">Millimetres (mm)</option>
                  <option value="in">Inches (in)</option>
                </select>
              </FieldRow>

              <FieldRow
                id="default-plate"
                label="Default plate"
                badge={badge('default_plate')}
                error={errors.default_plate}
                help="The plate the customizer draws and checks the model against until a printer is chosen in the print picker."
              >
                <select
                  id="default-plate"
                  value={plate}
                  onChange={(event) => setField('default_plate', event.target.value)}
                  className="sb-field cursor-pointer"
                >
                  <option value="">{plateSize([256, 256], unit)}</option>
                  {/* A value set through SCADBUDDY_DEFAULT_PLATE may be a code ("A1M"). */}
                  {plate && !plateNames.includes(plate) && <option value={plate}>{plate}</option>}
                  {(platesState.data?.plates ?? []).map((entry) => (
                    <option key={entry.name} value={entry.name}>
                      {entry.name} ({plateSize(entry.size, unit)})
                    </option>
                  ))}
                </select>
              </FieldRow>
            </>,
          )}

          <Section
            id="remembered"
            title={sectionTitle('remembered')}
            description="What the print dialog remembers per model and per printer. Forgetting one leaves the rest; the dialog then opens on its own defaults."
          >
            <RememberedChoicesPanel targets={targetsState.data} />
          </Section>

          <Section id="assistant" title={sectionTitle('assistant')} description="Applied at once; not part of any saved section.">
            <AiStatusSection />
            {/* Per browser and applied at once. Only the user may flip it: an agent must
                not grant itself access (#254). */}
            <div>
              <label className="flex items-start gap-2 text-[13px]" {...USER_ONLY}>
                <input
                  type="checkbox"
                  checked={webMcp}
                  onChange={(event) => setWebMcpEnabled(event.target.checked)}
                  className="mt-0.5"
                  aria-describedby="webmcp-help"
                />
                Let this browser&rsquo;s built-in agent use ScadBuddy tools (WebMCP)
              </label>
              <p id="webmcp-help" className="mt-1.5 text-[12px] text-muted">
                Off by default, as AI design spec §8.5 has an outside agent pair before it drives a tab. Even
                when on, the print and send tool can only open the dialog; you confirm it.
              </p>
            </div>
            <HeadlessBrowserSetting />
            <HttpRequestSetting />
            {/* Saves on its own (#790); hidden without the agent's database, like the switch above. */}
            <SessionLimitsSetting />
            {/* The agent service serves these routes, so they show only where the assistant
                would (#251): when the agent answers /api/v1/ai/status as available
                (useAiAvailability). */}
            {ai.available && (
              <>
                <McpAuthSection onSaved={(setting) => setMcpAuthMode(setting.mode)} />
                <McpTokensSection authMode={mcpAuthMode} />
              </>
            )}
            {ai.available && (
              <div>
                <p className="text-[13px]">MCP sign-in (OIDC)</p>
                {/* Saved on its own: the agent service owns it, not the backend's settings. */}
                <McpOidcSettings />
              </div>
            )}
            {/* Each action is its own request, applied at once. */}
            {ai.available && (
              <div>
                <p className="text-[13px]">Assistant plugins</p>
                <p className="mt-0.5 text-[12px] text-muted">
                  What the assistant can load besides ScadBuddy&rsquo;s own tools. Each change applies at once.
                </p>
                <PluginPackagesPanel />
                <RemotePluginsPanel />
              </div>
            )}
            {/* Saves on its own (#258); shown only when the assistant is available. */}
            <AiAuditSection />
          </Section>

          {saved('diagnostics', runtimeRows('diagnostics'))}

          <Section id="about" title={sectionTitle('about')}>
            <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[13px]" data-testid="about">
              <dt className="text-muted">ScadBuddy</dt>
              <dd className="sb-num">
                {settings.about?.version ?? 'unknown'} ({settings.about?.revision ?? 'unknown'})
              </dd>
              <dt className="text-muted">OpenSCAD</dt>
              <dd className="sb-num">{settings.about?.openscad_version ?? 'not found'}</dd>
              <dt className="text-muted">Bambuddy</dt>
              <dd className="sb-num">{connected ? (bambuddy?.version ?? 'unknown') : 'not connected'}</dd>
            </dl>
            <div>
              <p className="text-[13px]">Set by the deployment only</p>
              <p className="mt-0.5 text-[12px] text-muted">
                These cannot be changed here. Each is its <span className="sb-num">SCADBUDDY_*</span> variable or
                the image&rsquo;s default.
              </p>
              <table className="mt-2 w-full text-left text-[12px]" aria-label="Deployment values">
                <tbody>
                  {(settings.bootstrap ?? []).map((entry) => (
                    <tr key={entry.name} className="border-t border-line align-top">
                      <th scope="row" className="sb-num py-1.5 pr-3 font-normal">
                        {entry.env_var}
                      </th>
                      <td className="py-1.5">
                        <span className="sb-num">{entry.value ?? 'unset'}</span>{' '}
                        <span className="text-muted">({entry.source === 'env' ? 'set' : 'default'})</span>
                        <p className="mt-0.5 text-muted">{entry.reason}</p>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Section>
        </div>
      </div>

      <Dialog
        open={guard.pending !== null}
        title="Leave without saving?"
        description={`Unsaved changes in ${dirtySections.map(sectionTitle).join(', ')} will be lost.`}
        onClose={guard.stay}
        footer={
          <>
            <Button variant="ghost" onClick={guard.stay}>
              Stay
            </Button>
            <Button variant="danger" onClick={guard.leave}>
              Leave without saving
            </Button>
          </>
        }
      >
        <p className="text-[13px] text-muted">Save each section first to keep its changes.</p>
      </Dialog>
    </div>
  )
}
