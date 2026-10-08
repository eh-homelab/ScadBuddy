import { describe, expect, it } from 'vitest'
import { fetchPendingInput } from '../../agent/attention'
import { respond, RespondError } from '../../agent/respond'
import { setPendingAnswers, setPendingApprovals } from './pendingInput'

// #1479: an entry a test lists with setPendingApprovals / setPendingAnswers can be
// answered through the mocked respond route, as the agent would answer it.

async function listedIds(): Promise<string[]> {
  const res = await fetch('/api/v1/ai/pending-input')
  return ((await res.json()) as { entries: { id: string }[] }).entries.map((e) => e.id)
}

describe('the mocked respond route', () => {
  it('answers a listed entry, drops it from the list, and refuses it again with a 409', async () => {
    setPendingApprovals(2)
    setPendingAnswers(1, 1)
    const [first, second, question, attention] = await listedIds()

    expect(await respond(first!, { kind: 'approval', decision: 'approve' })).toBe('approved')
    expect(await respond(question!, { kind: 'answer', answers: { 'Which colour?': 'Red' } })).toBe('answered')
    expect(await respond(attention!, { kind: 'answer', choice: "I'm here" })).toBe('answered')
    expect(await listedIds()).toEqual([second])
    expect(await fetchPendingInput()).toEqual({ approvals: 1, questions: 0, attention: 0, summaries: 0 })

    const again = await respond(first!, { kind: 'approval', decision: 'deny' }).catch((e: unknown) => e)
    expect(again).toBeInstanceOf(RespondError)
    expect(again).toMatchObject({ status: 409, reason: 'it was already approved' })
  })

  it('refuses a body that is not the listed entry\'s kind with a 400', async () => {
    setPendingApprovals(1)
    const [id] = await listedIds()
    const wrong = await respond(id!, { kind: 'answer', text: 'yes' }).catch((e: unknown) => e)
    expect(wrong).toMatchObject({ status: 400 })
    expect(await listedIds()).toEqual([id])
  })
})
