import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { QueuedEntry } from '../../agent/chat/protocol'
import { QueuedMessages } from './QueuedMessages'

const you = { kind: 'browser', id: 'browser', label: 'You' } as const
const entries: QueuedEntry[] = [
  { id: 'q1', text: 'Also make it blue', author: you, queuedAt: '2026-11-15T00:00:00.000Z' },
  { id: 'q2', text: 'And a hole for the ring', author: { kind: 'bearer', id: 'tok', label: 'Claude Desktop' }, queuedAt: '2026-11-15T00:00:01.000Z' },
]

describe('QueuedMessages (#6386)', () => {
  it('lists each queued message in order, the first marked as the next to run', () => {
    render(<QueuedMessages entries={entries} sessionId="s1" canChange errors={0} onRemove={() => true} onEdit={() => {}} />)
    const items = within(screen.getByRole('list', { name: 'Queued messages' })).getAllByRole('listitem')
    expect(items.map((i) => i.getAttribute('aria-label'))).toEqual(['Queued, runs next: Also make it blue', 'Queued: And a hole for the ring'])
    // Another sender is named, as the transcript names them.
    expect(items[1]).toHaveTextContent('Claude Desktop, Queued')
  })

  it('shows nothing for an empty queue', () => {
    const { container } = render(<QueuedMessages entries={[]} sessionId="s1" canChange errors={0} onRemove={() => true} onEdit={() => {}} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('removes one, and waits for the queue to drop it', async () => {
    const onRemove = vi.fn(() => true)
    render(<QueuedMessages entries={entries} sessionId="s1" canChange errors={0} onRemove={onRemove} onEdit={() => {}} />)
    const first = screen.getAllByRole('listitem')[0]!
    await userEvent.click(within(first).getByRole('button', { name: 'Remove queued message' }))
    expect(onRemove).toHaveBeenCalledWith('q1')
    expect(within(first).getByRole('button', { name: 'Remove queued message' })).toBeDisabled()
  })

  it('keeps a refused remove usable: not sent, or refused by the agent with an error', async () => {
    const button = () => within(screen.getAllByRole('listitem')[0]!).getByRole('button', { name: 'Remove queued message' })
    // Not sent (the agent is unreachable and too much waits): the buttons stay.
    const { rerender } = render(<QueuedMessages entries={entries} sessionId="s1" canChange errors={0} onRemove={() => false} onEdit={() => {}} />)
    await userEvent.click(button())
    expect(button()).toBeEnabled()
    // Sent, then the agent answers with an error and the entry stays: the buttons work again.
    rerender(<QueuedMessages entries={entries} sessionId="s1" canChange errors={0} onRemove={() => true} onEdit={() => {}} />)
    await userEvent.click(button())
    expect(button()).toBeDisabled()
    rerender(<QueuedMessages entries={entries} sessionId="s1" canChange errors={1} onRemove={() => true} onEdit={() => {}} />)
    expect(button()).toBeEnabled()
    expect(within(screen.getAllByRole('listitem')[0]!).getByRole('button', { name: 'Edit queued message' })).toBeEnabled()
  })

  it('edits one: Save sends the new words, Escape sends nothing', async () => {
    const onEdit = vi.fn()
    render(<QueuedMessages entries={entries} sessionId="s1" canChange errors={0} onRemove={() => true} onEdit={onEdit} />)
    const second = () => screen.getAllByRole('listitem')[1]!
    await userEvent.click(within(second()).getByRole('button', { name: 'Edit queued message' }))
    expect(within(second()).getByRole('textbox', { name: 'Queued message' })).toHaveFocus()
    await userEvent.keyboard('{Escape}')
    expect(onEdit).not.toHaveBeenCalled()
    expect(within(second()).queryByRole('textbox')).toBeNull()
    await userEvent.click(within(second()).getByRole('button', { name: 'Edit queued message' }))
    await userEvent.clear(within(second()).getByRole('textbox', { name: 'Queued message' }))
    expect(within(second()).getByRole('button', { name: 'Save' })).toBeDisabled()
    await userEvent.type(within(second()).getByRole('textbox', { name: 'Queued message' }), 'And a 4 mm hole{Enter}')
    expect(onEdit).toHaveBeenCalledWith('q2', 'And a 4 mm hole')
  })

  it('offers no changes to a viewer who may not send to the session', () => {
    render(<QueuedMessages entries={entries} sessionId="s1" canChange={false} errors={0} onRemove={() => true} onEdit={() => {}} />)
    expect(screen.queryByRole('button')).toBeNull()
  })
})
