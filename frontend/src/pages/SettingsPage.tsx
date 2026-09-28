import { useEffect, useState } from 'react'
import { useAiAvailability } from '../agent/chat/availability'
import { USER_ONLY } from '../agent/dom'
import { committed, touchAfterRender } from '../agent/highlight'
import { AgentToolError } from '../agent/types'
import { useAgentHandlers, useLatest } from '../agent/useAgentHandlers'
import { setWebMcpEnabled, useWebMcpEnabled } from '../agent/webmcpPreference'
import { api, ApiError } from '../api/client'
import type { ConnectionTest, SettingsUpdate, SidebarLink } from '../api/types'
import { AiStatusSection } from '../components/assistant/AiStatusSection'
import { McpOidcSettings } from '../components/McpOidcSettings'
import { HeadlessBrowserSetting } from '../components/HeadlessBrowserSetting'
import { PluginPackagesPanel } from '../components/settings/PluginPackages'
import { RemotePluginsPanel } from '../components/settings/RemotePlugins'
import { McpTokensSection } from '../components/McpTokensSection'
import { Button } from '../components/ui/Button'
import { Spinner } from '../components/ui/Spinner'
import { useSubscription } from '../lib/realtime'
import { useAsync } from '../lib/useAsync'
import { plateSize, setDisplayUnit, type DisplayUnit } from '../lib/units'

/** `<select>`/`<input>` values are strings; Bambuddy's ids are integers. */
function asId(value: string): number | null {
  return value === '' ? null : Number(value)
}

function idValue(id: number | null | undefined): string {
  return id === null || id === undefined ? '' : String(id)
}

