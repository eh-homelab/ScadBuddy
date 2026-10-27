import { act, screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { useEffect, type ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { mockSettings, setMockSettings } from '../../mocks/handlers'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { ChoiceSelect, TipProvider } from '../../components/settings/form'
import { Button } from '@/components/ui/button'
import { BambuddyResourcesProvider } from './bambuddyResources'
import { SettingsCacheProvider, SettingsLoaded, useSettingsCache, type SettingsCache } from './cache'
import { FieldRow } from './controls'
import { SettingsDirtyProvider, useDirtySections } from './dirty'
import { FieldBadge, RuntimeRows, SavedSection } from './formParts'
import { storeChoice } from './values'
import { useBackendSettings } from './useBackendSettings'

let dirtyNow: readonly string[] = []
function DirtyProbe() {
  const seen = useDirtySections()
  useEffect(() => {
    dirtyNow = seen
  })
  return null
}
const dirtySections = () => dirtyNow

let cache: SettingsCache
function CacheProbe() {
  const seen = useSettingsCache()
  useEffect(() => {
    cache = seen
  })
  return null
}

function Timing() {
  const form = useBackendSettings('rendering', ['render_timeout', 'job_ttl'])
  return (
    <SavedSection form={form} id="rendering" title="Rendering">
      <RuntimeRows form={form} section="rendering" />
    </SavedSection>
  )
}

function Store() {
  const form = useBackendSettings('projects', ['store_backend'], {
    effective: (name, raw, f) => (name === 'store_backend' ? storeChoice(f.settings, f.value).backend : raw),
  })
  const store = storeChoice(form.settings, form.value)
  return (
    <SavedSection form={form} id="projects" title="Projects & files">
      <FieldRow id="store-backend" label="Blob store" badge={<FieldBadge form={form} name="store_backend" />}>
        <ChoiceSelect
          id="store-backend"
          value={store.backend}
          onValueChange={(next) => form.setField('store_backend', next)}
          choices={[
            { value: 'local', label: 'This server’s volume' },
            { value: 'bambuddy', label: 'Bambuddy library' },
            { value: 's3', label: 'S3' },
          ]}
        />
      </FieldRow>
      {/* What the select cannot do while Bambuddy's option is not ready: choose it anyway. */}
      <Button onClick={() => form.setField('store_backend', 'bambuddy')}>Choose Bambuddy</Button>
    </SavedSection>
  )
}

function Url() {
  const form = useBackendSettings('connection', ['bambuddy_url'], {
    alsoReset: (name, s) => (name === 'bambuddy_url' && s.store_backend === 'bambuddy' ? { store_backend: 'local' } : {}),
  })
  return (
    <SavedSection form={form} id="connection" title="Connection">
      <FieldRow
        id="bambuddy-url"
        label="Bambuddy URL"
        badge={<FieldBadge form={form} name="bambuddy_url" />}
        error={form.errors.bambuddy_url}
      >
        <input id="bambuddy-url" value={form.value('bambuddy_url')} onChange={(event) => form.setField('bambuddy_url', event.target.value)} />
      </FieldRow>
    </SavedSection>
  )
}

function Harness({ children }: { children?: ReactNode }) {
  return (
    <TipProvider>
      <SettingsDirtyProvider>
        <SettingsCacheProvider>
          <BambuddyResourcesProvider>
            <DirtyProbe />
            <CacheProbe />
            <SettingsLoaded>
              <Timing />
              <Store />
              <Url />
              {children}
            </SettingsLoaded>
          </BambuddyResourcesProvider>
        </SettingsCacheProvider>
      </SettingsDirtyProvider>
    </TipProvider>
  )
}

const region = (name: string) => screen.getByRole('region', { name })

async function renderHarness() {
  const view = renderPage(<Harness />)
  await screen.findByLabelText('Render timeout')
  return view
}

describe('useBackendSettings', () => {
  it('seeds its draft from the shared settings and reports clean', async () => {
    await renderHarness()
    expect(await screen.findByLabelText('Render timeout')).toHaveValue(300)
    expect(screen.getByLabelText('Bambuddy URL')).toHaveValue(mockSettings().bambuddy_url)
    expect(dirtySections()).toEqual([])
  })

  it('reports dirty on an edit, and clean again after Discard', async () => {
    const { user } = await renderHarness()
    const timeout = screen.getByLabelText('Render timeout')
    await user.clear(timeout)
    await user.type(timeout, '45')
    await waitFor(() => expect(dirtySections()).toEqual(['rendering']))
    expect(within(region('Rendering')).getByText('Unsaved')).toBeInTheDocument()

    await user.click(within(region('Rendering')).getByRole('button', { name: 'Discard Rendering changes' }))
    await waitFor(() => expect(dirtySections()).toEqual([]))
    expect(timeout).toHaveValue(300)
  })

  it('saves only its own changed fields', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = await renderHarness()
    const timeout = screen.getByLabelText('Render timeout')
    await user.clear(timeout)
    await user.type(timeout, '45')
    await user.type(screen.getByLabelText('Bambuddy URL'), '/x')
    await user.click(within(region('Rendering')).getByRole('button', { name: 'Save Rendering' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1))
    expect(put).toHaveBeenCalledWith({ render_timeout: 45 })
    await waitFor(() => expect(screen.getByTestId('source-render_timeout')).toHaveTextContent('Set here'))
    expect(within(region('Rendering')).queryByText('Unsaved')).toBeNull()
    expect(within(region('Rendering')).getByText(/Saved at/)).toBeInTheDocument()
    // The other section kept its edit.
    expect(screen.getByLabelText('Bambuddy URL')).toHaveValue(`${mockSettings().bambuddy_url}/x`)
    expect(dirtySections()).toEqual(['connection'])
    put.mockRestore()
  })

  it('keeps an edit typed while its own save is in flight (#767)', async () => {
    const save = api.putSettings.bind(api)
    let release: () => void = () => undefined
    const held = new Promise<void>((resolve) => (release = resolve))
    const put = vi.spyOn(api, 'putSettings').mockImplementation(async (body) => {
      const saved = await save(body)
      await held
      return saved
    })
    const { user } = await renderHarness()

    const timeout = screen.getByLabelText('Render timeout')
    await user.clear(timeout)
    await user.type(timeout, '45')
    await user.click(within(region('Rendering')).getByRole('button', { name: 'Save Rendering' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1))
    await user.clear(timeout)
    await user.type(timeout, '50')

    await act(async () => release())
    await waitFor(() => expect(screen.getByTestId('source-render_timeout')).toHaveTextContent('Set here'))
    expect(timeout).toHaveValue(50)
    expect(within(region('Rendering')).getByText('Unsaved')).toBeInTheDocument()
    put.mockRestore()
  })

  it("keeps another section's edit when this one saves (#767)", async () => {
    // Rendering's save is held; Connection's goes straight through. Connection's request
    // must not erase the edit Rendering's answer has to leave alone.
    const save = api.putSettings.bind(api)
    let release: () => void = () => undefined
    const held = new Promise<void>((resolve) => (release = resolve))
    const put = vi.spyOn(api, 'putSettings').mockImplementation(async (body) => {
      const saved = await save(body)
      if ('render_timeout' in body) await held
      return saved
    })
    const { user } = await renderHarness()

    const timeout = screen.getByLabelText('Render timeout')
    await user.clear(timeout)
    await user.type(timeout, '45')
    await user.click(within(region('Rendering')).getByRole('button', { name: 'Save Rendering' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1))
    await user.clear(timeout)
    await user.type(timeout, '50')
    await user.type(screen.getByLabelText('Bambuddy URL'), '/x')
    await user.click(within(region('Connection')).getByRole('button', { name: 'Save Connection' }))
    await waitFor(() => expect(within(region('Connection')).queryByText('Unsaved')).toBeNull())

    await act(async () => release())
    await waitFor(() => expect(screen.getByTestId('source-render_timeout')).toHaveTextContent('Set here'))
    expect(timeout).toHaveValue(50)
    put.mockRestore()
  })

  it('stays saving until the last of two saves in flight settles (#4847 review)', async () => {
    const save = api.putSettings.bind(api)
    const holds: (() => void)[] = []
    const put = vi.spyOn(api, 'putSettings').mockImplementation(async (body) => {
      await new Promise<void>((resolve) => holds.push(resolve))
      return save(body)
    })
    let form: ReturnType<typeof useBackendSettings> | undefined
    function Probe() {
      const seen = useBackendSettings('diagnostics', ['job_ttl'])
      useEffect(() => {
        form = seen
      })
      return null
    }
    renderPage(
      <Harness>
        <Probe />
      </Harness>,
    )
    await waitFor(() => expect(form).toBeDefined())
    act(() => form?.setField('job_ttl', '45'))
    let first: Promise<boolean> | undefined
    let second: Promise<boolean> | undefined
    act(() => {
      first = form?.save()
    })
    act(() => form?.setField('job_ttl', '46'))
    act(() => {
      second = form?.save()
    })
    await waitFor(() => expect(holds).toHaveLength(2))
    expect(form?.saving).toBe(true)

    await act(async () => {
      holds[0]?.()
      await first
    })
    expect(form?.saving).toBe(true)
    await act(async () => {
      holds[1]?.()
      await second
    })
    expect(form?.saving).toBe(false)
    put.mockRestore()
  })

  it('refuses a negative number before sending, tied to the field', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = await renderHarness()
    const timeout = screen.getByLabelText('Render timeout')
    await user.clear(timeout)
    await user.type(timeout, '-1')
    await user.click(within(region('Rendering')).getByRole('button', { name: 'Save Rendering' }))
    const error = await screen.findByText('Enter 0 or more.')
    expect(put).not.toHaveBeenCalled()
    expect(timeout.getAttribute('aria-describedby')?.split(' ')).toContain(error.id)
    expect(timeout).toHaveAttribute('aria-invalid', 'true')
    // Typing again clears it.
    await user.type(timeout, '5')
    await waitFor(() => expect(screen.queryByText('Enter 0 or more.')).toBeNull())
    put.mockRestore()
  })

  it("shows a 422's field error beside the field, and a detail-only error on the section", async () => {
    const answers = [
      HttpResponse.json(
        {
          type: 'about:blank',
          title: 'Unprocessable Content',
          status: 422,
          detail: 'the request did not match the expected shape',
          errors: [{ loc: ['body', 'bambuddy_url'], msg: 'Value error, not a URL' }],
        },
        { status: 422, headers: { 'content-type': 'application/problem+json' } },
      ),
      HttpResponse.json(
        { type: 'about:blank', title: 'Conflict', status: 409, detail: 'the store is migrating' },
        { status: 409, headers: { 'content-type': 'application/problem+json' } },
      ),
    ]
    server.use(http.put('/api/v1/settings', () => answers.shift()))
    const { user } = await renderHarness()
    const url = screen.getByLabelText('Bambuddy URL')
    await user.type(url, '/x')
    const save = within(region('Connection')).getByRole('button', { name: 'Save Connection' })
    await user.click(save)
    expect(await screen.findByText('not a URL')).toBeInTheDocument()
    expect(url).toHaveAttribute('aria-invalid', 'true')
    expect(within(region('Connection')).queryByText('the request did not match the expected shape')).toBeNull()

    await user.click(save)
    const sectionError = await within(region('Connection')).findByText('the store is migrating')
    expect(sectionError).toHaveAttribute('role', 'alert')
  })

  it('resets with alsoReset and reseeds those fields in every section', async () => {
    setMockSettings({ ...mockSettings(), store_backend: 'bambuddy' })
    const put = vi.spyOn(api, 'putSettings')
    const { user } = await renderHarness()
    await waitFor(() => expect(screen.getByLabelText('Blob store')).toHaveTextContent('Bambuddy library'))
    await user.click(screen.getByRole('button', { name: 'Reset to deployment value: Bambuddy URL' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1))
    expect(put).toHaveBeenCalledWith({ reset: ['bambuddy_url'], store_backend: 'local' })
    await waitFor(() => expect(screen.getByLabelText('Blob store')).toHaveTextContent('This server’s volume'))
    expect(screen.getByLabelText('Bambuddy URL')).toHaveValue('')
    expect(dirtySections()).toEqual([])
    put.mockRestore()
  })

  it('takes the latest settings when they arrive after Load latest, edits and all', async () => {
    const { user } = await renderHarness()
    const timeout = screen.getByLabelText('Render timeout')
    await user.clear(timeout)
    await user.type(timeout, '45')
    setMockSettings({ ...mockSettings(), render_timeout: 600, bambuddy_url: 'https://elsewhere.example' })
    act(() => cache.loadLatest())
    await waitFor(() => expect(timeout).toHaveValue(600))
    expect(screen.getByLabelText('Bambuddy URL')).toHaveValue('https://elsewhere.example')
    await waitFor(() => expect(dirtySections()).toEqual([]))
  })

  it("shows a refused reset on the field's section", async () => {
    server.use(
      http.put('/api/v1/settings', () =>
        HttpResponse.json(
          { type: 'about:blank', title: 'Conflict', status: 409, detail: 'cannot reset now' },
          { status: 409, headers: { 'content-type': 'application/problem+json' } },
        ),
      ),
    )
    const { user } = await renderHarness()
    await user.click(screen.getByRole('button', { name: 'Reset to deployment value: Bambuddy URL' }))
    expect(await within(region('Connection')).findByRole('alert')).toHaveTextContent('cannot reset now')
  })

  it('compares and sends the effective value', async () => {
    // No inbox folder: the Bambuddy store is not ready, so choosing it is still local.
    setMockSettings({ ...mockSettings(), library_folder_id: null })
    const { user, unmount } = await renderHarness()
    await user.click(screen.getByRole('button', { name: 'Choose Bambuddy' }))
    expect(screen.getByLabelText('Blob store')).toHaveTextContent('This server’s volume')
    expect(dirtySections()).toEqual([])
    expect(within(region('Projects & files')).queryByText('Unsaved')).toBeNull()
    unmount()

    // With the inbox saved, the choice is what is sent.
    setMockSettings({ ...mockSettings(), library_folder_id: 2 })
    const put = vi.spyOn(api, 'putSettings')
    const next = await renderHarness()
    await next.user.click(screen.getByRole('button', { name: 'Choose Bambuddy' }))
    await waitFor(() => expect(dirtySections()).toEqual(['projects']))
    await next.user.click(within(region('Projects & files')).getByRole('button', { name: 'Save Projects & files' }))
    await waitFor(() => expect(put).toHaveBeenCalledWith({ store_backend: 'bambuddy' }))
    await waitFor(() => expect(dirtySections()).toEqual([]))
    put.mockRestore()
  })
})
