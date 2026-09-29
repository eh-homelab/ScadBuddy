import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { FeedItem } from '../../agent/chat/state'
import { FeedItemView } from './FeedItemView'

const you = { kind: 'browser' as const, id: 'browser', label: 'You' }

function card(state: Extract<FeedItem, { kind: 'approval' }>['state']) {
  const item: FeedItem = { kind: 'approval', id: 'a1', tool: 't1', summary: 'Send it?', state, by: you }
  return render(<FeedItemView item={item} onDecide={vi.fn()} />)
}

describe('the approval card', () => {
  it('offers the buttons only while pending, and says where the answer is otherwise', () => {
    const cases: [Parameters<typeof card>[0], string | null][] = [
      ['pending', null],
      ['sent', 'Sending your answer…'],
      ['queued', 'Not connected: your answer goes first when the assistant reconnects.'],
      ['approved', 'Approved by You.'],
      ['denied', 'Denied by You.'],
    ]
    for (const [state, text] of cases) {
      const { unmount } = card(state)
      if (text === null) {
        expect(screen.getByRole('button', { name: 'Approve' })).toBeEnabled()
        expect(screen.getByRole('button', { name: 'Deny' })).toBeEnabled()
      } else {
        expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument()
        expect(screen.getByRole('status')).toHaveTextContent(text)
      }
      unmount()
    }
  })
})
