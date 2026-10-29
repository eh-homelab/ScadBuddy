import { renderHook, screen, waitFor, within } from '@testing-library/react'
import { accessName } from '../../test/select'
import { http, HttpResponse } from 'msw'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { isUserOnly } from '../../agent/dom'
import type { ItemView, SourceView } from '../../api/capabilities'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { SourceCard } from './SourceCard'
import { useCapabilities } from './useCapabilities'

function tool(n: number, over: Partial<ItemView> = {}): ItemView {
  return {
    id: `tool_${n}`,
    kind: 'tool',
    name: `tool_${n}`,
    harness_name: `mcp__scadbuddy__tool_${n}`,
    label: `Tool ${n}`,
    description: `Does thing ${n}.`,
    default: 'read',
    default_note: null,
    suggested: null,
    access: 'read',
    overridden: false,
    missing: false,
    allowed: null,
    fixed: null,
    reason: null,
    ...over,
  }
}

function source(over: Partial<SourceView> = {}, count = 10): SourceView {
  return {
    id: 'builtin:scadbuddy',
    kind: 'builtin',
    title: 'ScadBuddy tools',
    enabled: true,
    switchable: false,
    options_route: null,
    listed_at: null,
    truncated: false,
    items: Array.from({ length: count }, (_, i) => tool(i + 1, i === 0 ? { access: 'off', overridden: true } : {})),
    children: [],
    ...over,
  }
}

function Harness({ start }: { start: SourceView }) {
  const [shown, setShown] = useState(start)
  return <SourceCard source={shown} onChange={setShown} />
}

const card = (title: string) => screen.getByRole('region', { name: `Source ${title}` })

describe('SourceCard', () => {
  it('folds to its summary, and opens to its rows', async () => {
    const { user } = renderPage(<Harness start={source()} />)
    expect(within(card('ScadBuddy tools')).getByText('10 tools, 1 changed')).toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: accessName('tool_1') })).toBeNull()
    await user.click(screen.getByRole('button', { name: 'ScadBuddy tools' }))
    expect(screen.getAllByRole('combobox', { name: /^Access of / })).toHaveLength(10)
  })

  it('the switch enables a switchable source, is not user-only, and is absent otherwise', async () => {
    const http_ = source({ id: 'builtin:http', title: 'HTTP requests', switchable: true }, 1)
    const { user } = renderPage(<Harness start={http_} />)
    // Named for its source, so several switches on one page sound apart; the visible label stays.
    const toggle = screen.getByRole('switch', { name: 'Enable HTTP requests' })
    expect(within(card('HTTP requests')).getByText('Enabled')).toBeVisible()
    expect(toggle).toBeChecked()
    expect(isUserOnly(toggle)).toBe(false)
    await user.click(toggle)
    await waitFor(() => expect(screen.getByRole('switch', { name: 'Enable HTTP requests' })).not.toBeChecked())

    renderPage(<Harness start={source()} />)
    expect(within(card('ScadBuddy tools')).queryByRole('switch')).toBeNull()
  })

  it('filters rows by label, name and description', async () => {
    const items = [
      ...source().items.slice(0, 7),
      tool(8, { label: 'Printer camera' }),
      tool(9, { name: 'zebra_tool', id: 'zebra_tool' }),
      tool(10, { description: 'Reads a README.' }),
    ]
    const { user } = renderPage(<Harness start={source({ items })} />)
    await user.click(screen.getByRole('button', { name: 'ScadBuddy tools' }))
    const filter = screen.getByRole('textbox', { name: 'Filter ScadBuddy tools' })
    await user.type(filter, 'CAMERA')
    expect(screen.getAllByRole('combobox', { name: /^Access of / }).map((c) => c.getAttribute('aria-label'))).toEqual([
      'Access of tool_8 (Printer camera)',
    ])
    await user.clear(filter)
    await user.type(filter, 'zebra')
    expect(screen.getByRole('combobox', { name: accessName('zebra_tool') })).toBeInTheDocument()
    expect(screen.getAllByRole('combobox', { name: /^Access of / })).toHaveLength(1)
    await user.clear(filter)
    await user.type(filter, 'readme')
    expect(screen.getAllByRole('combobox', { name: /^Access of / }).map((c) => c.getAttribute('aria-label'))).toEqual([
      'Access of tool_10 (Tool 10)',
    ])
  })

  it('shows no filter for eight items or fewer', async () => {
    const { user } = renderPage(<Harness start={source({}, 8)} />)
    await user.click(screen.getByRole('button', { name: 'ScadBuddy tools' }))
    expect(screen.getAllByRole('combobox', { name: /^Access of / })).toHaveLength(8)
    expect(screen.queryByRole('textbox', { name: 'Filter ScadBuddy tools' })).toBeNull()
  })

  it('nests child sources', async () => {
    const child = source({ id: 'package:x/server', kind: 'package_server', title: 'Child server' }, 2)
    const { user } = renderPage(
      <Harness start={source({ children: [child] }, 1)} />,
    )
    await user.click(screen.getByRole('button', { name: 'ScadBuddy tools' }))
    const nested = within(card('ScadBuddy tools')).getByRole('region', { name: 'Source Child server' })
    expect(within(nested).getByText('2 tools, 1 changed')).toBeInTheDocument()
  })

  it("labels a non-built-in source's annotations as the server's claims, by its kind", async () => {
    const remote = source({ id: 'builtin:looks-built-in', kind: 'remote', title: 'Remote' }, 1)
    server.use(
      http.get('/api/v1/ai/capabilities/:source/items/:item', () =>
        HttpResponse.json({
          item: remote.items[0],
          input_schema: null,
          output_schema: null,
          annotations: { readOnlyHint: true },
          raw: null,
          omitted: [],
        }),
      ),
    )
    const { user } = renderPage(<Harness start={remote} />)
    await user.click(screen.getByRole('button', { name: 'Remote' }))
    await user.click(screen.getByRole('button', { name: 'Details of tool_1' }))
    expect(await screen.findByText('Claims made by the server, not checked by ScadBuddy.')).toBeInTheDocument()
  })

  it('shows its header extra and its children', async () => {
    const { user } = renderPage(
      <SourceCard source={source({}, 1)} onChange={() => {}} headerExtra={<span>extra</span>}>
        <p>options here</p>
      </SourceCard>,
    )
    expect(screen.getByText('extra')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'ScadBuddy tools' }))
    expect(screen.getByText('options here')).toBeInTheDocument()
  })
})

describe('useCapabilities', () => {
  it('lists once, filters, and replaces one source in place', async () => {
    const calls = vi.fn()
    server.events.on('request:start', ({ request }) => {
      if (request.method === 'GET' && new URL(request.url).pathname === '/api/v1/ai/capabilities') calls()
    })
    const { result } = renderHook(() => useCapabilities((s) => s.kind === 'builtin'))
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.error).toBeNull()
    expect(result.current.sources.map((s) => s.id)).toEqual(['builtin:scadbuddy', 'builtin:browser', 'builtin:http', 'builtin:own-plugin'])
    const replaced = { ...result.current.sources[2]!, enabled: false }
    result.current.replace(replaced)
    await waitFor(() => expect(result.current.sources[2]!.enabled).toBe(false))
    expect(calls).toHaveBeenCalledTimes(1)
  })

  it('says why the list failed', async () => {
    server.use(http.get('/api/v1/ai/capabilities', () => HttpResponse.json({ detail: 'agent down' }, { status: 503 })))
    const { result } = renderHook(() => useCapabilities(() => true))
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.error?.message).toContain('agent down')
    expect(result.current.sources).toEqual([])
  })
})
