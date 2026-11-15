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
    render(<QueuedMessages entries={entries} sessionId="s1" canChange refusals={{}} onRemove={() => true} onEdit={() => true} />)
    const items = within(screen.getByRole('list', { name: 'Queued messages' })).getAllByRole('listitem')
    expect(items.map((i) => i.getAttribute('aria-label'))).toEqual(['Queued, runs next: Also make it blue', 'Queued: And a hole for the ring'])
    // Another sender is named, as the transcript names them.
    expect(items[1]).toHaveTextContent('Claude Desktop, Queued')
  })

  it('shows nothing for an empty queue', () => {
    const { container } = render(<QueuedMessages entries={[]} sessionId="s1" canChange refusals={{}} onRemove={() => true} onEdit={() => true} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('removes one, and waits for the queue to drop it', async () => {
    const onRemove = vi.fn(() => true)
    render(<QueuedMessages entries={entries} sessionId="s1" canChange refusals={{}} onRemove={onRemove} onEdit={() => true} />)
    const first = screen.getAllByRole('listitem')[0]!
    await userEvent.click(within(first).getByRole('button', { name: 'Remove queued message' }))
    expect(onRemove).toHaveBeenCalledWith('q1')
    expect(within(first).getByRole('button', { name: 'Remove queued message' })).toBeDisabled()
  })

  it('keeps a refused remove usable: not sent, or refused by the agent with an error', async () => {
    const button = () => within(screen.getAllByRole('listitem')[0]!).getByRole('button', { name: 'Remove queued message' })
    // Not sent (the agent is unreachable and too much waits): the buttons stay.
    const { rerender } = render(<QueuedMessages entries={entries} sessionId="s1" canChange refusals={{}} onRemove={() => false} onEdit={() => true} />)
    await userEvent.click(button())
    expect(button()).toBeEnabled()
    // Sent, then the agent answers with an error and the entry stays: the buttons work again.
    rerender(<QueuedMessages entries={entries} sessionId="s1" canChange refusals={{}} onRemove={() => true} onEdit={() => true} />)
    await userEvent.click(button())
    expect(button()).toBeDisabled()
    // Another entry's refusal leaves it waiting; its own brings the buttons back.
    rerender(<QueuedMessages entries={entries} sessionId="s1" canChange refusals={{ q2: 1 }} onRemove={() => true} onEdit={() => true} />)
    expect(button()).toBeDisabled()
    rerender(<QueuedMessages entries={entries} sessionId="s1" canChange refusals={{ q1: 1 }} onRemove={() => true} onEdit={() => true} />)
    expect(button()).toBeEnabled()
    expect(within(screen.getAllByRole('listitem')[0]!).getByRole('button', { name: 'Edit queued message' })).toBeEnabled()
  })

  it('edits one: Save sends the new words, Escape sends nothing', async () => {
    const onEdit = vi.fn(() => true)
    render(<QueuedMessages entries={entries} sessionId="s1" canChange refusals={{}} onRemove={() => true} onEdit={onEdit} />)
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

  it('shows sent words as saving until the queue carries them, and the old ones again on a refusal', async () => {
    const item = () => screen.getAllByRole('listitem')[0]!
    const { rerender } = render(<QueuedMessages entries={entries} sessionId="s1" canChange refusals={{}} onRemove={() => true} onEdit={() => true} />)
    await userEvent.click(within(item()).getByRole('button', { name: 'Edit queued message' }))
    await userEvent.clear(within(item()).getByRole('textbox', { name: 'Queued message' }))
    await userEvent.type(within(item()).getByRole('textbox', { name: 'Queued message' }), 'Make it red{Enter}')
    expect(item()).toHaveTextContent('Make it red')
    expect(item()).toHaveTextContent('Saving…')
    expect(within(item()).getByRole('button', { name: 'Edit queued message' })).toBeDisabled()
    expect(within(item()).getByRole('button', { name: 'Remove queued message' })).toBeDisabled()
    // The agent refused it: the old words, and the buttons, are back.
    rerender(<QueuedMessages entries={entries} sessionId="s1" canChange refusals={{ q1: 1 }} onRemove={() => true} onEdit={() => true} />)
    expect(item()).toHaveTextContent('Also make it blue')
    expect(item()).not.toHaveTextContent('Saving…')
    // Sent again and carried by the next session.queue: plain words.
    await userEvent.click(within(item()).getByRole('button', { name: 'Edit queued message' }))
    await userEvent.clear(within(item()).getByRole('textbox', { name: 'Queued message' }))
    await userEvent.type(within(item()).getByRole('textbox', { name: 'Queued message' }), 'Make it red{Enter}')
    const edited = [{ ...entries[0]!, text: 'Make it red' }, entries[1]!]
    rerender(<QueuedMessages entries={edited} sessionId="s1" canChange refusals={{ q1: 1 }} onRemove={() => true} onEdit={() => true} />)
    expect(item()).toHaveTextContent('Make it red')
    expect(item()).not.toHaveTextContent('Saving…')
    expect(within(item()).getByRole('button', { name: 'Edit queued message' })).toBeEnabled()
    // Changed later from elsewhere (another tab, the API, an agent's tool): those words, plainly.
    const elsewhere = [{ ...entries[0]!, text: 'Make it green' }, entries[1]!]
    rerender(<QueuedMessages entries={elsewhere} sessionId="s1" canChange refusals={{ q1: 1 }} onRemove={() => true} onEdit={() => true} />)
    expect(item()).toHaveTextContent('Make it green')
    expect(item()).not.toHaveTextContent('Saving…')
    expect(within(item()).getByRole('button', { name: 'Edit queued message' })).toBeEnabled()
  })

  it('offers no changes to a viewer who may not send to the session', () => {
    render(<QueuedMessages entries={entries} sessionId="s1" canChange={false} refusals={{}} onRemove={() => true} onEdit={() => true} />)
    expect(screen.queryByRole('button')).toBeNull()
  })
})
