import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { isUserOnly } from '../../agent/dom'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { RemotePluginsPanel } from './RemotePlugins'

describe('RemotePluginsPanel', () => {
  it('lists endpoints with the header name and last four only', async () => {
    renderPage(<RemotePluginsPanel />)
    const card = await screen.findByRole('listitem', { name: 'Plugin endpoint hindsight' })
    expect(within(card).getByText('Authorization: …9f3a')).toBeInTheDocument()
    expect(within(card).getByText('Disabled')).toBeInTheDocument()
  })

  it('registers an endpoint with a write-only secret', async () => {
    let sent: unknown
    server.events.on('request:start', ({ request }) => {
      if (request.method === 'POST' && request.url.endsWith('/ai/plugins')) void request.clone().json().then((b) => (sent = b))
    })
    const { user } = renderPage(<RemotePluginsPanel />)
    await screen.findByRole('listitem', { name: 'Plugin endpoint hindsight' })
    await user.type(screen.getByLabelText('Name'), 'notes')
    await user.type(screen.getByLabelText('MCP endpoint URL'), 'https://notes.example/mcp')
    expect(screen.getByLabelText('Header value')).toHaveAttribute('type', 'password')
    await user.type(screen.getByLabelText('Header value'), 'Bearer abcd1234')
    await user.click(screen.getByRole('button', { name: 'Add endpoint' }))
    const card = await screen.findByRole('listitem', { name: 'Plugin endpoint notes' })
    expect(within(card).getByText('Authorization: …1234')).toBeInTheDocument()
    expect(sent).toEqual({
      name: 'notes',
      url: 'https://notes.example/mcp',
      secret: 'Bearer abcd1234',
      auth_header: 'Authorization',
    })
  })

  it('shows a refusal', async () => {
    const { user } = renderPage(<RemotePluginsPanel />)
    await screen.findByRole('listitem', { name: 'Plugin endpoint hindsight' })
    await user.type(screen.getByLabelText('Name'), 'hindsight')
    await user.type(screen.getByLabelText('MCP endpoint URL'), 'https://x.example/mcp')
    await user.click(screen.getByRole('button', { name: 'Add endpoint' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('already exists')
  })

  it('tests the connection, reviews tool tiers (renamed tools stay outward), saves, and enables', async () => {
    const { user } = renderPage(<RemotePluginsPanel />)
    const card = await screen.findByRole('listitem', { name: 'Plugin endpoint hindsight' })
    await user.click(within(card).getByRole('button', { name: 'Test connection' }))
    expect(await within(card).findByRole('status')).toHaveTextContent('connected; 3 tools')
    expect(within(card).getByLabelText('Tier of recall')).toHaveValue('read')
    expect(within(card).getByLabelText('Tier of retain')).toHaveValue('outward')
    expect(within(card).getByLabelText('Tier of files.list')).toBeDisabled()
    expect(within(card).getByText('(server suggests read)')).toBeInTheDocument()

    await user.selectOptions(within(card).getByLabelText('Tier of retain'), 'write')
    await user.click(within(card).getByLabelText('Hide files.list'))
    await user.click(within(card).getByRole('button', { name: 'Save tool settings' }))
    await user.click(within(card).getByRole('button', { name: 'Test connection' }))
    await waitFor(() => expect(within(card).getByLabelText('Tier of retain')).toHaveValue('write'))

    await waitFor(() => expect(within(card).getByRole('button', { name: 'Enable' })).toBeEnabled())
    await user.click(within(card).getByRole('button', { name: 'Enable' }))
    expect(await within(card).findByText('Enabled')).toBeInTheDocument()
  })

  it('removes an endpoint after confirming', async () => {
    const { user } = renderPage(<RemotePluginsPanel />)
    const card = await screen.findByRole('listitem', { name: 'Plugin endpoint hindsight' })
    await user.click(within(card).getByRole('button', { name: 'Remove' }))
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Remove endpoint' }))
    expect(await screen.findByText('No plugin endpoints.')).toBeInTheDocument()
  })

  it('lists the built-in tool sets first, marked built in, with no Remove', async () => {
    renderPage(<RemotePluginsPanel />)
    const builtIns = await screen.findByRole('list', { name: 'Built-in tools' })
    const cards = within(builtIns).getAllByRole('listitem')
    expect(cards.map((c) => c.getAttribute('aria-label'))).toEqual([
      'Built-in tools scadbuddy',
      'Built-in tools playwright',
    ])
    for (const card of cards) {
      expect(within(card).getByText('Built in')).toBeInTheDocument()
      expect(within(card).queryByRole('button', { name: 'Remove' })).not.toBeInTheDocument()
      expect(within(card).queryByRole('button', { name: 'Test connection' })).not.toBeInTheDocument()
    }
    // ScadBuddy's own tools have no switch as a set; the headless browser's does.
    expect(within(cards[0]!).queryByRole('button', { name: /^(Enable|Disable)$/ })).not.toBeInTheDocument()
    expect(within(cards[1]!).getByRole('button', { name: 'Enable' })).toBeInTheDocument()
  })

  it('offers only tiers at or above a built-in tool’s own, and saves raised tiers and disabled tools', async () => {
    let sent: unknown
    server.events.on('request:start', ({ request }) => {
      if (request.method === 'PATCH' && request.url.endsWith('/ai/plugins/scadbuddy')) void request.clone().json().then((b) => (sent = b))
    })
    const { user } = renderPage(<RemotePluginsPanel />)
    const card = await screen.findByRole('listitem', { name: 'Built-in tools scadbuddy' })
    await user.click(within(card).getByRole('button', { name: 'Review tools' }))
    const options = (label: string) =>
      within(within(card).getByLabelText(label)).getAllByRole('option').map((o) => (o as HTMLOptionElement).value)
    expect(options('Tier of list_models')).toEqual(['read', 'write', 'outward'])
    expect(options('Tier of save_model')).toEqual(['write', 'outward'])
    expect(options('Tier of send_to_printer')).toEqual(['outward'])
    expect(within(card).getByLabelText('Tier of save_model')).toHaveValue('write')

    await user.selectOptions(within(card).getByLabelText('Tier of list_models'), 'outward')
    await user.click(within(card).getByLabelText('Disable save_model'))
    await user.click(within(card).getByRole('button', { name: 'Save tool settings' }))
    await waitFor(() => expect(sent).toEqual({ tool_tiers: { list_models: 'outward' }, disabled_tools: ['save_model'] }))
    expect(await within(card).findByText(/1 raised, 1 disabled/)).toBeInTheDocument()
  })

  it('switches the headless browser set with its own switch', async () => {
    const { user } = renderPage(<RemotePluginsPanel />)
    const card = await screen.findByRole('listitem', { name: 'Built-in tools playwright' })
    await user.click(within(card).getByRole('button', { name: 'Enable' }))
    expect(await within(card).findByText('Enabled')).toBeInTheDocument()
  })

  it('marks every write user-only', async () => {
    renderPage(<RemotePluginsPanel />)
    const card = await screen.findByRole('listitem', { name: 'Plugin endpoint hindsight' })
    for (const name of ['Test connection', 'Enable', 'Remove']) {
      expect(isUserOnly(within(card).getByRole('button', { name }))).toBe(true)
    }
    expect(isUserOnly(screen.getByRole('button', { name: 'Add endpoint' }))).toBe(true)
    const browser = screen.getByRole('listitem', { name: 'Built-in tools playwright' })
    expect(isUserOnly(within(browser).getByRole('button', { name: 'Enable' }))).toBe(true)
    expect(isUserOnly(screen.getByLabelText('Header value'))).toBe(true)
  })
})
