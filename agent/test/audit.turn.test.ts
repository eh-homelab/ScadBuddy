import { describe, expect, it } from 'vitest'
import type { AuditEntry, AuditLog } from '../src/audit/log.js'
import { TurnAuditor } from '../src/audit/turn.js'
import type { ApprovalGate } from '../src/harness/permissions.js'
import { event } from '../src/sessions/protocol.js'

// TurnAuditor's ordering (#258), without a database: a row for a call that
// was parked on an approval must carry the gate's verdict even when the turn
// ends (an interrupt) before the gate has finished recording it.

function stubLog(): { log: AuditLog; entries: AuditEntry[] } {
  const entries: AuditEntry[] = []
  const log = {
    record: async (entry: AuditEntry) => {
      entries.push(entry)
    },
    hash: () => 'h',
    summarise: () => '{}',
  } as unknown as AuditLog
  return { log, entries }
}

const call = (id: string) =>
  event({ type: 'tool.call', sessionId: 's', id, name: 'mcp__scadbuddy__print_output', input: {}, risk: 'outward' })

describe('TurnAuditor', () => {
  it('an interrupted call is refused with its approval id, even when the turn ends while the gate is still recording', async () => {
    const { log, entries } = stubLog()
    const turn = new TurnAuditor(log, {
      sessionId: '11111111-1111-4111-8111-111111111111',
      turnId: '22222222-2222-4222-8222-222222222222',
      actor: { kind: 'browser', id: 'browser', label: 'You' },
      tierOf: () => 'outward',
      secrets: () => [],
    })
    const abort = new AbortController()
    // The approval gate parks until the abort, then rejects (as ApprovalService.gate's waitFor does).
    const parked: ApprovalGate = () =>
      new Promise((_, reject) => abort.signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true }))
    // The lookup of the approval the gate had recorded is slow (a database round trip under load).
    let lookedUp!: () => void
    const lookup = new Promise<void>((resolve) => (lookedUp = resolve))
    const gate = turn.gate(parked, async () => {
      await lookup
      return '33333333-3333-4333-8333-333333333333'
    })

    await turn.observe(call('t1'))
    const decided = gate({ toolName: 'mcp__scadbuddy__print_output', input: {}, toolUseId: 't1', tier: 'outward', signal: abort.signal })
    abort.abort()
    // The turn ends now, while the gate's catch is still waiting on the lookup.
    const finished = turn.finish('the turn was interrupted')
    await new Promise((r) => setTimeout(r, 20))
    expect(entries).toEqual([]) // the row waits for the verdict
    lookedUp()
    await expect(decided).rejects.toThrow('aborted')
    await finished
    expect(entries).toEqual([
      expect.objectContaining({
        toolUseId: 't1',
        outcome: 'refused',
        approvalId: '33333333-3333-4333-8333-333333333333',
        detail: 'no result: the turn was interrupted',
      }),
    ])
  })

  it('writes the rows of every call still on the gate when the turn ends at once: one settle wait, not one per call', async () => {
    const { log, entries } = stubLog()
    const turn = new TurnAuditor(
      log,
      {
        sessionId: 's',
        turnId: 't',
        actor: { kind: 'browser', id: 'browser', label: 'You' },
        tierOf: () => 'outward',
        secrets: () => [],
      },
      { settleMs: 200 },
    )
    // A gate that never records a verdict, so each row waits the full settle time.
    const gate = turn.gate(() => new Promise(() => {}))
    const ids = ['t1', 't2', 't3', 't4']
    for (const id of ids) {
      await turn.observe(call(id))
      void gate({ toolName: 'mcp__scadbuddy__print_output', input: {}, toolUseId: id, tier: 'outward', signal: new AbortController().signal })
    }
    const started = performance.now()
    await turn.finish('the turn was interrupted')
    // One after another would be 4 × 200 ms.
    expect(performance.now() - started).toBeLessThan(500)
    expect(entries.map((e) => e.toolUseId).sort()).toEqual(ids)
    expect(entries.every((e) => e.outcome === 'error' && e.detail === 'no result: the turn was interrupted')).toBe(true)
  })

  it('a call that never met the gate (read or write tier) is written at once', async () => {
    const { log, entries } = stubLog()
    const turn = new TurnAuditor(log, {
      sessionId: 's',
      turnId: 't',
      actor: { kind: 'browser', id: 'browser', label: 'You' },
      tierOf: () => 'read',
      secrets: () => [],
    })
    await turn.observe(call('r1'))
    await turn.observe(event({ type: 'tool.result', sessionId: 's', id: 'r1', ok: true, summary: 'x' }))
    expect(entries).toEqual([expect.objectContaining({ toolUseId: 'r1', outcome: 'ok' })])
  })
})
