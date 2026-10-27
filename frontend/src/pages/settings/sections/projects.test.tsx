import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it } from 'vitest'
import type { Settings } from '../../../api/types'
import { mockSettings, setMockSettings } from '../../../mocks/handlers'
import { server } from '../../../mocks/server'
import { choose, optionDisabled } from '../../../test/select'
import { enter, region, renderSettingsSection } from '../testing'
import { Component as Connection } from './connection'
import { Component as Projects } from './projects'

const route = '/settings/library'

/** The form seeds itself from the server, so wait for the URL to arrive. */
async function seeded() {
  await waitFor(() =>
    expect(screen.getByLabelText('Bambuddy URL')).toHaveValue('https://bambuddy.internal.nullreference.io'),
  )
}

/** The section renders once the settings are loaded. */
async function loaded() {
  await screen.findByLabelText('Blob store')
}

describe('Projects & files, the blob store (#426)', () => {
  const stored = {
    ...mockSettings(),
    bambuddy_url: 'https://bambuddy.internal.nullreference.io',
    has_api_key: true,
    library_folder_id: 2,
    store_backend: 'local' as const,
  }

  function serve(settings: Settings) {
    const bodies: Record<string, unknown>[] = []
    server.use(
      http.get('/api/v1/settings', () => HttpResponse.json(settings)),
      http.put('/api/v1/settings', async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>
        bodies.push(body)
        const { bambuddy_render_api_key: key, ...rest } = body
        return HttpResponse.json({
          ...settings,
          ...rest,
          ...(typeof key === 'string' ? { has_render_api_key: key !== '', render_key_fallback: key === '' } : {}),
        })
      }),
    )
    return bodies
  }

  it('seeds the Blob store choice and sends a change with Projects & files', async () => {
    const bodies = serve({ ...stored, has_render_api_key: true, render_key_fallback: false })
    const { user } = renderSettingsSection(<Projects />, { route })
    await loaded()
    const store = screen.getByLabelText('Blob store')
    expect(store).toHaveTextContent('This server’s volume')
    expect(await optionDisabled(user, store, /Bambuddy library/)).toBe(false)
    await choose(user, store, /Bambuddy library/)
    expect(within(region('Projects & files')).getByText('Unsaved')).toBeInTheDocument()
    await user.click(within(region('Projects & files')).getByRole('button', { name: 'Save Projects & files' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ store_backend: 'bambuddy' })
  })

  it('offers ScadBuddy’s own library only over a content store', async () => {
    serve({ ...stored, store_backend: 'local' })
    const { user } = renderSettingsSection(<Projects />, { route })
    await loaded()
    const library = screen.getByLabelText('Print library')
    expect(library).toHaveTextContent('Bambuddy')
    expect(await optionDisabled(user, library, /ScadBuddy.s own/)).toBe(true)
  })

  it('saves slicer_backend, which applies at once (plan 2026-10-16 Task 14)', async () => {
    const bodies = serve({ ...stored, applies: { ...stored.applies, slicer_backend: 'live' } })
    const { user } = renderSettingsSection(<Projects />, { route })
    await loaded()
    const slicer = screen.getByLabelText('Slicer')
    expect(slicer).toHaveTextContent('Bambuddy’s slicer')
    await choose(user, slicer, /ScadBuddy.s slicer \(Bambu Studio CLI\)/)
    await user.click(within(region('Projects & files')).getByRole('button', { name: 'Save Projects & files' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ slicer_backend: 'native' })
    expect(screen.getByLabelText('Slicer')).toHaveTextContent('ScadBuddy’s slicer (Bambu Studio CLI)')
    expect(within(region('Projects & files')).getByTestId('source-slicer_backend')).not.toHaveTextContent(
      'Applies on restart',
    )
  })

  it('saves library_backend and shows it applies on restart', async () => {
    const bodies = serve({
      ...stored,
      store_backend: 'bambuddy',
      has_render_api_key: true,
      render_key_fallback: false,
    })
    const { user } = renderSettingsSection(<Projects />, { route })
    await loaded()
    await waitFor(() => expect(screen.getByLabelText('Blob store')).toHaveTextContent('Bambuddy library'))
    const library = screen.getByLabelText('Print library')
    expect(await optionDisabled(user, library, /ScadBuddy.s own/)).toBe(false)
    await choose(user, library, /ScadBuddy.s own/)
    await user.click(within(region('Projects & files')).getByRole('button', { name: 'Save Projects & files' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(within(region('Projects & files')).getByTestId('source-library_backend')).toHaveTextContent(
      'Applies on restart',
    )
    expect(bodies[0]).toEqual({ library_backend: 'native' })
    expect(screen.getByLabelText('Print library')).toHaveTextContent('ScadBuddy’s own')
    // Only the API reads it at start: the workers ask the API on every call (#3827).
    expect(
      within(region('Projects & files')).getByText(/Takes effect when the ScadBuddy API restarts; its workers need no restart/),
    ).toBeInTheDocument()
  })

  it('does not fall back to local while the library is native', async () => {
    const bodies: Record<string, unknown>[] = []
    const stuck = { ...stored, store_backend: 'bambuddy' as const, library_backend: 'native' as const }
    server.use(
      http.get('/api/v1/settings', () => HttpResponse.json(stuck)),
      http.put('/api/v1/settings', async ({ request }) => {
        bodies.push((await request.json()) as Record<string, unknown>)
        return HttpResponse.json(
          {
            type: 'about:blank',
            title: 'Unprocessable Content',
            status: 422,
            detail:
              'ScadBuddy\'s own library keeps its files in the blob store: set store_backend to s3 or bambuddy first',
          },
          { status: 422, headers: { 'Content-Type': 'application/problem+json' } },
        )
      }),
    )
    const { user } = renderSettingsSection(
      <>
        <Connection />
        <Projects />
      </>,
      { route },
    )
    await seeded()
    await waitFor(() => expect(screen.getByLabelText('Print library')).toHaveTextContent('ScadBuddy’s own'))
    await user.clear(screen.getByLabelText('Bambuddy URL'))
    await user.click(within(region('Connection')).getByRole('button', { name: 'Save Connection' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ bambuddy_url: null })
    expect(await screen.findByText(/set store_backend to s3 or bambuddy/)).toBeVisible()
    expect(screen.getByLabelText('Bambuddy URL')).toHaveValue('')
  })

  it('keeps a stored Bambuddy store backend on an unrelated save', async () => {
    const bodies = serve({ ...stored, store_backend: 'bambuddy', has_render_api_key: true, render_key_fallback: false })
    const { user } = renderSettingsSection(
      <>
        <Connection />
        <Projects />
      </>,
      { route },
    )
    await seeded()
    await waitFor(() => expect(screen.getByLabelText('Blob store')).toHaveTextContent('Bambuddy library'))
    const own = screen.getByLabelText(/ScadBuddy.s own URL/)
    await user.clear(own)
    await enter(user, own, 'https://scadbuddy.test')
    await user.click(within(region('Connection')).getByRole('button', { name: 'Save Connection' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).not.toHaveProperty('store_backend')
    expect(screen.getByLabelText('Blob store')).toHaveTextContent('Bambuddy library')
  })

  it('falls back to the local store when the Bambuddy store loses its inbox', async () => {
    const bodies = serve({ ...stored, store_backend: 'bambuddy', has_render_api_key: true, render_key_fallback: false })
    const { user } = renderSettingsSection(<Projects />, { route })
    await loaded()
    const store = screen.getByLabelText('Blob store')
    await waitFor(() => expect(store).toHaveTextContent('Bambuddy library'))
    await choose(user, screen.getByLabelText(/Inbox folder/), 'Library root')
    expect(await optionDisabled(user, store, /Bambuddy library/)).toBe(true)
    expect(store).toHaveTextContent('This server’s volume')
    await user.click(within(region('Projects & files')).getByRole('button', { name: 'Save Projects & files' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ library_folder_id: null, store_backend: 'local' })
  })

  it('falls back to the local store when a Connection save clears the Bambuddy URL', async () => {
    const bodies = serve({ ...stored, store_backend: 'bambuddy', has_render_api_key: true, render_key_fallback: false })
    const { user } = renderSettingsSection(
      <>
        <Connection />
        <Projects />
      </>,
      { route },
    )
    await seeded()
    await waitFor(() => expect(screen.getByLabelText('Blob store')).toHaveTextContent('Bambuddy library'))
    await user.clear(screen.getByLabelText('Bambuddy URL'))
    // The choice follows the saved URL, so it moves once Connection is saved.
    expect(screen.getByLabelText('Blob store')).toHaveTextContent('Bambuddy library')
    await user.click(within(region('Connection')).getByRole('button', { name: 'Save Connection' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ bambuddy_url: null, store_backend: 'local' })
    await waitFor(() => expect(screen.getByLabelText('Blob store')).toHaveTextContent('This server’s volume'))
  })

  it('falls back to the local store when the Bambuddy URL is reset while on the Bambuddy store', async () => {
    const bodies = serve({ ...stored, store_backend: 'bambuddy', has_render_api_key: true, render_key_fallback: false })
    const { user } = renderSettingsSection(
      <>
        <Connection />
        <Projects />
      </>,
      { route },
    )
    await seeded()
    await waitFor(() => expect(screen.getByLabelText('Blob store')).toHaveTextContent('Bambuddy library'))
    await user.click(within(screen.getByTestId('source-bambuddy_url')).getByRole('button', { name: 'Reset to deployment value: Bambuddy URL' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ reset: ['bambuddy_url'], store_backend: 'local' })
  })

  it('does not fall back to local on a reset while the library is native', async () => {
    const bodies = serve({
      ...stored,
      store_backend: 'bambuddy',
      library_backend: 'native',
      has_render_api_key: true,
      render_key_fallback: false,
    })
    const { user } = renderSettingsSection(
      <>
        <Connection />
        <Projects />
      </>,
      { route },
    )
    await seeded()
    await waitFor(() => expect(screen.getByLabelText('Print library')).toHaveTextContent('ScadBuddy’s own'))
    await user.click(within(screen.getByTestId('source-bambuddy_url')).getByRole('button', { name: 'Reset to deployment value: Bambuddy URL' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ reset: ['bambuddy_url'] })
  })

  it('resets only the Bambuddy URL while on the local store', async () => {
    const bodies = serve(stored)
    const { user } = renderSettingsSection(
      <>
        <Connection />
        <Projects />
      </>,
      { route },
    )
    await seeded()
    await user.click(within(screen.getByTestId('source-bambuddy_url')).getByRole('button', { name: 'Reset to deployment value: Bambuddy URL' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ reset: ['bambuddy_url'] })
  })

  it('keeps the Bambuddy store on a Connection save while an unsaved Projects edit drops the inbox', async () => {
    const bodies = serve({ ...stored, store_backend: 'bambuddy', has_render_api_key: true, render_key_fallback: false })
    const { user } = renderSettingsSection(
      <>
        <Connection />
        <Projects />
      </>,
      { route },
    )
    await seeded()
    await waitFor(() => expect(screen.getByLabelText('Blob store')).toHaveTextContent('Bambuddy library'))
    await choose(user, screen.getByLabelText(/Inbox folder/), 'Library root')
    const own = screen.getByLabelText('ScadBuddy’s own URL')
    await user.clear(own)
    await enter(user, own, 'https://mine.test')
    await user.click(within(region('Connection')).getByRole('button', { name: 'Save Connection' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ public_url: 'https://mine.test' })
  })

  it('keeps the Bambuddy store on a Projects save while an unsaved Connection edit clears the URL', async () => {
    const bodies = serve({ ...stored, store_backend: 'bambuddy', has_render_api_key: true, render_key_fallback: false })
    const { user } = renderSettingsSection(
      <>
        <Connection />
        <Projects />
      </>,
      { route },
    )
    await seeded()
    await waitFor(() => expect(screen.getByLabelText('Blob store')).toHaveTextContent('Bambuddy library'))
    await user.clear(screen.getByLabelText('Bambuddy URL'))
    await choose(user, screen.getByLabelText(/Inbox folder/), 'Keychains')
    await user.click(within(region('Projects & files')).getByRole('button', { name: 'Save Projects & files' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ library_folder_id: 3 })
  })

  it('offers the Bambuddy store only once its URL is saved, then marks choosing it unsaved', async () => {
    serve({ ...stored, bambuddy_url: null })
    const { user } = renderSettingsSection(
      <>
        <Connection />
        <Projects />
      </>,
      { route },
    )
    // Saved: an inbox folder, no Bambuddy URL.
    const url = await screen.findByLabelText('Bambuddy URL')
    await waitFor(() => expect(screen.getByTestId('source-store_backend')).toBeInTheDocument())
    await enter(user, url, 'https://bambuddy.new.test')
    expect(await optionDisabled(user, screen.getByLabelText('Blob store'), /Bambuddy library/)).toBe(true)
    expect(screen.getByTestId('store-backend-hint')).toHaveTextContent('Bambuddy URL is not saved yet')
    await user.click(within(region('Connection')).getByRole('button', { name: 'Save Connection' }))
    await waitFor(async () =>
      expect(await optionDisabled(user, screen.getByLabelText('Blob store'), /Bambuddy library/)).toBe(false),
    )
    expect(screen.queryByTestId('store-backend-hint')).toBeNull()
    await choose(user, screen.getByLabelText('Blob store'), /Bambuddy library/)
    expect(within(region('Projects & files')).getByText('Unsaved')).toBeInTheDocument()
    expect(within(region('Projects & files')).getByRole('button', { name: 'Save Projects & files' })).toBeEnabled()
  })

  it('offers the Bambuddy store only once an inbox folder is chosen', async () => {
    serve({ ...stored, library_folder_id: null })
    const { user } = renderSettingsSection(<Projects />, { route })
    await loaded()
    expect(await optionDisabled(user, screen.getByLabelText('Blob store'), /Bambuddy library/)).toBe(true)
  })

  it('shows what the blob store holds, where, against its caps', async () => {
    server.use(
      http.get('/api/v1/store/usage', () =>
        HttpResponse.json({
          backend: 'bambuddy',
          count: 12,
          bytes: 3_450_000,
          max_count: 10_000,
          max_total_bytes: 1_000_000_000,
          by_kind: { piece: 1024, asset: 1024 },
        }),
      ),
    )
    renderSettingsSection(<Projects />, { route })
    const usage = await screen.findByTestId('store-usage')
    expect(usage).toHaveTextContent('Bambuddy library')
    expect(within(usage).getByText('Files').nextElementSibling).toHaveTextContent(/^12 of 10000$/)
    expect(usage).toHaveTextContent('3.5 MB of 1.0 GB')
    expect(
      screen.getByText(
        'The Where row is the store this process uses; it moves to the Blob store choice above at its next restart, so the two can differ until then.',
      ),
    ).toBeInTheDocument()
  })

  it('says a store cap of zero is no limit', async () => {
    server.use(
      http.get('/api/v1/store/usage', () =>
        HttpResponse.json({ backend: 'local', by_kind: {}, count: 2, bytes: 640, max_count: 0, max_total_bytes: 0 }),
      ),
    )
    renderSettingsSection(<Projects />, { route })
    const usage = await screen.findByTestId('store-usage')
    expect(usage).toHaveTextContent('This server’s volume')
    expect(usage).toHaveTextContent('2 (no limit)')
    expect(usage).toHaveTextContent('640 B (no limit)')
  })
})

// The S3 store: its fields under the Blob store choice, the choice offered once they are saved.
describe('Projects & files, the S3 store', () => {
  const s3 = {
    store_s3_endpoint: 'https://s3.example.test',
    store_s3_bucket: 'renders',
    store_s3_access_key_id: 'AKIA',
  }

  it('offers the S3 store once its endpoint, bucket and keys are saved', async () => {
    setMockSettings({ ...mockSettings(), ...s3 })
    const { user } = renderSettingsSection(<Projects />, { route })
    await loaded()
    const store = screen.getByLabelText('Blob store')
    expect(await optionDisabled(user, store, /S3-compatible object store/)).toBe(true)
    expect(screen.getByTestId('store-s3-hint')).toHaveTextContent(
      'The S3 store needs its endpoint, bucket and keys saved first.',
    )
    const secret = screen.getByLabelText('Secret access key')
    expect(secret).toHaveAttribute('type', 'password')
    expect(secret).toHaveAttribute('placeholder', 'Paste the key')
    await enter(user, secret, 'shh')
    await user.click(within(region('Projects & files')).getByRole('button', { name: 'Save Projects & files' }))
    await waitFor(async () =>
      expect(await optionDisabled(user, screen.getByLabelText('Blob store'), /S3-compatible object store/)).toBe(false),
    )
    expect(mockSettings().has_store_s3_secret_access_key).toBe(true)
    expect(screen.queryByTestId('store-s3-hint')).toBeNull()
    expect(screen.getByLabelText('Secret access key')).toHaveValue('')
    expect(screen.getByLabelText('Secret access key')).toHaveAttribute(
      'placeholder',
      expect.stringContaining('A key is stored'),
    )
    await choose(user, screen.getByLabelText('Blob store'), /S3-compatible object store/)
    await user.click(within(region('Projects & files')).getByRole('button', { name: 'Save Projects & files' }))
    await waitFor(() => expect(mockSettings().store_backend).toBe('s3'))
    expect(screen.getByLabelText('Blob store')).toHaveTextContent('S3-compatible object store')
  })

  it('sends the S3 fields and the path-style switch with Projects & files', async () => {
    const bodies: Record<string, unknown>[] = []
    server.events.on('request:start', async ({ request }) => {
      if (request.method === 'PUT' && new URL(request.url).pathname === '/api/v1/settings') {
        bodies.push((await request.clone().json()) as Record<string, unknown>)
      }
    })
    const { user } = renderSettingsSection(<Projects />, { route })
    await loaded()
    expect(screen.getByLabelText('Region')).toHaveValue('us-east-1')
    expect(screen.getByLabelText('Prefix')).toHaveValue('scadbuddy')
    expect(screen.getByLabelText('Path-style addressing')).toBeChecked()
    await enter(user, screen.getByLabelText('Endpoint'), 'https://s3.example.test')
    await enter(user, screen.getByLabelText('Bucket'), 'renders')
    await user.click(screen.getByLabelText('Path-style addressing'))
    await user.click(within(region('Projects & files')).getByRole('button', { name: 'Save Projects & files' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    server.events.removeAllListeners()
    expect(bodies[0]).toEqual({
      store_s3_endpoint: 'https://s3.example.test',
      store_s3_bucket: 'renders',
      store_s3_path_style: false,
    })
  })

  it('refuses to switch to S3 without its fields', async () => {
    // Ready as saved, but the same save removes the secret: the server refuses the switch.
    setMockSettings({ ...mockSettings(), ...s3, has_store_s3_secret_access_key: true })
    const { user } = renderSettingsSection(<Projects />, { route })
    await loaded()
    await choose(user, screen.getByLabelText('Blob store'), /S3-compatible object store/)
    await user.click(within(region('Projects & files')).getByRole('button', { name: 'Remove key' }))
    await user.click(within(region('Projects & files')).getByRole('button', { name: 'Save Projects & files' }))
    expect(
      await within(region('Projects & files')).findByText(
        'the S3 store needs its endpoint, bucket, access key id and secret access key saved first',
      ),
    ).toBeInTheDocument()
    expect(mockSettings().has_store_s3_secret_access_key).toBe(true)
    expect(mockSettings().store_backend).toBe('local')
  })

  it('says where an S3 store is', async () => {
    server.use(
      http.get('/api/v1/store/usage', () =>
        HttpResponse.json({ backend: 's3', by_kind: {}, count: 2, bytes: 640, max_count: 0, max_total_bytes: 0 }),
      ),
    )
    renderSettingsSection(<Projects />, { route })
    const usage = await screen.findByTestId('store-usage')
    expect(within(usage).getByText('Where').nextElementSibling).toHaveTextContent(/^S3 object store$/)
  })
})
