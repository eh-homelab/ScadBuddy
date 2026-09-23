import { useEffect, useState } from 'react'
import { api, ApiError } from '../api/client'
import type { ConnectionTest, SettingsUpdate } from '../api/types'
import { Button } from '../components/ui/Button'
import { Spinner } from '../components/ui/Spinner'
import { useAsync } from '../lib/useAsync'

export function SettingsPage() {
  const settingsState = useAsync(() => api.getSettings(), [])
  const targetsState = useAsync(() => api.getBambuddyTargets(), [])

  const [url, setUrl] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [folderId, setFolderId] = useState('')
  const [pipelineId, setPipelineId] = useState('')

  const [saving, setSaving] = useState(false)
  const [savedAt, setSavedAt] = useState<string | null>(null)
  const [testing, setTesting] = useState(false)
  const [test, setTest] = useState<ConnectionTest | null>(null)
  const [registering, setRegistering] = useState(false)
  const [sidebar, setSidebar] = useState<{ ok: boolean; detail: string } | null>(null)
  const [error, setError] = useState<string | null>(null)

  const settings = settingsState.data
  useEffect(() => {
    if (!settings) return
    setUrl(settings.bambuddy_url)
    setFolderId(settings.library_folder_id ?? '')
    setPipelineId(settings.pipeline_id ?? '')
  }, [settings])

  function draft(): SettingsUpdate {
    const body: SettingsUpdate = { bambuddy_url: url }
    if (apiKey.length > 0) body.api_key = apiKey
    if (folderId) body.library_folder_id = folderId
    if (pipelineId) body.pipeline_id = pipelineId
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
      setTest(await api.testSettings(draft()))
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
                  settings?.api_key_set
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
                <p
                  role="status"
                  className={`text-[12px] ${test.ok ? 'text-ok' : 'text-warn'}`}
                >
                  {test.detail}
                  {test.printers && test.printers.length > 0 && (
                    <span className="text-muted"> ({test.printers.map((p) => p.name).join(', ')})</span>
                  )}
                </p>
              )}
            </div>
          </div>
        </section>

        <section className="mt-4 rounded-[6px] border border-line bg-surface">
          <h2 className="border-b border-line px-4 py-2.5 text-[13px] font-medium">Where files go</h2>
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
                <option value="">None — upload without slicing</option>
                {(targetsState.data?.pipelines ?? []).map((pipeline) => (
                  <option key={pipeline.id} value={pipeline.id}>
                    {pipeline.name}
                  </option>
                ))}
              </select>
              <p className="mt-1.5 text-[12px] text-muted">
                Used by &ldquo;Slice and queue&rdquo;. Without one, files are uploaded to the library
                only.
              </p>
            </div>
          </div>
        </section>

        <section className="mt-4 rounded-[6px] border border-line bg-surface">
          <h2 className="border-b border-line px-4 py-2.5 text-[13px] font-medium">
            Bambuddy sidebar
          </h2>
          <div className="p-4">
            <p className="text-[13px] text-muted">
              Adds ScadBuddy to Bambuddy&rsquo;s sidebar as an External Link called
              &ldquo;Customize&rdquo;, opening inside Bambuddy rather than a new tab. Running it
              again updates the existing entry.
            </p>
            <div className="mt-3 flex items-center gap-2">
              <Button onClick={() => void addSidebar()} disabled={registering} aria-busy={registering}>
                {registering && <Spinner />}
                Add to Bambuddy sidebar
              </Button>
              {(sidebar ?? (settings?.sidebar_registered ? { ok: true, detail: 'Already in the sidebar.' } : null)) && (
                <p role="status" className="text-[12px] text-ok">
                  {sidebar?.detail ?? 'Already in the sidebar.'}
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
