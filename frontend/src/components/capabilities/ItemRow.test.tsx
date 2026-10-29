import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'
import { isUserOnly } from '../../agent/dom'
import type { Access, ItemView, SourceView } from '../../api/capabilities'
import { server } from '../../mocks/server'
import { choose, optionsOf } from '../../test/select'
import { renderPage } from '../../test/utils'
import { ItemRow } from './ItemRow'

// Radix Select scrolls its selected option into view; jsdom has no layout.
Element.prototype.scrollIntoView ??= () => {}

const BASE = '/api/v1/ai/capabilities'

function item(over: Partial<ItemView> = {}): ItemView {
  return {
    id: 'send_to_printer',
    kind: 'tool',
    name: 'send_to_printer',
    harness_name: 'mcp__scadbuddy__send_to_printer',
    label: 'Send to printer',
    description: 'Sends an output to a printer and starts the print.',
    default: 'outward',
    default_note: null,
    suggested: null,
    access: 'outward',
    overridden: false,
    missing: false,
    allowed: null,
    fixed: null,
    reason: null,
    ...over,
  }
}

type Sent = { method: string; access?: Access }

/** Answers the row's writes as the agent would for one item, and records them. */
function serve(start: ItemView, source = 'builtin:scadbuddy'): Sent[] {
  const sent: Sent[] = []
  let current = start
  const view = (): SourceView => ({
    id: source,
    kind: 'builtin',
    title: 'ScadBuddy tools',
    enabled: true,
    switchable: false,
    options_route: null,
    listed_at: null,
    truncated: false,
    items: [current],
    children: [],
  })
  server.use(
    http.put(`${BASE}/:source/items/:item`, async ({ request }) => {
      const { access } = (await request.json()) as { access: Access }
      sent.push({ method: 'PUT', access })
      current = { ...current, access, overridden: access !== current.default }
      return HttpResponse.json(view())
    }),
    http.delete(`${BASE}/:source/items/:item`, () => {
      sent.push({ method: 'DELETE' })
      current = { ...current, access: current.default, overridden: false }
      return HttpResponse.json(view())
    }),
  )
  return sent
}

function Harness({ start, source = 'builtin:scadbuddy' }: { start: ItemView; source?: string }) {
  const [shown, setShown] = useState(start)
  return <ItemRow source={source} item={shown} onSaved={(s) => setShown(s.items.find((i) => i.id === shown.id)!)} />
}

const select = (name: string) => screen.getByRole('combobox', { name: `Access of ${name}` })

