import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { isUserOnly } from '../../agent/dom'
import { aiPlugins, type RemotePlugin } from '../../api/aiPlugins'
import { aiCapabilities, type RefreshResult } from '../../api/capabilities'
import { setRemotePluginProblem } from '../../mocks/aiPlugins'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { RemotePluginsPanel } from './RemotePlugins'
import { choose, optionsOf } from '../../test/select'

// Radix Select scrolls its selected option into view; jsdom has no layout.
Element.prototype.scrollIntoView ??= () => {}

const endpoint = (name = 'hindsight') => screen.findByRole('listitem', { name: `Plugin endpoint ${name}` })
const badge = (card: HTMLElement, text: string) => within(card).queryByText(text, { selector: '[data-slot="badge"]' })

async function hindsight(): Promise<RemotePlugin> {
  return (await aiPlugins.listPlugins()).find((p): p is RemotePlugin => p.name === 'hindsight')!
}

/** Tests the card's connection and opens its source's rows. */
async function tested(user: ReturnType<typeof renderPage>['user'], card: HTMLElement) {
  await user.click(within(card).getByRole('button', { name: 'Test connection' }))
  await within(card).findByText(/connected; 5 tools/)
  await user.click(within(card).getByRole('button', { name: 'hindsight' }))
}

