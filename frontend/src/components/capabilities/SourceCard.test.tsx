import { act, renderHook, screen, waitFor, within } from '@testing-library/react'
import { accessName } from '../../test/select'
import { http, HttpResponse } from 'msw'
import { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { isUserOnly } from '../../agent/dom'
import type { ItemView, SourceView } from '../../api/capabilities'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { SourceCard } from './SourceCard'
import { POLL_MAX, POLL_MS, useCapabilities } from './useCapabilities'

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
    // The item's own access follows `access` unless a test sets it.
    own_access: over.access === undefined ? 'read' : over.access,
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
    problem: null,
    checking: false,
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

  it('counts a row at the default tier (a migrated one, #4994) as changed, and offers its Reset', async () => {
    const start = source({ items: [tool(1, { access: 'read', overridden: true }), tool(2)] })
    const { user } = renderPage(<Harness start={start} />)
    expect(within(card('ScadBuddy tools')).getByText('2 tools, 1 changed')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'ScadBuddy tools' }))
    expect(screen.getByRole('button', { name: 'Reset tool_1' })).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Reset tool_2' })).toBeNull()
  })

  it('the switch enables a switchable source, is not user-only, and is absent otherwise', async () => {
    const http_ = source({ id: 'builtin:http', title: 'HTTP requests', switchable: true }, 1)
    const { user } = renderPage(<Harness start={http_} />)
    // Named for its source, so several switches on one page sound apart; the visible label stays.
    const toggle = screen.getByRole('switch', { name: 'Enabled (HTTP requests)' })
    expect(within(card('HTTP requests')).getByText('Enabled')).toBeVisible()
    expect(toggle).toBeChecked()
    expect(isUserOnly(toggle)).toBe(false)
    await user.click(toggle)
    await waitFor(() => expect(screen.getByRole('switch', { name: 'Enabled (HTTP requests)' })).not.toBeChecked())

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

  it('shows a load problem once, keeps the switch on, and switching off clears it (#4757)', async () => {
    const problem = 'plugin package greeter is not loaded: skills/hello/SKILL.md changed since it was approved'
    const child = source({ id: 'package:greeter/mcp:mem', kind: 'package_server', title: 'mem', switchable: true, problem }, 1)
    const refused = source({ id: 'package:greeter', kind: 'package', title: 'greeter', enabled: false, switchable: true, problem, children: [child] }, 1)
    let sent: unknown
    server.use(
      http.put('/api/v1/ai/capabilities/:source', async ({ request }) => {
        sent = await request.json()
        return HttpResponse.json({ ...refused, problem: null, enabled: false, children: [{ ...child, problem: null }] })
      }),
    )
    const { user } = renderPage(<Harness start={refused} />)
    await user.click(screen.getByRole('button', { name: 'greeter' }))
    expect(within(card('greeter')).getAllByText(problem)).toHaveLength(1)
    // Switched on, though a turn does not load it: switching off is what is offered.
    const toggle = screen.getByRole('switch', { name: 'Enabled (greeter)' })
    expect(toggle).toBeChecked()
    await user.click(toggle)
    await waitFor(() => expect(screen.getByRole('switch', { name: 'Enabled (greeter)' })).not.toBeChecked())
    expect(sent).toEqual({ enabled: false })
    expect(screen.queryByText(problem)).toBeNull()
  })

  it('puts the extra for each nested source in its header', async () => {
    const child = source({ id: 'package:x/mcp:s', kind: 'package_server', title: 'Child server' }, 1)
    const { user } = renderPage(
      <SourceCard source={source({ children: [child] }, 1)} onChange={() => {}} childExtra={(c) => <span>{`extra of ${c.title}`}</span>} />,
    )
    await user.click(screen.getByRole('button', { name: 'ScadBuddy tools' }))
    expect(within(card('Child server')).getByText('extra of Child server')).toBeInTheDocument()
    expect(screen.getAllByText(/^extra of /)).toHaveLength(1)
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

  describe('while a source is checking (#4757)', () => {
    afterEach(() => vi.useRealTimers())

    /** Serves the list with `package:x`'s child checking for the first `checkingReads` reads. */
    function serve(checkingReads: number) {
      let reads = 0
      server.use(
        http.get('/api/v1/ai/capabilities', () => {
          reads += 1
          const child = source({ id: 'package:x/mcp:s', kind: 'package_server', title: 's', checking: reads <= checkingReads }, 1)
          return HttpResponse.json({ sources: [source({ id: 'package:x', kind: 'package', title: 'x', children: [child] }, 1)] })
        }),
      )
      return () => reads
    }
    const tick = (ms: number) => act(() => vi.advanceTimersByTimeAsync(ms))
    const checking = (sources: SourceView[]) => sources[0]?.children[0]?.checking

    it('reads again, and stops once it settles', async () => {
      const reads = serve(2)
      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
      const { result } = renderHook(() => useCapabilities(() => true))
      await tick(0)
      expect(checking(result.current.sources)).toBe(true)
      await tick(POLL_MS)
      expect(reads()).toBe(2)
      await tick(POLL_MS)
      expect(reads()).toBe(3)
      expect(checking(result.current.sources)).toBe(false)
      await tick(POLL_MS * 5)
      expect(reads()).toBe(3)
    })

    it('gives up after POLL_MAX reads', async () => {
      const reads = serve(Infinity)
      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
      renderHook(() => useCapabilities(() => true))
      await tick(0)
      for (let i = 0; i < POLL_MAX + 3; i++) await tick(POLL_MS)
      expect(reads()).toBe(1 + POLL_MAX)
    })

    it('stops once unmounted', async () => {
      const reads = serve(Infinity)
      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
      const { unmount } = renderHook(() => useCapabilities(() => true))
      await tick(0)
      unmount()
      await tick(POLL_MS * 3)
      expect(reads()).toBe(1)
    })
  })

  it('says why the list failed', async () => {
    server.use(http.get('/api/v1/ai/capabilities', () => HttpResponse.json({ detail: 'agent down' }, { status: 503 })))
    const { result } = renderHook(() => useCapabilities(() => true))
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.error?.message).toContain('agent down')
    expect(result.current.sources).toEqual([])
  })
})
