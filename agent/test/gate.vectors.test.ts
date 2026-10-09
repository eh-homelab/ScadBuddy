import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { durableRequestId, parseRequestId } from '../src/gate/ids.js'
import { CANCEL_INPUT_UPDATE, GATE_REFUSED, INTERRUPT_SIGNAL, PENDING_INPUT_QUERY, RESPOND_UPDATE } from '../src/gate/names.js'
import { roleOf } from '../src/gate/role.js'
import { type GateEntry, RESPONSE_MAX, RespondRefusal, type Role, validateRespond } from '../src/gate/validate.js'
import { ANSWER_MAX } from '../src/harness/questions.js'

// The shared vectors of the tool-call gate's validator (spec 2026-10-01 §6.6, §8):
// the same file runs against agent-durable's gate/validate.py, so the classic and the
// durable validators cannot drift. `durable_only` vectors (an entry being resolved,
// a state a classic decision never has) are skipped here by name, never silently.

type Who = { kind: 'browser' | 'bearer'; id: string }
type Vector = {
  name: string
  durable_only?: boolean
  entry: {
    id: string
    kind: 'approval' | 'answer'
    state: GateEntry['state']
    input_hash: string | null
    requested_by: Who | null
    session_owner: Who | null
    session_creator: Who | null
    questions?: { question: string; multi_select: boolean }[]
    options?: string[]
  } | null
  request: { request_id: string; response: unknown; responder: Who; role: Role }
  expect: { ok: true; decision?: string; answers?: string[] } | { refused: string }
}
type Doc = { response_max: number; answer_max: number; prompt_max: number; names: Record<string, string>; vectors: Vector[] }

const doc = JSON.parse(readFileSync(new URL('./fixtures/pending-input-vectors.json', import.meta.url), 'utf8')) as Doc

function expand(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(expand)
  if (typeof value === 'object' && value !== null) {
    const v = value as Record<string, unknown>
    if (typeof v.$repeat === 'string') {
      const times = typeof v.times === 'number' ? v.times : (doc as unknown as Record<string, number>)[v.times as string]!
      return v.$repeat.repeat(times + 1)
    }
    return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, expand(x)]))
  }
  return value
}

function entryOf(e: NonNullable<Vector['entry']>): GateEntry {
  return {
    id: e.id,
    kind: e.kind,
    state: e.state,
    inputHash: e.input_hash,
    requestedBy: e.requested_by,
    sessionOwner: e.session_owner,
    sessionCreator: e.session_creator,
    ...(e.questions ? { questions: e.questions.map((q) => ({ question: q.question, multiSelect: q.multi_select })) } : {}),
    ...(e.options ? { options: e.options } : {}),
  }
}

describe('the pending-input vectors', () => {
  it('carry the cap, the answer bound and the handler names this service uses', () => {
    expect(doc.response_max).toBe(RESPONSE_MAX)
    expect(doc.answer_max).toBe(ANSWER_MAX)
    expect(doc.names).toEqual({
      pending_input: PENDING_INPUT_QUERY,
      respond: RESPOND_UPDATE,
      cancel_input: CANCEL_INPUT_UPDATE,
      interrupt: INTERRUPT_SIGNAL,
      refused_type: GATE_REFUSED,
    })
    expect(doc.vectors.length).toBeGreaterThan(20)
  })

  for (const vector of doc.vectors) {
    const run = vector.durable_only ? it.skip : it
    run(vector.name, () => {
      const request = { ...vector.request, response: expand(vector.request.response) }
      const call = () =>
        validateRespond(vector.entry ? entryOf(vector.entry) : undefined, {
          requestId: request.request_id,
          response: request.response,
          responder: request.responder,
          role: request.role,
        })
      if ('refused' in vector.expect) {
        let thrown: unknown
        try {
          call()
        } catch (err) {
          thrown = err
        }
        expect(thrown).toBeInstanceOf(RespondRefusal)
        expect((thrown as RespondRefusal).code).toBe(vector.expect.refused)
      } else {
        const valid = call()
        if (vector.expect.decision) expect(valid).toEqual({ decision: vector.expect.decision })
        if (vector.expect.answers) expect(valid).toEqual({ answers: vector.expect.answers })
      }
    })
  }
})

describe('request ids', () => {
  const session = '00000000-0000-4000-8000-0000000000AB'
  it('round-trip a durable id, keeping a tool_use_id with colons whole', () => {
    const id = durableRequestId(session, 'run-1', 'toolu:a:b')
    expect(id).toBe('durable:00000000-0000-4000-8000-0000000000ab:run-1:toolu:a:b')
    expect(parseRequestId(id)).toEqual({ store: 'durable', sessionId: session.toLowerCase(), runId: 'run-1', toolUseId: 'toolu:a:b' })
  })
  it('read the classic and flow prefixes', () => {
    expect(parseRequestId(`approval:${session}`)).toEqual({ store: 'approval', rowId: session })
    expect(parseRequestId(`question:${session}`)).toEqual({ store: 'question', rowId: session })
    expect(parseRequestId('flow:r:w:c')).toEqual({ store: 'flow', runId: 'r', workflowRunId: 'w', callId: 'c' })
  })
  it('refuse anything malformed', () => {
    for (const bad of [
      '',
      session,
      `other:${session}`,
      'approval:not-a-uuid',
      'durable:not-a-uuid:run:toolu',
      `durable:${session}:run`,
      `durable:${session}::toolu`,
      `durable:${session}:run:`,
      'flow:r:w',
    ]) {
      expect(parseRequestId(bad), bad).toBeUndefined()
    }
  })
})

describe("the route's role", () => {
  const browser = { kind: 'browser' as const, id: 'browser' }
  const a = { kind: 'bearer' as const, id: 'a' }
  const c = { kind: 'bearer' as const, id: 'c' }
  it('is browser for the browser user, whoever owns the session', () => {
    expect(roleOf(browser, { owner: a, creator: a }, false)).toBe('browser')
  })
  it('is owner for a non-browser owner or starter, even holding the grant', () => {
    expect(roleOf(a, { owner: a, creator: c }, true)).toBe('owner')
    expect(roleOf(a, { owner: c, creator: a }, true)).toBe('owner')
  })
  it('is grant for another principal with the grant, and nothing without it', () => {
    expect(roleOf(c, { owner: a, creator: a }, true)).toBe('grant')
    expect(roleOf(c, null, true)).toBe('grant')
    expect(roleOf(c, { owner: a, creator: a }, false)).toBeUndefined()
  })
})
