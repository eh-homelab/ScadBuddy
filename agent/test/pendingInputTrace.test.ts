import { SpanStatusCode } from '@opentelemetry/api'
import { beforeEach, describe, expect, it } from 'vitest'
import { durableRequestId } from '../src/gate/ids.js'
import { RespondRefusal } from '../src/gate/validate.js'
import { decide, RISK_TIERS, type GuardVerdict } from '../src/harness/permissions.js'
import { respond } from '../src/routes/pendingInput.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { browser } from './support/sessions.js'
import { resetTracing, testTracing } from './support/tracing.js'

// The `agent.approval` span of an AgentSession decision (#5041, #5045), without Postgres or
// Temporal: a stand-in SessionManager whose workflow refuses the Update as one that already
// took this decision does (#3717).

const spans = testTracing()
const SESSION = '0f8fad5b-d9cb-469f-a165-70867728950e'
const REQUEST = durableRequestId(SESSION, 'run-2', 'toolu_1')

function sessions(recorded: { decision: string } | undefined): SessionManager {
  return {
    approvals: { holdsGrant: () => Promise.resolve(false) },
    projection: {
      session: () =>
        Promise.resolve({ mode: 'durable', owner: { kind: 'browser', id: 'browser', label: 'You' }, creator: { kind: 'browser', id: 'browser' } }),
      inRun: () => Promise.resolve(false),
      parkTrace: () => Promise.resolve({ tool: 'mcp__scadbuddy__print_output', createdAt: new Date(), traceparent: null }),
      markDecision: () => Promise.resolve(),
      recorded: () =>
        Promise.resolve(
          recorded && { kind: 'approval', outcome: 'approved', responder: browser, response: recorded, responseHash: null },
        ),
    },
    durable: { respond: () => Promise.reject(new RespondRefusal('resolved', 'already decided')) },
  } as unknown as SessionManager
}

describe('an AgentSession decision span', () => {
  beforeEach(async () => {
    await resetTracing()
  })

  it('is no error when the decision is a retry of the one recorded (#5045)', async () => {
    expect(await respond(sessions({ decision: 'approve' }), browser, REQUEST, { kind: 'approval', decision: 'approve' })).toMatchObject({
      outcome: 'approved',
    })
    const [span] = spans.getFinishedSpans().filter((s) => s.name === 'agent.approval')
    expect(span!.status.code).not.toBe(SpanStatusCode.ERROR)
    expect(span!.attributes['scadbuddy.outcome']).toBe('approved')
  })

  it('is an error when the refusal stands', async () => {
    await expect(respond(sessions({ decision: 'deny' }), browser, REQUEST, { kind: 'approval', decision: 'approve' })).rejects.toMatchObject({
      status: 409,
    })
    const [span] = spans.getFinishedSpans().filter((s) => s.name === 'agent.approval')
    expect(span!.status.code).toBe(SpanStatusCode.ERROR)
  })

  it("names the outward tier, which is every parked call's by construction (#5045)", async () => {
    // Only a call that needs approval parks, and that decision is always outward.
    const verdicts: (GuardVerdict | undefined)[] = [undefined, { outward: 'o' }, { allow: 'a' }, { input: {} }, { deny: 'd' }]
    for (const own of RISK_TIERS) {
      for (const verdict of verdicts) {
        const decision = decide('t', () => own, {}, verdict)
        if (decision.decision === 'needs_approval') expect(decision.tier, `${own} ${JSON.stringify(verdict)}`).toBe('outward')
      }
    }
    await respond(sessions({ decision: 'approve' }), browser, REQUEST, { kind: 'approval', decision: 'approve' })
    const [span] = spans.getFinishedSpans().filter((s) => s.name === 'agent.approval')
    expect(span!.attributes['scadbuddy.tier']).toBe('outward')
  })
})
