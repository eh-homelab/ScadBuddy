import { describe, expect, it } from 'vitest'
import { attentionMessage, noticeTitle, pendingNotices } from '../src/notify/dispatcher.js'
import type { PendingInputPage } from '../src/routes/pendingInput.js'

// What the dispatcher announces (#815): every request that waits on the person, approvals and
// plain questions as well as attention requests, never a done summary.

const entry = (fields: Record<string, unknown>) => ({ session_id: 'sess-1', prompt: '', summary: '', ...fields })

describe('pendingNotices', () => {
  it('lists approvals, plain questions and attention requests, and nothing that waits on no one', () => {
    const page = {
      summaries_truncated: false,
      entries: [
        entry({ id: 'approval:a1', kind: 'approval', attention: null }),
        entry({ id: 'question:q1', kind: 'answer', attention: null, prompt: 'Which colour?' }),
        entry({ id: 'question:q2', kind: 'answer', attention: { reason: 'blocked', on_timeout: 'proceed' } }),
        entry({ id: 'question:q3', kind: 'answer', attention: { reason: 'done', on_timeout: null } }),
        entry({ id: 'approval:a2', kind: 'approval', attention: null, expiring: true }),
        entry({ id: 'approval:a3', kind: 'approval', attention: null, run: { id: 'r', hop: 'parent', reason: null } }),
      ],
    } as unknown as PendingInputPage
    expect(pendingNotices(page)).toEqual([
      { id: 'approval:a1', sessionId: 'sess-1', reason: 'approval' },
      { id: 'question:q1', sessionId: 'sess-1', reason: 'question' },
      { id: 'question:q2', sessionId: 'sess-1', reason: 'blocked' },
    ])
  })

  it('titles an approval as one, and sends a channel no more than its reason and a link', () => {
    expect(noticeTitle('approval')).toBe('The assistant needs your approval')
    expect(attentionMessage({ id: 'approval:a1', sessionId: 's', reason: 'approval' }, 'https://sb.example')).toEqual({
      title: 'The assistant needs your approval',
      body: 'Open ScadBuddy to decide it.',
      click: 'https://sb.example/?assistant-session=s',
    })
  })
})
