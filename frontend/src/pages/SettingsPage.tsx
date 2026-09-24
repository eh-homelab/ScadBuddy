import { useEffect, useState } from 'react'
import { api, ApiError } from '../api/client'
import type { ConnectionTest, SettingsUpdate, SidebarLink } from '../api/types'
import { Button } from '../components/ui/Button'
import { Spinner } from '../components/ui/Spinner'
import { useAsync } from '../lib/useAsync'

/** `<select>`/`<input>` values are strings; Bambuddy's ids are integers. */
function asId(value: string): number | null {
  return value === '' ? null : Number(value)
}

function idValue(id: number | null | undefined): string {
  return id === null || id === undefined ? '' : String(id)
}

export function SettingsPage() {
  const settingsState = useAsync(() => api.getSettings(), [])

  const [url, setUrl] = useState('')
  const [publicUrl, setPublicUrl] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [folderId, setFolderId] = useState('')
  const [pipelineId, setPipelineId] = useState('')
  const [printerId, setPrinterId] = useState('')

  const [saving, setSaving] = useState(false)
  const [savedAt, setSavedAt] = useState<string | null>(null)
  const [testing, setTesting] = useState(false)
  const [test, setTest] = useState<ConnectionTest | null>(null)
  const [registering, setRegistering] = useState(false)
  const [sidebar, setSidebar] = useState<SidebarLink | null>(null)
  const [error, setError] = useState<string | null>(null)

  const settings = settingsState.data
  const connected = Boolean(settings?.bambuddy_url)

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
  }, [settings])

  function draft(): SettingsUpdate {
    const body: SettingsUpdate = {
      bambuddy_url: url,
      public_url: publicUrl || null,
      library_folder_id: asId(folderId),
      pipeline_id: asId(pipelineId),
      printer_id: asId(printerId),
    }
    // Omitted entirely, so an unchanged field leaves the stored key alone.
    if (apiKey.length > 0) body.bambuddy_api_key = apiKey
    return body
  }

  async function save() {
    setSaving(true)
    setError(null)
    try {
      const next = await api.putSettings(draft())
      settingsState.setData(next)
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
      await api.putSettings(draft())
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
              <Button onClick={() => void runTest()} disabled={testing} aria-busy={testing}>
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
              &ldquo;Customize&rdquo;, opening inside Bambuddy rather than a new tab. Running it
              again updates the existing entry.
            </p>
            <div className="flex items-center gap-2">
              <Button
                onClick={() => void addSidebar()}
                disabled={registering}
                aria-busy={registering}
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
          <Button variant="primary" onClick={() => void save()} disabled={saving} aria-busy={saving}>
            {saving && <Spinner />}
            Save changes
          </Button>
          {savedAt && <span className="text-[12px] text-ok">Saved at {savedAt}</span>}
        </div>
      </div>
    </div>
  )
}