describe('ItemRow', () => {
  it('shows label, description and the raw name, with the default marked', async () => {
    const { user } = renderPage(<Harness start={item()} />)
    expect(screen.getByText('Send to printer')).toBeInTheDocument()
    expect(screen.getByText('Sends an output to a printer and starts the print.')).toHaveClass('line-clamp-2')
    expect(screen.getByText('mcp__scadbuddy__send_to_printer')).toHaveClass('sb-num', 'text-muted-foreground')
    expect(select('send_to_printer')).toHaveTextContent('outward (asks first) (default)')
    expect(await optionsOf(user, select('send_to_printer'))).toEqual(['off', 'read', 'write', 'outward (asks first) (default)'])
  })

  it('raising applies at once with no confirmation', async () => {
    const start = item({ id: 'list_models', name: 'list_models', label: 'List models', default: 'read', access: 'read' })
    const sent = serve(start)
    const { user } = renderPage(<Harness start={start} />)
    await choose(user, select('list_models'), 'write')
    expect(screen.queryByRole('alertdialog')).toBeNull()
    await waitFor(() => expect(select('list_models')).toHaveTextContent('write'))
    expect(sent).toEqual([{ method: 'PUT', access: 'write' }])
  })

  it('lowering an outward tool asks one line, and confirming saves it', async () => {
    const sent = serve(item())
    const { user } = renderPage(<Harness start={item()} />)
    await choose(user, select('send_to_printer'), 'read')
    const dialog = await screen.findByRole('alertdialog', { name: 'Lower Send to printer?' })
    expect(within(dialog).getByText('It will run without asking first.')).toBeInTheDocument()
    expect(within(dialog).queryByRole('checkbox')).toBeNull()
    expect(sent).toEqual([])
    await user.click(within(dialog).getByRole('button', { name: 'Lower to read' }))
    await waitFor(() => expect(select('send_to_printer')).toHaveTextContent('read'))
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(sent).toEqual([{ method: 'PUT', access: 'read' }])
  })

  it.each([
    ['off', item(), 'off', 'The assistant will not be able to use it.'],
    ['write from read', item({ access: 'read', overridden: true }), 'write', 'It will be able to change things without asking first.'],
    ['read from outward', item(), 'read', 'It will run without asking first.'],
    [
      'write from read with a by-call default',
      item({ id: 'http_request', name: 'http_request', label: 'Make an HTTP request', default: null, access: 'read', overridden: true }),
      'write',
      'It will be able to change things without asking first.',
    ],
    [
      'read from a write default',
      item({ id: 'save_model', name: 'save_model', label: 'Save a model', default: 'write', access: 'write' }),
      'read',
      'Callers limited to read will be able to use it.',
    ],
  ] as const)('says the line that fits each lowering: %s', async (_, start, to, line) => {
    serve(start)
    const { user } = renderPage(<Harness start={start} />)
    await choose(user, select(start.name), to)
    const dialog = await screen.findByRole('alertdialog')
    expect(within(dialog).getByText(line)).toBeInTheDocument()
  })

  it('cancelling the confirmation sends nothing and keeps the access', async () => {
    const sent = serve(item())
    const { user } = renderPage(<Harness start={item()} />)
    await choose(user, select('send_to_printer'), 'off')
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(select('send_to_printer')).toHaveTextContent('outward (asks first) (default)')

    await choose(user, select('send_to_printer'), 'off')
    await screen.findByRole('alertdialog')
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(select('send_to_printer')).toHaveTextContent('outward (asks first) (default)')
    expect(sent).toEqual([])
  })

  it('returns focus to the select after a confirmed lowering, a cancel and Escape, and ignores a choice while saving', async () => {
    let release: () => void = () => {}
    const held = new Promise<void>((resolve) => (release = resolve))
    let puts = 0
    serve(item())
    server.use(
      http.put(`${BASE}/:source/items/:item`, async () => {
        puts += 1
        await held
        return HttpResponse.json({ detail: 'held' }, { status: 409 })
      }),
    )
    const { user } = renderPage(<Harness start={item()} />)
    await choose(user, select('send_to_printer'), 'off')
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(select('send_to_printer')).toHaveFocus())

    await choose(user, select('send_to_printer'), 'off')
    await screen.findByRole('alertdialog')
    await user.keyboard('{Escape}')
    await waitFor(() => expect(select('send_to_printer')).toHaveFocus())

    await choose(user, select('send_to_printer'), 'read')
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Lower to read' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    // The save is still running: the select keeps focus while it does.
    await waitFor(() => expect(select('send_to_printer')).toHaveFocus())
    // A choice while the save runs is ignored.
    await choose(user, select('send_to_printer'), 'write')
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(puts).toBe(1)
    release()
    expect(await screen.findByRole('alert')).toHaveTextContent('held')
  })

  it('choosing the default resets the item', async () => {
    const start = item({ access: 'write', overridden: true })
    const sent = serve(start)
    const { user } = renderPage(<Harness start={start} />)
    await choose(user, select('send_to_printer'), 'outward (asks first) (default)')
    await waitFor(() => expect(select('send_to_printer')).toHaveTextContent('outward (asks first) (default)'))
    expect(sent).toEqual([{ method: 'DELETE' }])
  })

  it('Reset appears only when overridden and resets', async () => {
    const start = item({ access: 'off', overridden: true })
    const sent = serve(start)
    const { user } = renderPage(<Harness start={start} />)
    await user.click(screen.getByRole('button', { name: 'Reset send_to_printer' }))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Reset send_to_printer' })).toBeNull())
    expect(select('send_to_printer')).toHaveTextContent('outward (asks first) (default)')
    expect(select('send_to_printer')).toHaveFocus()
    expect(sent).toEqual([{ method: 'DELETE' }])
  })

  it("a fixed item's select is disabled and says why", () => {
    const reason = 'Two servers list a tool under this name, so it stays off'
    renderPage(<Harness start={item({ access: 'off', fixed: { access: 'off', reason } })} />)
    expect(select('send_to_printer')).toBeDisabled()
    expect(select('send_to_printer')).toHaveTextContent('off')
    expect(screen.getByText(reason)).toBeInTheDocument()
  })

  it('a renamed tool offers off and outward, with its reason', async () => {
    const reason = 'Claude Code renames this tool, so it cannot take a tier: it asks first, or is off'
    const start = item({ id: 'files.list', name: 'files.list', label: 'files.list', allowed: ['off', 'outward'], reason })
    const { user } = renderPage(<Harness start={start} source="remote:hindsight" />)
    expect(select('files.list')).toBeEnabled()
    expect(screen.getByText(reason)).toBeInTheDocument()
    expect(await optionsOf(user, select('files.list'))).toEqual(['off', 'outward (asks first) (default)'])
  })

  it('By call (default) for http_request, and read asks to lower', async () => {
    const start = item({
      id: 'http_request',
      name: 'http_request',
      label: 'Make an HTTP request',
      default: null,
      default_note: 'read for GET and HEAD, outward (asks first) for anything else',
      access: null,
    })
    const sent = serve(start, 'builtin:http')
    const { user } = renderPage(<Harness start={start} source="builtin:http" />)
    expect(select('http_request')).toHaveTextContent('By call (default)')
    expect(screen.getByText('read for GET and HEAD, outward (asks first) for anything else')).toBeInTheDocument()
    expect(await optionsOf(user, select('http_request'))).toEqual(['By call (default)', 'off', 'read', 'write', 'outward (asks first)'])
    await choose(user, select('http_request'), 'read')
    const dialog = await screen.findByRole('alertdialog', { name: 'Lower Make an HTTP request?' })
    expect(within(dialog).getByText('It will run without asking first.')).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Lower to read' }))
    await waitFor(() => expect(sent).toEqual([{ method: 'PUT', access: 'read' }]))
  })

  it("shows the server's suggestion and a missing item", () => {
    renderPage(<Harness start={item({ suggested: 'read' })} source="remote:hindsight" />)
    expect(screen.getByText('server suggests read')).toBeInTheDocument()
    renderPage(<Harness start={item({ id: 'gone', name: 'gone', label: 'gone', missing: true, overridden: true, access: 'off' })} />)
    expect(screen.getByText('Not in the last listing')).toBeInTheDocument()
  })

  it('shows no suggestion that matches the access', () => {
    renderPage(<Harness start={item({ suggested: 'outward' })} source="remote:hindsight" />)
    expect(screen.queryByText(/server suggests/)).toBeNull()
  })

  it('shows a refusal', async () => {
    server.use(http.put(`${BASE}/:source/items/:item`, () => HttpResponse.json({ detail: 'nope, refused' }, { status: 409 })))
    const start = item({ id: 'list_models', name: 'list_models', default: 'read', access: 'read' })
    const { user } = renderPage(<Harness start={start} />)
    await choose(user, select('list_models'), 'write')
    expect(await screen.findByRole('alert')).toHaveTextContent('nope, refused')
    expect(select('list_models')).toHaveTextContent('read (default)')
  })

  it('leaves every access control to the agent too', async () => {
    serve(item({ access: 'write', overridden: true }))
    const { user } = renderPage(<Harness start={item({ access: 'write', overridden: true })} />)
    expect(isUserOnly(select('send_to_printer'))).toBe(false)
    expect(isUserOnly(screen.getByRole('button', { name: 'Reset send_to_printer' }))).toBe(false)
    await choose(user, select('send_to_printer'), 'off')
    const dialog = await screen.findByRole('alertdialog')
    expect(isUserOnly(within(dialog).getByRole('button', { name: 'Cancel' }))).toBe(false)
    expect(isUserOnly(within(dialog).getByRole('button', { name: 'Lower to off' }))).toBe(false)
  })
})
