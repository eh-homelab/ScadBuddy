import { HttpResponse, http } from 'msw'
import { describe, expect, it } from 'vitest'
import { server } from '../mocks/server'
import type { Question } from './chat/protocol'
import { answerBody, decisionBody, RespondError, respond } from './respond'

const question = (text: string, labels: string[]): Question => ({
  question: text,
  header: text,
  multiSelect: false,
  options: labels.map((label) => ({ label, description: '' })),
})

describe('answerBody', () => {
  const attention = [question('The tab closed; reopen it?', ["I'm here", 'Cancel'])]

  it("sends an attention request's picked option as its choice", () => {
    expect(answerBody(attention, ["I'm here"], true)).toEqual({ kind: 'answer', choice: "I'm here" })
  })

  it("sends an attention request's own words as text, even when they only resemble an option", () => {
    expect(answerBody(attention, ['reopened it'], true)).toEqual({ kind: 'answer', text: 'reopened it' })
    expect(answerBody(attention, ["i'm here"], true)).toEqual({ kind: 'answer', text: "i'm here" })
  })

  it("keys a question card's answers by question text, in the questions' order", () => {
    const qs = [question('Which colour?', ['Red', 'Blue']), question('Which size?', ['S', 'M'])]
    const body = answerBody(qs, ['Blue', 'my own size'], false)
    expect(body).toEqual({ kind: 'answer', answers: { 'Which colour?': 'Blue', 'Which size?': 'my own size' } })
    expect(body.kind === 'answer' && 'answers' in body && Object.keys(body.answers)).toEqual(['Which colour?', 'Which size?'])
  })

  it('sends an approval decision', () => {
    expect(decisionBody(true)).toEqual({ kind: 'approval', decision: 'approve' })
    expect(decisionBody(false)).toEqual({ kind: 'approval', decision: 'deny' })
  })
})

describe('RespondError.settled', () => {
  it('is true only for an entry already resolved (409) or expired (410)', () => {
    expect(new RespondError('x', 409).settled).toBe(true)
    expect(new RespondError('x', 410).settled).toBe(true)
    expect(new RespondError('x', 404).settled).toBe(false)
    expect(new RespondError('x', 400).settled).toBe(false)
    expect(new RespondError('x').settled).toBe(false)
  })
})

describe('respond', () => {
  const id = 'question:abc'
  const route = '/api/v1/ai/pending-input/:id'

  it("returns the agent's outcome, and carries a refusal's detail and status", async () => {
    server.use(http.post(route, () => HttpResponse.json({ outcome: 'answered' })))
    expect(await respond(id, { kind: 'answer', text: 'hi' })).toBe('answered')

    server.use(http.post(route, () => HttpResponse.json({ detail: 'no longer waiting' }, { status: 409 })))
    const err = await respond(id, { kind: 'answer', text: 'hi' }).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(RespondError)
    expect(err).toMatchObject({ message: 'no longer waiting', status: 409, settled: true })
  })

  it('is not settled when the agent cannot be reached', async () => {
    server.use(http.post(route, () => HttpResponse.error()))
    const err = await respond(id, { kind: 'approval', decision: 'deny' }).catch((e: unknown) => e)
    expect(err).toMatchObject({ status: undefined, settled: false })
  })
})
