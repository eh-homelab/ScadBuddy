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

describe('the memory line', () => {
  const memory = (item: Partial<Extract<FeedItem, { kind: 'memory' }>>) =>
    render(
      <FeedItemView
        item={{ kind: 'memory', id: 'memory-0', action: 'recall', bank: 'scadbuddy', outcome: 'ok', ...item }}
        onDecide={vi.fn()}
      />,
    )

  it('says what memory did, and expands to the bank and count only', () => {
    const cases: [Parameters<typeof memory>[0], string][] = [
      [{ count: 3 }, 'Recalled 3 memories'],
      [{ count: 1 }, 'Recalled 1 memory'],
      [{ count: 0 }, 'No memories recalled'],
      [{ outcome: 'timeout', detail: 'timed out after 3000 ms' }, 'Memory recall timed out'],
      [{ outcome: 'error', detail: 'HTTP 500' }, 'Memory recall failed'],
      [{ action: 'retain' }, 'Saved to memory'],
      [{ action: 'retain', outcome: 'timeout' }, 'Saving to memory timed out'],
      [{ action: 'retain', outcome: 'error', detail: 'HTTP 503' }, 'Could not save to memory'],
    ]
    for (const [item, headline] of cases) {
      const { unmount } = memory(item)
      const line = screen.getByTestId('agent-memory')
      expect(line.querySelector('summary')).toHaveTextContent(headline)
      expect(line).toHaveTextContent('Bank scadbuddy')
      if (item.detail) expect(line).toHaveTextContent(item.detail)
      unmount()
    }
    memory({ count: 3 })
    expect(screen.getByTestId('agent-memory')).toHaveTextContent('Bank scadbuddy · 3 found')
  })
})
