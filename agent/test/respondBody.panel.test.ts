import { describe, expect, it } from 'vitest'
import { RespondBody } from '../src/routes/pendingInput.js'
import { frontendRespondBodies } from './support/frontendProtocol.js'

// #1386: the panel's RespondBody (frontend/src/agent/respond.ts) is a hand copy of the
// route's schema. Every body the panel builds must parse with the route's own.

const question = (text: string) => ({
  question: text,
  header: 'Pick',
  multiSelect: false,
  options: [
    { label: 'Approve', description: 'File it' },
    { label: 'Cancel', description: 'Do not' },
  ],
})

describe("the panel's respond bodies", () => {
  it("parse with the route's RespondBody, every shape", async () => {
    const { decisionBody, answerBody } = await frontendRespondBodies()
    const bodies = {
      approve: decisionBody(true),
      deny: decisionBody(false),
      answers: answerBody([question('Which draft?'), question('Labels?')], ['Approve', 'my own words'], false),
      choice: answerBody([question('Go on?')], ['Approve'], true),
      text: answerBody([question('Go on?')], ['wait for me'], true),
    }
    // Each body is the shape its name says, so a change to the panel's builders shows up here.
    expect(bodies).toEqual({
      approve: { kind: 'approval', decision: 'approve' },
      deny: { kind: 'approval', decision: 'deny' },
      answers: { kind: 'answer', answers: { 'Which draft?': 'Approve', 'Labels?': 'my own words' } },
      choice: { kind: 'answer', choice: 'Approve' },
      text: { kind: 'answer', text: 'wait for me' },
    })
    for (const [name, body] of Object.entries(bodies)) {
      const parsed = RespondBody.safeParse(body)
      expect(parsed.success, `${name}: ${parsed.error?.message ?? ''}`).toBe(true)
    }
  })
})
