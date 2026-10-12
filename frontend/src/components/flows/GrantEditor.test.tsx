import { screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { FlowGrantSpec } from '../../api/types'
import { renderPage } from '../../test/utils'
import { grantProblems } from '../../lib/grants'
import { GrantEditor } from './GrantEditor'

// #2481 (spec 2026-10-11 §3.1) — the grant a person attaches to a run: one rule per host
// function and outward tool, printers and models from the existing reads, maxima, an
// expiry (24 h by default, at most 7 days), and the two session switches.

const HOUR = 3_600_000
const OUTWARD = new Set(['send_to_bambuddy'])

function render(initial?: FlowGrantSpec) {
  const onSubmit = vi.fn<(spec: FlowGrantSpec) => void>()
  const view = renderPage(<GrantEditor initial={initial} submitLabel="Use grant" onSubmit={onSubmit} />)
  return { ...view, onSubmit }
}

const rule = (name: string) => screen.findByRole('group', { name: `Rule ${name}` })

describe('GrantEditor', () => {
  it('builds a grant the backend accepts: only the bounds set, 24 h ahead by default', async () => {
    const { user, onSubmit } = render()
    const print = await rule('queue_print')
    await user.click(within(print).getByRole('checkbox', { name: 'Allow without asking' }))
    await user.click(within(print).getByRole('checkbox', { name: 'Any printer' }))
    await user.click(await within(print).findByRole('checkbox', { name: '3DP-31B-598' }))
    await user.type(within(print).getByLabelText('Max prints'), '5')
    await user.click(screen.getByRole('button', { name: 'Use grant' }))

    expect(onSubmit).toHaveBeenCalledTimes(1)
    const spec = onSubmit.mock.calls[0]![0]
    expect(spec.sessions).toBe(false)
    expect(spec.ask_starter_session).toBe(true)
    expect(spec.rules).toEqual([{ fn: 'queue_print', printers: [1], max_prints: 5 }])
    const ahead = new Date(spec.expires_at).getTime() - Date.now()
    expect(ahead).toBeGreaterThan(23 * HOUR)
    expect(ahead).toBeLessThanOrEqual(24 * HOUR)
    expect(spec.expires_at).toMatch(/(Z|[+-]\d\d:\d\d)$/)
    expect(grantProblems(spec, new Date(), OUTWARD)).toEqual([])
  })

  it('warns in red about a rule with no bounds, until one is set', async () => {
    const { user } = render()
    const arrange = await rule('arrange')
    await user.click(within(arrange).getByRole('checkbox', { name: 'Allow without asking' }))
    expect(within(arrange).getByText(/any model, any number of calls/)).toHaveClass('text-warn')
    await user.type(within(arrange).getByLabelText('Max calls'), '3')
    expect(within(arrange).queryByText(/any model, any number of calls/)).not.toBeInTheDocument()
  })

  it('lists outward tools only, with argument bounds', async () => {
    const { user, onSubmit } = render()
    expect(await rule('send_to_bambuddy')).toBeInTheDocument()
    expect(screen.queryByRole('group', { name: 'Rule list_models' })).not.toBeInTheDocument()
    const send = await rule('send_to_bambuddy')
    await user.click(within(send).getByRole('checkbox', { name: 'Allow without asking' }))
    await user.click(within(send).getByRole('button', { name: 'Add argument bound' }))
    await user.type(within(send).getByLabelText('Argument path'), '/printer_id')
    await user.type(within(send).getByLabelText('Allowed values'), '3, 4')
    await user.click(screen.getByRole('checkbox', { name: 'Extends to the sessions it starts' }))
    await user.click(screen.getByRole('checkbox', { name: 'Ask the starting session first' }))
    await user.click(screen.getByRole('button', { name: 'Use grant' }))
    const spec = onSubmit.mock.calls[0]![0]
    expect(spec.rules).toEqual([{ fn: 'tool', tool: 'send_to_bambuddy', args: [{ path: '/printer_id', in: [3, 4] }] }])
    expect(spec.sessions).toBe(true)
    expect(spec.ask_starter_session).toBe(false)
    expect(grantProblems(spec, new Date(), OUTWARD)).toEqual([])
  })

  it('refuses an expiry more than 7 days away', async () => {
    const { user, onSubmit } = render()
    const expires = await screen.findByLabelText('Expires')
    await user.clear(expires)
    await user.type(expires, '2099-01-01T00:00')
    expect(screen.getByText('The grant expires more than 7 days from now.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Use grant' })).toBeDisabled()
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('pre-fills a requested grant and keeps a bound that admits nothing', async () => {
    const initial: FlowGrantSpec = {
      expires_at: new Date(Date.now() + 2 * HOUR).toISOString(),
      sessions: true,
      ask_starter_session: false,
      rules: [{ fn: 'queue_print', printers: [2], library_files: [], max_prints: 12 }],
    }
    const { user, onSubmit } = render(initial)
    const print = await rule('queue_print')
    expect(within(print).getByRole('checkbox', { name: 'Allow without asking' })).toBeChecked()
    expect(await within(print).findByRole('checkbox', { name: '3DP-77A-114' })).toBeChecked()
    await user.clear(within(print).getByLabelText('Max prints'))
    await user.type(within(print).getByLabelText('Max prints'), '4')
    await user.click(screen.getByRole('button', { name: 'Use grant' }))
    const spec = onSubmit.mock.calls[0]![0]
    expect(spec.rules).toEqual([{ fn: 'queue_print', printers: [2], library_files: [], max_prints: 4 }])
    expect(spec.sessions).toBe(true)
    expect(spec.ask_starter_session).toBe(false)
    expect(Math.abs(new Date(spec.expires_at).getTime() - new Date(initial.expires_at).getTime())).toBeLessThan(60_000)
  })
})

describe('grantProblems', () => {
  const now = new Date('2026-10-12T12:00:00Z')
  const base: FlowGrantSpec = { expires_at: '2026-10-13T12:00:00Z', sessions: false, ask_starter_session: true, rules: [] }

  it('mirrors the backend: bounds per function, outward tools, pointers and expiry', () => {
    expect(grantProblems(base, now, OUTWARD)).toEqual([])
    expect(
      grantProblems(
        {
          ...base,
          expires_at: '2026-10-20T12:00:00Z',
          rules: [
            { fn: 'arrange', printers: [1] },
            { fn: 'tool', tool: 'list_models' },
            { fn: 'tool', tool: 'send_to_bambuddy', args: [{ path: 'printer_id', in: [1] }] },
            { fn: 'queue_print', tool: 'send_to_bambuddy', max_calls: -1 },
          ],
        },
        now,
        OUTWARD,
      ),
    ).toEqual([
      'Rule 1: printers does not bound arrange.',
      'Rule 2: list_models is not an outward tool; a grant names only outward tools.',
      "Rule 3: 'printer_id' is not a JSON pointer.",
      'Rule 4: only a tool rule names a tool.',
      'Rule 4: max_calls is negative.',
      'The grant expires more than 7 days from now.',
    ])
    expect(grantProblems({ ...base, expires_at: '2026-10-12T11:00:00Z' }, now, OUTWARD)).toEqual([
      'The grant expires in the past.',
    ])
  })
})