describe('RemotePluginsPanel', () => {
  it('lists endpoints with the header name and last four only', async () => {
    renderPage(<RemotePluginsPanel />)
    const card = await endpoint()
    expect(within(card).getByText('Authorization: …9f3a')).toBeInTheDocument()
    await waitFor(() => expect(badge(card, 'Disabled')).toBeInTheDocument())
  })

  it('says when a stored endpoint is not loaded because Claude Code reserves its name (#2127)', async () => {
    const problem = 'its name "hindsight" is reserved by Claude Code, which drops the server; delete it and register it again under another name'
    setRemotePluginProblem('hindsight', problem)
    renderPage(<RemotePluginsPanel />)
    const card = await endpoint()
    expect(within(card).getByRole('alert')).toHaveTextContent(`Not loaded: ${problem}`)
  })

  it('shows no problem for an endpoint that has none', async () => {
    renderPage(<RemotePluginsPanel />)
    const card = await endpoint()
    expect(within(card).queryByText(/Not loaded/)).toBeNull()
  })

  it('registers an endpoint with a write-only secret', async () => {
    let sent: unknown
    server.events.on('request:start', ({ request }) => {
      if (request.method === 'POST' && request.url.endsWith('/ai/plugins')) void request.clone().json().then((b) => (sent = b))
    })
    const { user } = renderPage(<RemotePluginsPanel />)
    await endpoint()
    await user.type(screen.getByLabelText('Name'), 'notes')
    await user.type(screen.getByLabelText('MCP endpoint URL'), 'https://notes.example/mcp')
    expect(screen.getByLabelText('Header value')).toHaveAttribute('type', 'password')
    await user.type(screen.getByLabelText('Header value'), 'Bearer abcd1234')
    await user.click(screen.getByRole('button', { name: 'Add endpoint' }))
    const card = await endpoint('notes')
    expect(within(card).getByText('Authorization: …1234')).toBeInTheDocument()
    expect(sent).toEqual({
      name: 'notes',
      url: 'https://notes.example/mcp',
      secret: 'Bearer abcd1234',
      auth_header: 'Authorization',
    })
    // A new plugin's card reads its source.
    const source = await within(card).findByRole('region', { name: 'Source notes' })
    expect(within(source).getByText('No tools listed yet')).toBeInTheDocument()
  })

  it('shows a refusal', async () => {
    const { user } = renderPage(<RemotePluginsPanel />)
    await endpoint()
    await user.type(screen.getByLabelText('Name'), 'hindsight')
    await user.type(screen.getByLabelText('MCP endpoint URL'), 'https://x.example/mcp')
    await user.click(screen.getByRole('button', { name: 'Add endpoint' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('already exists')
  })

  it('tests the connection, then lists each tool with its access (a renamed tool offers off and outward)', async () => {
    const { user } = renderPage(<RemotePluginsPanel />)
    const card = await endpoint()
    await tested(user, card)
    expect(within(card).getByRole('combobox', { name: 'Access of recall' })).toHaveTextContent('read')
    expect(within(card).getByRole('combobox', { name: 'Access of retain' })).toHaveTextContent('outward (asks first) (default)')
    const renamed = within(card).getByRole('combobox', { name: 'Access of files.list' })
    expect(renamed).toBeEnabled()
    expect(await optionsOf(user, renamed)).toEqual(['off', 'outward (asks first) (default)'])
    expect(within(card).getByText(/Claude Code renames this tool/)).toBeInTheDocument()
  })

  it('shows a tool with stored access that the last listing lacks, with its access and Reset', async () => {
    const { user } = renderPage(<RemotePluginsPanel />)
    const card = await endpoint()
    await user.click(await within(card).findByRole('button', { name: 'hindsight' }))
    expect(within(card).getByText('Not in the last listing')).toBeInTheDocument()
    expect(within(card).getByRole('combobox', { name: 'Access of recall' })).toHaveTextContent('read')
    await user.click(within(card).getByRole('button', { name: 'Reset recall' }))
    await waitFor(() => expect(within(card).queryByRole('combobox', { name: 'Access of recall' })).toBeNull())
    expect((await hindsight()).tool_tiers).toEqual({})
  })

  it('says a truncated listing may be incomplete', async () => {
    server.use(
      http.post('/api/v1/ai/capabilities/:source/refresh', async () => {
        const source = await aiCapabilities.source('remote:hindsight')
        const test = { ok: true, detail: 'connected; 50 tools', duration_ms: 3, server: null, tools: [], truncated: true }
        return HttpResponse.json({ test, source: { ...source, truncated: true } } satisfies RefreshResult)
      }),
    )
    const { user } = renderPage(<RemotePluginsPanel />)
    const card = await endpoint()
    await user.click(within(card).getByRole('button', { name: 'Test connection' }))
    expect(await within(card).findByText(/the list may be incomplete/)).toBeInTheDocument()
  })

  it('sets a tool off, which replaces the Hidden column', async () => {
    const { user } = renderPage(<RemotePluginsPanel />)
    const card = await endpoint()
    await tested(user, card)
    expect(within(card).queryByRole('switch', { name: /^Show / })).toBeNull()
    expect(within(card).queryByRole('button', { name: 'Save tool settings' })).toBeNull()
    await choose(user, within(card).getByRole('combobox', { name: 'Access of retain' }), 'off')
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Lower to off' }))
    await waitFor(() => expect(within(card).getByRole('combobox', { name: 'Access of retain' })).toHaveTextContent('off'))
    expect((await hindsight()).disabled_tools).toEqual(['retain'])
  })

  it('lowering asks once, raising does not', async () => {
    const { user } = renderPage(<RemotePluginsPanel />)
    const card = await endpoint()
    await tested(user, card)
    await choose(user, within(card).getByRole('combobox', { name: 'Access of retain' }), 'write')
    const dialog = await screen.findByRole('alertdialog')
    expect(within(dialog).getByRole('heading')).toHaveTextContent('Lower retain?')
    await user.click(within(dialog).getByRole('button', { name: 'Lower to write' }))
    await waitFor(() => expect(within(card).getByRole('combobox', { name: 'Access of retain' })).toHaveTextContent('write'))
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect((await hindsight()).tool_tiers).toMatchObject({ retain: 'write' })

    // Raising recall back to its default asks nothing.
    await choose(user, within(card).getByRole('combobox', { name: 'Access of recall' }), 'outward (asks first) (default)')
    await waitFor(() => expect(within(card).queryByRole('button', { name: 'Reset recall' })).toBeNull())
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(within(card).getByText('server suggests read')).toBeInTheDocument()
    expect((await hindsight()).tool_tiers).toEqual({ retain: 'write' })
  })

  it('enables the plugin with its source switch, the one enable control', async () => {
    const { user } = renderPage(<RemotePluginsPanel />)
    const card = await endpoint()
    const source = await within(card).findByRole('region', { name: 'Source hindsight' })
    expect(within(card).queryByRole('button', { name: /^(Enable|Disable)$/ })).toBeNull()
    expect(within(card).getAllByRole('switch')).toHaveLength(1)
    await user.click(within(source).getByRole('switch', { name: 'Enabled' }))
    await waitFor(() => expect(badge(card, 'Enabled')).toBeInTheDocument())
    expect(badge(card, 'Disabled')).toBeNull()
    expect((await hindsight()).enabled).toBe(true)
  })

  it('folds the test line to its status (#3048)', async () => {
    const { user } = renderPage(<RemotePluginsPanel />)
    const card = await endpoint()
    await user.click(within(card).getByRole('button', { name: 'Test connection' }))
    const status = await within(card).findByText('connected; 5 tools (hindsight 0.4.1)')
    expect(status).toHaveAttribute('role', 'status')
    // The rows stay folded under the source's summary until it is opened.
    const fold = within(card).getByRole('button', { name: 'hindsight' })
    expect(fold).toHaveAttribute('aria-expanded', 'false')
    expect(within(card).getByText('5 tools, 1 changed')).toBeInTheDocument()
    expect(within(card).getByRole('combobox', { name: 'Access of retain', hidden: true })).not.toBeVisible()
    await user.click(fold)
    expect(within(card).getByRole('combobox', { name: 'Access of retain' })).toBeVisible()
    await user.click(fold)
    expect(status).toBeVisible()
  })

  it('says a failed test as a warning line, and keeps the last listing', async () => {
    const { user } = renderPage(<RemotePluginsPanel />)
    const card = await endpoint()
    await tested(user, card)
    server.use(
      http.post('/api/v1/ai/capabilities/:source/refresh', async () => {
        const source = await aiCapabilities.source('remote:hindsight')
        const test = { ok: false, detail: 'connection refused', duration_ms: 3, server: null, tools: [], truncated: false }
        return HttpResponse.json({ test, source } satisfies RefreshResult)
      }),
    )
    await user.click(within(card).getByRole('button', { name: 'Test connection' }))
    expect(await within(card).findByText('connection refused')).toHaveAttribute('role', 'status')
    expect(within(card).getByRole('combobox', { name: 'Access of retain' })).toBeVisible()
  })

  it('says a refused test as an alert, and keeps the last listing', async () => {
    const { user } = renderPage(<RemotePluginsPanel />)
    const card = await endpoint()
    await tested(user, card)
    server.use(
      http.post('/api/v1/ai/capabilities/:source/refresh', () => HttpResponse.json({ detail: 'bad gateway' }, { status: 502 })),
    )
    await user.click(within(card).getByRole('button', { name: 'Test connection' }))
    expect(await within(card).findByRole('alert')).toHaveTextContent('bad gateway')
    expect(within(card).getByRole('combobox', { name: 'Access of retain' })).toBeVisible()
  })

  it('fixes both tools of a colliding pair off, with the reason', async () => {
    const { user } = renderPage(<RemotePluginsPanel />)
    const card = await endpoint()
    await tested(user, card)
    for (const [name, other] of [['notes.get', 'notes_get'], ['notes_get', 'notes.get']] as const) {
      const select = within(card).getByRole('combobox', { name: `Access of ${name}` })
      expect(select).toHaveTextContent('off')
      expect(select).toBeDisabled()
      expect(within(card).getByText(`Shares its name with ${other}, so both are hidden`)).toBeInTheDocument()
    }
  })

  it("says why a plugin's tools could not be read", async () => {
    server.use(
      http.get('/api/v1/ai/capabilities/:source', () => HttpResponse.json({ detail: 'listing store down' }, { status: 500 })),
    )
    const { user } = renderPage(<RemotePluginsPanel />)
    await endpoint()
    await user.type(screen.getByLabelText('Name'), 'notes')
    await user.type(screen.getByLabelText('MCP endpoint URL'), 'https://notes.example/mcp')
    await user.click(screen.getByRole('button', { name: 'Add endpoint' }))
    const card = await endpoint('notes')
    expect(await within(card).findByRole('alert')).toHaveTextContent("Could not read the plugin’s tools: listing store down")
    expect(within(card).getByRole('button', { name: 'Test connection' })).toBeInTheDocument()
  })

  it('removes an endpoint without reading the tree again', async () => {
    const reads = { count: 0 }
    server.events.on('request:start', ({ request }) => {
      if (request.method === 'GET' && new URL(request.url).pathname === '/api/v1/ai/capabilities') reads.count++
    })
    const { user } = renderPage(<RemotePluginsPanel />)
    const card = await endpoint()
    await within(card).findByRole('region', { name: 'Source hindsight' })
    await user.click(within(card).getByRole('button', { name: 'Remove' }))
    await user.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Remove endpoint' }))
    expect(await screen.findByText('No plugin endpoints.')).toBeInTheDocument()
    expect(screen.queryByText(/Loading the plugins/)).toBeNull()
    expect(reads.count).toBe(1)
  })

  it("keeps the endpoint's buttons when its tools cannot be read, and says why unless the agent has no database", async () => {
    server.use(http.get('/api/v1/ai/capabilities', () => HttpResponse.json({ detail: 'agent broke' }, { status: 500 })))
    const first = renderPage(<RemotePluginsPanel />)
    expect(await screen.findByRole('alert')).toHaveTextContent("Could not load the plugins’ tools: agent broke")
    const card = await endpoint()
    expect(within(card).getByRole('button', { name: 'Test connection' })).toBeInTheDocument()
    expect(within(card).getByRole('button', { name: 'Remove' })).toBeInTheDocument()
    first.unmount()

    server.use(http.get('/api/v1/ai/capabilities', () => HttpResponse.json({ detail: 'no database' }, { status: 503 })))
    renderPage(<RemotePluginsPanel />)
    await endpoint()
    await waitFor(() => expect(screen.queryByText(/Loading the plugins/)).toBeNull())
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('removes an endpoint after confirming', async () => {
    const { user } = renderPage(<RemotePluginsPanel />)
    const card = await endpoint()
    await within(card).findByRole('region', { name: 'Source hindsight' })
    await user.click(within(card).getByRole('button', { name: 'Remove' }))
    await user.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Remove endpoint' }))
    expect(await screen.findByText('No plugin endpoints.')).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Source hindsight' })).toBeNull()
  })

  it('lists no built-in tool sets here', async () => {
    renderPage(<RemotePluginsPanel />)
    await endpoint()
    expect(screen.queryByRole('list', { name: 'Built-in tools' })).toBeNull()
    expect(screen.queryByRole('listitem', { name: /^Built-in tools / })).toBeNull()
    expect(screen.queryByText('Built in')).toBeNull()
    const regions = await screen.findAllByRole('region', { name: /^Source / })
    expect(regions.map((r) => r.getAttribute('aria-label'))).toEqual(['Source hindsight'])
    expect(screen.getByText(/whose tools the assistant may use/)).not.toHaveTextContent(/raise a tool/)
  })

  it("marks the endpoint's secret controls user-only and leaves access controls unmarked", async () => {
    const { user } = renderPage(<RemotePluginsPanel />)
    const card = await endpoint()
    for (const name of ['Test connection', 'Remove']) {
      expect(isUserOnly(within(card).getByRole('button', { name }))).toBe(true)
    }
    expect(isUserOnly(screen.getByRole('button', { name: 'Add endpoint' }))).toBe(true)
    expect(isUserOnly(screen.getByLabelText('Header value'))).toBe(true)

    await tested(user, card)
    expect(isUserOnly(within(card).getByRole('switch', { name: 'Enabled' }))).toBe(false)
    const selects = within(card).getAllByRole('combobox', { name: /^Access of / })
    expect(selects).toHaveLength(5)
    for (const select of selects) expect(isUserOnly(select)).toBe(false)
    expect(isUserOnly(within(card).getByRole('button', { name: 'Reset recall' }))).toBe(false)
  })
})
