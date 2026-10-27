import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import { api } from '../../../api/client'
import type { Settings } from '../../../api/types'
import { mockSettings } from '../../../mocks/handlers'
import { server } from '../../../mocks/server'
import { enter, region, renderSettingsSection } from '../testing'
import { Component as Connection } from './connection'
import { Component as Printers } from './printers'

/** Bambuddy's "Test connection", as against a plugin endpoint's (RemotePluginsPanel). */
const bambuddyTest = () => within(region('Connection')).getByRole('button', { name: 'Test connection' })

/** The form seeds itself from the server, so wait for the URL to arrive. */
async function seeded() {
  await waitFor(() =>
    expect(screen.getByLabelText('Bambuddy URL')).toHaveValue('https://bambuddy.internal.nullreference.io'),
  )
}

/** The render key's warning (#426) is a status too, so find the one that says `text`. */
function findStatus(text: string) {
  return screen.findByText(
    (_, element) => element?.getAttribute('role') === 'status' && (element.textContent ?? '').includes(text),
  )
}

describe('Connection', () => {
  it('loads the stored connection without revealing the key', async () => {
    renderSettingsSection(<Connection />)
    await seeded()
    const key = screen.getByLabelText('API key')
    expect(key).toHaveValue('')
    expect(key).toHaveAttribute('type', 'password')
    expect(key).toHaveAttribute('placeholder', expect.stringContaining('A key is stored'))
  })

  it('says when no key is stored yet', async () => {
    server.use(http.get('/api/v1/settings', () => HttpResponse.json({ bambuddy_url: '', has_api_key: false })))
    renderSettingsSection(<Connection />)
    expect(await screen.findByLabelText('API key')).toHaveAttribute('placeholder', 'Paste the key')
  })

  it('tests the connection and names the printers it found', async () => {
    const { user } = renderSettingsSection(<Connection />)
    await seeded()

    await user.click(bambuddyTest())
    expect(await findStatus('3DP-31B-598')).toBeInTheDocument()
  })

  it("imports Bambuddy's printers once the connection tests OK (#4439)", async () => {
    const { user } = renderSettingsSection(
      <>
        <Connection />
        <Printers />
      </>,
    )
    await seeded()

    await user.click(bambuddyTest())

    const imported = await screen.findByRole('status', { name: 'Printer import' })
    expect(imported).toHaveTextContent('Printers from Bambuddy: 1 added, 1 linked, 1 already linked, 1 skipped')
    const section = screen.getByRole('region', { name: 'Printers' })
    expect(await within(section).findByRole('listitem', { name: 'Printer H2C Virtual' })).toBeInTheDocument()
    expect(within(section).getByRole('button', { name: 'Import from Bambuddy' })).toBeEnabled()
  })

  it('imports nothing when the connection test fails (#4439)', async () => {
    const posts: string[] = []
    server.events.on('request:start', ({ request }) => {
      if (request.method === 'POST') posts.push(new URL(request.url).pathname)
    })
    server.use(
      http.post('/api/v1/settings/test', () =>
        HttpResponse.json({ ok: false, detail: "The key needs the 'Read Status' scope", printers: [] }),
      ),
    )
    const { user } = renderSettingsSection(<Connection />)
    await seeded()

    await user.click(bambuddyTest())

    expect(await findStatus("'Read Status' scope")).toBeInTheDocument()
    expect(posts).not.toContain('/api/v1/printers/sync-bambuddy')
  })

  it('names the missing scope rather than the bare status code', async () => {
    server.use(
      http.post('/api/v1/settings/test', () =>
        HttpResponse.json({
          ok: false,
          detail: "The key needs the 'Read Status' scope",
          printers: [],
        }),
      ),
    )
    const { user } = renderSettingsSection(<Connection />)
    await seeded()

    await user.click(bambuddyTest())
    expect(await findStatus("'Read Status' scope")).toBeInTheDocument()
  })

  it('shows what Bambuddy itself answered a failed test (#1542)', async () => {
    server.use(
      http.post('/api/v1/settings/test', () =>
        HttpResponse.json({
          ok: false,
          detail: 'Bambuddy is limiting requests and refused to list the printers; it asks to wait 30 s',
          printers: [],
          upstream: { status: 429, retry_after: '30', detail: 'Too many requests for this API key', error: null },
        }),
      ),
    )
    const { user } = renderSettingsSection(<Connection />)
    await seeded()

    await user.click(bambuddyTest())
    expect(await findStatus('limiting requests')).toBeInTheDocument()
    const answered = screen.getByText('What Bambuddy answered').closest<HTMLElement>('[data-slot=collapsible]')!
    expect(within(answered).getByText('429')).toBeInTheDocument()
    expect(within(answered).getByText('30')).toBeInTheDocument()
    expect(within(answered).getByText('Too many requests for this API key')).toBeInTheDocument()
  })

  it('shows no answer when the test passed', async () => {
    const { user } = renderSettingsSection(<Connection />)
    await seeded()

    await user.click(bambuddyTest())
    expect(await findStatus('3DP-31B-598')).toBeInTheDocument()
    expect(screen.queryByText('What Bambuddy answered')).not.toBeInTheDocument()
  })

  it('sends the key only when one was typed', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderSettingsSection(<Connection />)
    await seeded()

    const url = screen.getByLabelText('Bambuddy URL')
    await user.type(url, '/')
    await user.click(within(region('Connection')).getByRole('button', { name: 'Save Connection' }))
    await waitFor(() => expect(put).toHaveBeenCalled())
    expect(put.mock.calls[0]?.[0]).not.toHaveProperty('bambuddy_api_key')
    await waitFor(() => expect(screen.getByText(/Saved at/)).toBeInTheDocument())

    await enter(user, screen.getByLabelText('API key'), 'secret')
    await user.click(within(region('Connection')).getByRole('button', { name: 'Save Connection' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(2))
    expect(put.mock.calls[1]?.[0]).toEqual({ bambuddy_api_key: 'secret' })
    put.mockRestore()
  })

  it('clears the key field once it is saved', async () => {
    const { user } = renderSettingsSection(<Connection />)
    await seeded()

    await enter(user, screen.getByLabelText('API key'), 'secret')
    await user.click(within(region('Connection')).getByRole('button', { name: 'Save Connection' }))
    await waitFor(() => expect(screen.getByLabelText('API key')).toHaveValue(''))
    expect(screen.getByText(/Saved at/)).toBeInTheDocument()
  })

  it('registers the Bambuddy sidebar entry', async () => {
    const { user } = renderSettingsSection(<Connection />)
    await seeded()

    await user.click(screen.getByRole('button', { name: 'Add to Bambuddy sidebar' }))
    // Bambuddy renders the link in a sandboxed iframe at /external/{id}.
    expect(await findStatus('/external/3')).toBeInTheDocument()
  })

  it('lists every scope, the write scopes as not checked', async () => {
    const { user } = renderSettingsSection(<Connection />)
    await seeded()
    await user.click(screen.getByRole('button', { name: 'Test connection' }))
    const scopes = await screen.findByRole('list', { name: 'Scopes' })
    expect(within(scopes).getAllByRole('listitem')).toHaveLength(6)
    expect(scopes).toHaveTextContent('Read Status: granted.')
    expect(scopes).toHaveTextContent('Manage Queue: not checked.')
    expect(scopes).toHaveTextContent('Manage Archives (optional): not checked.')
    expect(scopes).toHaveTextContent('Manage Inventory (optional): not checked.')
  })

  it('says Bambuddy takes no finish photo, with a link to turn it on', async () => {
    renderSettingsSection(<Connection />)
    const status = await screen.findByTestId('bambuddy-status')
    await waitFor(() => expect(status).toHaveTextContent('Finish photo in Bambuddy: off'))
    expect(within(status).getByRole('link', { name: 'Turn it on in Bambuddy' })).toHaveAttribute(
      'href',
      'https://bambuddy.internal.nullreference.io/settings',
    )
  })

  it('typing a key after Remove key replaces it instead of clearing it', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderSettingsSection(<Connection />)
    await seeded()
    const key = screen.getByLabelText('API key')
    const remove = screen.getByRole('button', { name: 'Remove key' })
    await user.click(remove)
    expect(key).toHaveAttribute('placeholder', 'Cleared when you save.')
    expect(remove).toBeDisabled()

    await user.type(key, 'n')
    expect(remove).toBeEnabled()
    // Emptied again, the field is back to leaving the stored key alone.
    await user.clear(key)
    expect(key).toHaveAttribute('placeholder', 'A key is stored. Paste a new one to replace it.')
    expect(within(region('Connection')).getByRole('button', { name: 'Save Connection' })).toBeDisabled()
    expect(put).not.toHaveBeenCalled()
    put.mockRestore()
  })
})