/** Decimal units, as the server's caps are written (1 GB = 1 000 000 000 bytes). */
function formatBytes(bytes: number): string {
  if (bytes < 1000) return `${bytes} B`
  const units = ['kB', 'MB', 'GB', 'TB']
  let value = bytes / 1000
  let unit = 0
  while (value >= 1000 && unit < units.length - 1) {
    value /= 1000
    unit += 1
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`
}

/** #296 — `used` of `limit`, where a limit of 0 means none. */
function ofLimit(used: string, limit: number, format: (n: number) => string): string {
  return limit > 0 ? `${used} of ${format(limit)}` : `${used} (no limit)`
}

export function SettingsPage() {
  const settingsState = useAsync(() => api.getSettings(), [])
  // The agent-service sections render only where the agent is (#256, #261).
  const ai = useAiAvailability()

  const [url, setUrl] = useState('')
  const [publicUrl, setPublicUrl] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [folderId, setFolderId] = useState('')
  const [pipelineId, setPipelineId] = useState('')
  const [printerId, setPrinterId] = useState('')
  const [defaultPlate, setDefaultPlate] = useState('')
  const [unit, setUnit] = useState<DisplayUnit>('mm')

  const [saving, setSaving] = useState(false)
  const [savedAt, setSavedAt] = useState<string | null>(null)
  const [testing, setTesting] = useState(false)
  const [test, setTest] = useState<ConnectionTest | null>(null)
  const [registering, setRegistering] = useState(false)
  const [sidebar, setSidebar] = useState<SidebarLink | null>(null)
  const [error, setError] = useState<string | null>(null)

  const settings = settingsState.data
  const webMcp = useWebMcpEnabled()
  const connected = Boolean(settings?.bambuddy_url)
  // #81 — needs no Bambuddy: the plates are ScadBuddy's own table.
  const platesState = useAsync(() => api.listPlates(), [])
  const usage = useAsync(() => api.getAssetUsage(), []).data
  const plateNames = (platesState.data?.plates ?? []).map((plate) => plate.name)

  // The pickers need a live Bambuddy, so they are only fetched once one is configured.
  const targetsState = useAsync(
    () => (connected ? api.getBambuddyTargets() : Promise.resolve(null)),
    [connected],
  )

  useEffect(() => {
    if (!settings) return
    setUrl(settings.bambuddy_url ?? '')
    setPublicUrl(settings.public_url ?? '')
    setFolderId(idValue(settings.library_folder_id))
    setPipelineId(idValue(settings.pipeline_id))
    setPrinterId(idValue(settings.printer_id))
    setDefaultPlate(settings.default_plate ?? '')
    setUnit(settings.display_unit)
  }, [settings])

  // #269 — the settings changed elsewhere (another tab, an agent). An untouched form
  // follows them; an edited one (`dirty`, below) is never overwritten, and says so.
  const [changedElsewhere, setChangedElsewhere] = useState(false)
  const loadLatest = () => {
    setChangedElsewhere(false)
    setApiKey('')
    settingsState.refresh()
  }

  function draft(): SettingsUpdate {
    const body: SettingsUpdate = {
      bambuddy_url: url,
      public_url: publicUrl || null,
      library_folder_id: asId(folderId),
      pipeline_id: asId(pipelineId),
      printer_id: asId(printerId),
      default_plate: defaultPlate || null,
      display_unit: unit,
    }
    // Omitted entirely, so an unchanged field leaves the stored key alone.
    if (apiKey.length > 0) body.bambuddy_api_key = apiKey
    return body
  }

  // #254 — the settings form's browser tools. They change the form, never what is stored:
  // Save stays the user's (a settings write is outward, AI design spec §8.1), and the API
  // key is neither readable nor settable here.
  const form = {
    bambuddy_url: [url, setUrl, 'bambuddy-url'],
    public_url: [publicUrl, setPublicUrl, 'public-url'],
    library_folder_id: [folderId, setFolderId, 'library-folder'],
    pipeline_id: [pipelineId, setPipelineId, 'slicer-pipeline'],
    printer_id: [printerId, setPrinterId, 'printer'],
    default_plate: [defaultPlate, setDefaultPlate, 'default-plate'],
    display_unit: [unit, (next: string) => setUnit(next as DisplayUnit), 'display-unit'],
  } as const satisfies Record<string, readonly [string, (next: string) => void, string]>

  const choices = {
    library_folder_id: ['', ...(targetsState.data?.folders ?? []).map((folder) => String(folder.id))],
    pipeline_id: ['', ...(targetsState.data?.pipelines ?? []).map((pipeline) => String(pipeline.id))],
    printer_id: ['', ...(targetsState.data?.printers ?? []).map((printer) => String(printer.id))],
    default_plate: ['', ...plateNames, ...(defaultPlate && !plateNames.includes(defaultPlate) ? [defaultPlate] : [])],
    display_unit: ['mm', 'in'],
  } as Partial<Record<keyof typeof form, string[]>>

  // Unsaved: the form differs from what the server has, or a key has been typed.
  const dirty =
    apiKey.length > 0 ||
    (settings !== undefined &&
      (url !== (settings.bambuddy_url ?? '') ||
        publicUrl !== (settings.public_url ?? '') ||
        folderId !== idValue(settings.library_folder_id) ||
        pipelineId !== idValue(settings.pipeline_id) ||
        printerId !== idValue(settings.printer_id) ||
        defaultPlate !== (settings.default_plate ?? '') ||
        unit !== settings.display_unit))

  // Read when the answer lands, not when the event came: typing may have started since.
  const isDirty = useLatest(() => dirty)
  useSubscription('settings', (signal) => {
    // Save and Test connection both write the settings: that event is this tab's own.
    if (signal === 'resync' || saving || testing) return
    if (isDirty.current()) {
      setChangedElsewhere(true)
      return
    }
    settingsState.refresh(() => {
      if (!isDirty.current()) return true
      setChangedElsewhere(true)
      return false
    })
  })

  function formValues() {
    return Object.fromEntries(Object.entries(form).map(([field, [value]]) => [field, value]))
  }

  const live = useLatest(formValues)

  useAgentHandlers(
    'settings',
    {
      get_form: () => ({
        values: formValues(),
        unsaved: dirty,
        has_api_key: settings?.has_api_key ?? false,
        api_key_typed: apiKey.length > 0,
        choices: {
          library_folder_id: (targetsState.data?.folders ?? []).map((folder) => ({ value: String(folder.id), name: folder.name })),
          pipeline_id: (targetsState.data?.pipelines ?? []).map((pipeline) => ({ value: String(pipeline.id), name: pipeline.name })),
          printer_id: (targetsState.data?.printers ?? []).map((printer) => ({ value: String(printer.id), name: printer.name })),
          default_plate: plateNames,
          display_unit: ['mm', 'in'],
        },
        last_test: test ? { ok: test.ok, detail: test.detail } : null,
        error,
      }),
      set_field: async ({ field, value }) => {
        const allowed = choices[field]
        if (allowed && !allowed.includes(value)) {
          throw new AgentToolError(
            'invalid_args',
            `"${value}" is not a choice for ${field}: ${allowed.map((choice) => JSON.stringify(choice)).join(', ')}.`,
          )
        }
        const [, set, id] = form[field]
        set(value)
        touchAfterRender(() => document.getElementById(id))
        await committed(() => live.current()[field] === value)
        return { field, value, saved: false, note: 'The user saves the form with Save changes.' }
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

  async function save() {
    setSaving(true)
    setError(null)
    try {
      const next = await api.putSettings(draft())
      settingsState.setData(next)
      setDisplayUnit(next.display_unit)
      setApiKey('')
      setSavedAt(new Date().toLocaleTimeString())
      targetsState.reload()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'Could not save the settings.')
    } finally {
      setSaving(false)
    }
  }

  async function runTest() {
    setTesting(true)
    setError(null)
    setTest(null)
    try {
      // The server tests what it has stored, so save first or the test lags the form.
      settingsState.setData(await api.putSettings(draft()))
      setApiKey('')
      setTest(await api.testSettings())
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

  if (settingsState.loading) {
    return (
      <p className="flex h-full items-center justify-center gap-2 text-[13px] text-muted">
        <Spinner /> Loading settings
      </p>
    )
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-2xl px-4 py-6">
        <h1 className="text-lg font-semibold tracking-tight">Settings</h1>
        <p className="mt-0.5 text-[13px] text-muted">
          How ScadBuddy reaches Bambuddy. The API key is stored on the server and never sent
          back to the browser.
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

        <section className="mt-5 rounded-[6px] border border-line bg-surface">
          <h2 className="border-b border-line px-4 py-2.5 text-[13px] font-medium">Connection</h2>
          <div className="space-y-4 p-4">
            <div>
              <label htmlFor="bambuddy-url" className="block text-[13px]">
                Bambuddy URL
              </label>
              <input
                id="bambuddy-url"
                type="url"
                value={url}
                onChange={(event) => setUrl(event.target.value)}
                placeholder="https://bambuddy.internal.example"
                className="sb-field sb-num mt-1.5"
              />
            </div>

            <div>
              <label htmlFor="bambuddy-key" className="block text-[13px]">
                API key
              </label>
              <input
                id="bambuddy-key"
                type="password"
                value={apiKey}
                autoComplete="off"
                onChange={(event) => setApiKey(event.target.value)}
                placeholder={
                  settings?.has_api_key
                    ? 'A key is stored. Paste a new one to replace it.'
                    : 'Paste the key'
                }
                className="sb-field sb-num mt-1.5"
              />
              <p className="mt-1.5 text-[12px] text-muted">
                Needs Manage Library and Manage Queue; Read Status lets ScadBuddy list printers.
              </p>
            </div>

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
          </div>
        </section>

        <section className="mt-4 rounded-[6px] border border-line bg-surface">
          <h2 className="border-b border-line px-4 py-2.5 text-[13px] font-medium">
            Where files go
          </h2>
          <div className="space-y-4 p-4">
            <div>
              <label htmlFor="library-folder" className="block text-[13px]">
                Library folder
              </label>
              <select
                id="library-folder"
                value={folderId}
                onChange={(event) => setFolderId(event.target.value)}
                className="sb-field mt-1.5 cursor-pointer"
              >
                <option value="">Library root</option>
                {(targetsState.data?.folders ?? []).map((folder) => (
                  <option key={folder.id} value={folder.id}>
                    {folder.name}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label htmlFor="slicer-pipeline" className="block text-[13px]">
                Slicer pipeline
              </label>
              <select
                id="slicer-pipeline"
                value={pipelineId}
                onChange={(event) => setPipelineId(event.target.value)}
                className="sb-field mt-1.5 cursor-pointer"
              >
                <option value="">None — slice with the presets below</option>
                {(targetsState.data?.pipelines ?? []).map((pipeline) => (
                  <option key={pipeline.id} value={pipeline.id}>
                    {pipeline.name}
                  </option>
                ))}
              </select>
              <p className="mt-1.5 text-[12px] text-muted">
                The fallback for &ldquo;Slice and queue&rdquo; and for Print. A model given its
                own pipeline in the print picker uses that instead. Without either, ScadBuddy
                slices with the stored presets and queues to the printer below.
              </p>
            </div>

            <div>
              <label htmlFor="printer" className="block text-[13px]">
                Printer
              </label>
              <select
                id="printer"
                value={printerId}
                onChange={(event) => setPrinterId(event.target.value)}
                className="sb-field mt-1.5 cursor-pointer"
              >
                <option value="">None</option>
                {(targetsState.data?.printers ?? []).map((printer) => (
                  <option key={printer.id} value={printer.id}>
                    {printer.name}
                    {printer.model ? ` (${printer.model})` : ''}
                  </option>
                ))}
              </select>
            </div>
          </div>
        </section>

        <section className="mt-4 rounded-[6px] border border-line bg-surface">
          <h2 className="border-b border-line px-4 py-2.5 text-[13px] font-medium">Preview</h2>
          <div className="space-y-4 p-4">
            <div>
              <label htmlFor="display-unit" className="block text-[13px]">
                Show dimensions in
              </label>
              <select
                id="display-unit"
                value={unit}
                onChange={(event) => setUnit(event.target.value as DisplayUnit)}
                className="sb-field mt-1.5 cursor-pointer"
              >
                <option value="mm">Millimetres (mm)</option>
                <option value="in">Inches (in)</option>
              </select>
              <p className="mt-1.5 text-[12px] text-muted">
                For every model: the bounding box, plate sizes and fit warnings. Models,
                parameters and the files sent to Bambuddy stay in millimetres.
              </p>
            </div>

            <div>
              <label htmlFor="default-plate" className="block text-[13px]">
                Default plate
              </label>
              <select
                id="default-plate"
                value={defaultPlate}
                onChange={(event) => setDefaultPlate(event.target.value)}
                className="sb-field mt-1.5 cursor-pointer"
              >
                <option value="">{plateSize([256, 256], unit)}</option>
                {/* A value set through SCADBUDDY_DEFAULT_PLATE may be a code ("A1M"). */}
                {defaultPlate && !plateNames.includes(defaultPlate) && (
                  <option value={defaultPlate}>{defaultPlate}</option>
                )}
                {(platesState.data?.plates ?? []).map((plate) => (
                  <option key={plate.name} value={plate.name}>
                    {plate.name} ({plateSize(plate.size, unit)})
                  </option>
                ))}
              </select>
              <p className="mt-1.5 text-[12px] text-muted">
                The plate the customizer draws and checks the model against until a printer
                is chosen in the print picker.
              </p>
            </div>
          </div>
        </section>

        {usage && (
          <section className="mt-4 rounded-[6px] border border-line bg-surface">
            <h2 className="border-b border-line px-4 py-2.5 text-[13px] font-medium">
              Uploaded files
            </h2>
            <div className="p-4">
              <dl
                className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[13px]"
                data-testid="asset-usage"
              >
                <dt className="text-muted">Files</dt>
                <dd className="sb-num">{ofLimit(String(usage.count), usage.max_count, String)}</dd>
                <dt className="text-muted">Size</dt>
                <dd className="sb-num">
                  {ofLimit(formatBytes(usage.bytes), usage.max_total_bytes, formatBytes)}
                </dd>
              </dl>
              <p className="mt-1.5 text-[12px] text-muted">
                The SVGs and PNGs attached to file parameters. One that no saved output, preset
                or render uses is removed once it has gone unused for the sweep&rsquo;s grace
                period (a week by default). Past either limit, a new upload is refused.
              </p>
            </div>
          </section>
        )}

        <section className="mt-4 rounded-[6px] border border-line bg-surface">
          <h2 className="border-b border-line px-4 py-2.5 text-[13px] font-medium">
            Browser agent
          </h2>
          <div className="p-4">
            {/* Per browser and applied at once, so it is not part of the saved form. Only
                the user may flip it: an agent must not grant itself access (#254). */}
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
              Off by default, as AI design spec §8.5 has an outside agent pair before it drives
              a tab. Even when on, the print and send tool can only open the dialog; you confirm it.
            </p>
          </div>
        </section>

        <AiStatusSection />
        <HeadlessBrowserSetting />

        {/* Applied at once, not part of the saved form (#251). The agent service serves
            these routes, so the section shows only where the assistant would: when the
            agent answers /api/v1/ai/status as available (useAiAvailability). */}
        {ai.available && <McpTokensSection />}

        {ai.available && (
          <section className="mt-4 rounded-[6px] border border-line bg-surface">
            <h2 className="border-b border-line px-4 py-2.5 text-[13px] font-medium">
              MCP sign-in (OIDC)
            </h2>
            <div className="p-4">
              {/* Saved on its own: the agent service owns it, not the backend's settings. */}
              <McpOidcSettings />
            </div>
          </section>
        )}

        <section className="mt-4 rounded-[6px] border border-line bg-surface">
          <h2 className="border-b border-line px-4 py-2.5 text-[13px] font-medium">
            Bambuddy sidebar
          </h2>
          <div className="space-y-4 p-4">
            <div>
              <label htmlFor="public-url" className="block text-[13px]">
                ScadBuddy&rsquo;s own URL
              </label>
              <input
                id="public-url"
                type="url"
                value={publicUrl}
                onChange={(event) => setPublicUrl(event.target.value)}
                placeholder="https://scadbuddy.internal.example"
                className="sb-field sb-num mt-1.5"
              />
              <p className="mt-1.5 text-[12px] text-muted">
                What Bambuddy links to. ScadBuddy cannot infer it — it sits behind a proxy.
              </p>
            </div>

            <p className="text-[13px] text-muted">
              Adds ScadBuddy to Bambuddy&rsquo;s sidebar as an External Link called
              &ldquo;ScadBuddy&rdquo;, opening inside Bambuddy rather than a new tab. Running it
              again updates the existing entry.
            </p>
            <div className="flex items-center gap-2">
              <Button
                onClick={() => void addSidebar()}
                disabled={registering}
                aria-busy={registering}
                {...USER_ONLY}
              >
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
        </section>

        {error && (
          <p role="alert" className="mt-4 text-[13px] text-warn">
            {error}
          </p>
        )}

        <div className="mt-5 flex items-center gap-3">
          <Button
            variant="primary"
            onClick={() => void save()}
            disabled={saving}
            aria-busy={saving}
            {...USER_ONLY}
          >
            {saving && <Spinner />}
            Save changes
          </Button>
          {savedAt && <span className="text-[12px] text-ok">Saved at {savedAt}</span>}
        </div>

        {/* Applied as you go, not by Save changes: each action is its own request. */}
        {ai.available && (
          <div className="mt-8">
            <h2 className="text-[15px] font-semibold tracking-tight">Assistant plugins</h2>
            <p className="mt-0.5 text-[13px] text-muted">
              What the assistant can load besides ScadBuddy&rsquo;s own tools. Each change applies at once.
            </p>
            <PluginPackagesPanel />
            <RemotePluginsPanel />
          </div>
        )}
      </div>
    </div>
  )
}