// #426 — the render key, beside the API key in Connection.
describe('Connection render key (#426)', () => {
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

  it('warns while render workers would hold the full key', async () => {
    serve({ ...stored, has_render_api_key: false, render_key_fallback: true })
    renderSettingsSection(<Connection />)
    await seeded()
    const warning = screen.getByTestId('render-key-fallback')
    expect(warning).toHaveAttribute('role', 'status')
    expect(warning).toHaveTextContent(
      /^Render workers hold the full Bambuddy key; template code can print\. Create a key with only Manage Library in Bambuddy and paste it above as the render key\.$/,
    )
    expect(screen.getByLabelText('Render key')).toHaveAttribute('placeholder', 'Paste the key')
  })

  it('drops the warning once a render key is stored, and never shows the key', async () => {
    serve({ ...stored, has_render_api_key: true, render_key_fallback: false })
    renderSettingsSection(<Connection />)
    await seeded()
    expect(screen.queryByTestId('render-key-fallback')).toBeNull()
    const key = screen.getByLabelText('Render key')
    expect(key).toHaveValue('')
    expect(key).toHaveAttribute('type', 'password')
    expect(key).toHaveAttribute('placeholder', expect.stringContaining('A key is stored'))
  })

  it('sends a typed render key with Connection and leaves the stored API key alone', async () => {
    const bodies = serve({ ...stored, has_render_api_key: false, render_key_fallback: true })
    const { user } = renderSettingsSection(<Connection />)
    await seeded()
    await enter(user, screen.getByLabelText('Render key'), 'narrow')
    await user.click(within(region('Connection')).getByRole('button', { name: 'Save Connection' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ bambuddy_render_api_key: 'narrow' })
    await waitFor(() => expect(screen.queryByTestId('render-key-fallback')).toBeNull())
    expect(screen.getByLabelText('Render key')).toHaveValue('')
  })

  it('removes a stored render key when Connection is saved', async () => {
    const bodies = serve({ ...stored, has_render_api_key: true, render_key_fallback: false })
    const { user } = renderSettingsSection(<Connection />)
    await seeded()
    const row = screen.getByLabelText('Render key').closest('div') as HTMLElement
    await user.click(within(row).getByRole('button', { name: 'Remove key' }))
    expect(screen.getByLabelText('Render key')).toHaveAttribute('placeholder', 'Cleared when you save.')
    await user.click(within(region('Connection')).getByRole('button', { name: 'Save Connection' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ bambuddy_render_api_key: '' })
  })
})
