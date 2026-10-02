import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
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
  const memory = (item: Partial<Extract<FeedItem, { kind: 'memory' }>>, advanced = false) =>
    render(
      <FeedItemView
        item={{ kind: 'memory', id: 'memory-0', action: 'recall', bank: 'scadbuddy', outcome: 'ok', ...item }}
        onDecide={vi.fn()}
        advanced={advanced}
      />,
    )
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

  it('in basic mode says only what memory did, and why when it failed', () => {
    for (const [item, headline] of cases.filter(([c]) => !(c.action === 'retain' && (c.outcome ?? 'ok') === 'ok'))) {
      const { unmount } = memory({ ...item, input: 'make a box', memories: ['1. The user prints in PETG.'] })
      const line = screen.getByTestId('agent-memory')
      expect(line).toHaveTextContent(headline)
      if (item.detail) expect(line).toHaveTextContent(item.detail)
      expect(line).not.toHaveTextContent('Bank scadbuddy')
      expect(screen.queryByTestId('agent-memory-input')).toBeNull()
      expect(screen.queryByTestId('agent-memory-output')).toBeNull()
      unmount()
    }
  })

  it('in basic mode says nothing for a retain that worked', () => {
    memory({ action: 'retain', input: 'user: a box' })
    expect(screen.queryByTestId('agent-memory')).toBeNull()
  })

  it('in advanced mode shows the bank, the query and the memories, open', () => {
    for (const [item, headline] of cases) {
      const { unmount } = memory(item, true)
      const line = screen.getByTestId('agent-memory')
      expect(line).toHaveAttribute('open')
      expect(line.querySelector('summary')).toHaveTextContent(headline)
      expect(line).toHaveTextContent('Bank scadbuddy')
      if (item.detail) expect(line).toHaveTextContent(item.detail)
      unmount()
    }
    memory({ count: 1, input: 'make a <b>box</b>', memories: ['1. The user prints in PETG.'] }, true)
    expect(screen.getByTestId('agent-memory')).toHaveTextContent('Bank scadbuddy · 1 found')
    const query = screen.getByTestId('agent-memory-input')
    expect(query).toHaveAttribute('open')
    expect(query.querySelector('summary')).toHaveTextContent('Query')
    // Plain text, never markup: memories and queries are untrusted.
    expect(query).toHaveTextContent('make a <b>box</b>')
    expect(query.querySelector('b')).toBeNull()
    const found = screen.getByTestId('agent-memory-output')
    expect(found).toHaveAttribute('open')
    expect(found).toHaveTextContent('1. The user prints in PETG.')
  })

  it("labels a retain's input as what was saved", () => {
    memory({ action: 'retain', input: 'user: a box, 40 mm' }, true)
    const saved = screen.getByTestId('agent-memory-input')
    expect(saved.querySelector('summary')).toHaveTextContent('Saved')
    expect(saved).toHaveTextContent('user: a box, 40 mm')
    expect(screen.queryByTestId('agent-memory-output')).toBeNull()
  })
})

describe('the tool card', () => {
  const card = (advanced: boolean) =>
    render(
      <FeedItemView
        item={{
          kind: 'tool',
          id: 't1',
          name: 'mcp__scadbuddy__set_parameters',
          risk: 'write',
          input: { width: 40 },
          result: { ok: true, summary: 'Set 1 parameter', sources: [{ title: 'Customizer docs', url: 'https://example.com/c' }] },
        } as Extract<FeedItem, { kind: 'tool' }>}
        onDecide={vi.fn()}
        advanced={advanced}
      />,
    )

  it('in basic mode shows what ran and how it ended, without its arguments', () => {
    card(false)
    const tool = screen.getByTestId('agent-tool')
    expect(tool).toHaveTextContent('Set 1 parameter')
    expect(screen.queryByTestId('agent-tool-arguments')).toBeNull()
    expect(tool.querySelector('details')).not.toHaveAttribute('open')
  })

  it('in advanced mode shows its arguments and sources, open', () => {
    card(true)
    const args = screen.getByTestId('agent-tool-arguments')
    expect(args).toHaveAttribute('open')
    expect(args).toHaveTextContent('"width": 40')
    expect(screen.getByRole('link', { name: 'Customizer docs' })).toBeVisible()
  })
})

describe('the question card (#940)', () => {
  type Question = Extract<FeedItem, { kind: 'question' }>
  const colour = {
    question: 'Which colour should the base be?',
    header: 'Colour',
    multiSelect: false,
    options: [
      { label: 'Red', description: 'PLA red' },
      { label: 'Blue', description: 'PLA blue' },
    ],
  }
  const draft = {
    question: 'Approve this issue draft?',
    header: 'Draft',
    multiSelect: false,
    options: [
      { label: 'Approve', description: 'File it as written', preview: '## Bed level\n\nThe **first** layer lifts.' },
      { label: 'Cancel', description: 'Do not file it' },
    ],
  }
  const extras = {
    question: 'Which extras?',
    header: 'Extras',
    multiSelect: true,
    options: [
      { label: 'Magnets', description: 'Two 6 mm magnets' },
      { label: 'Hook', description: 'A wall hook' },
    ],
  }

  function ask(questions: Question['questions'], extra: Partial<Question> = {}) {
    const onAnswer = vi.fn()
    const item: Question = { kind: 'question', id: 'q1', tool: 't1', questions, state: 'pending', ...extra }
    const view = render(<FeedItemView item={item} onDecide={vi.fn()} onAnswer={onAnswer} />)
    return { onAnswer, ...view }
  }

  it('sends the chosen option, and only once every question has an answer', async () => {
    const user = userEvent.setup()
    const { onAnswer } = ask([colour, extras])
    const send = screen.getByRole('button', { name: 'Send answer' })
    expect(send).toBeDisabled()
    await user.click(screen.getByRole('radio', { name: /Blue/ }))
    expect(send).toBeDisabled()
    await user.click(screen.getByRole('checkbox', { name: /Magnets/ }))
    await user.click(screen.getByRole('checkbox', { name: /Hook/ }))
    await user.click(send)
    expect(onAnswer).toHaveBeenCalledWith('q1', ['Blue', 'Magnets, Hook'])
  })

  it('takes the user’s own words instead of an option', async () => {
    const user = userEvent.setup()
    const { onAnswer } = ask([colour])
    await user.click(screen.getByRole('radio', { name: 'Other…' }))
    expect(screen.getByRole('button', { name: 'Send answer' })).toBeDisabled()
    // No longer than the agent takes: a longer answer would be refused before reaching the question.
    expect(screen.getByRole('textbox', { name: 'Your answer' })).toHaveAttribute('maxlength', '20000')
    await user.type(screen.getByRole('textbox', { name: 'Your answer' }), 'Green, please')
    await user.click(screen.getByRole('button', { name: 'Send answer' }))
    expect(onAnswer).toHaveBeenCalledWith('q1', ['Green, please'])
  })

  it('shows a draft as Markdown, approves it, or returns the user’s edit of it', async () => {
    const user = userEvent.setup()
    const { onAnswer, unmount } = ask([draft])
    const preview = screen.getByTestId('agent-question-preview')
    expect(screen.getByRole('heading', { name: 'Bed level' })).toBeVisible()
    expect(preview.querySelector('strong')).toHaveTextContent('first')
    // Cancel declines the draft, so it is not shown under Cancel.
    await user.click(screen.getByRole('radio', { name: /Cancel/ }))
    expect(screen.queryByTestId('agent-question-preview')).toBeNull()
    expect(screen.getByRole('radio', { name: 'Edit…' })).toBeInTheDocument()
    await user.click(screen.getByRole('radio', { name: /Approve/ }))
    expect(screen.getByRole('heading', { name: 'Bed level' })).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Send answer' }))
    expect(onAnswer).toHaveBeenLastCalledWith('q1', ['Approve'])
    unmount()

    const edit = ask([draft])
    await user.click(screen.getByRole('radio', { name: 'Edit…' }))
    // The draft to edit, as the agent wrote it.
    const box = screen.getByRole('textbox', { name: 'Your answer' })
    expect(box).toHaveValue('## Bed level\n\nThe **first** layer lifts.')
    await user.clear(box)
    await user.type(box, '## Bed adhesion')
    await user.click(screen.getByRole('button', { name: 'Send answer' }))
    expect(edit.onAnswer).toHaveBeenCalledWith('q1', ['## Bed adhesion'])
  })

  it('offers its controls only to the user, never to the browser bridge', () => {
    ask([colour])
    for (const control of [...screen.getAllByRole('radio'), screen.getByRole('button', { name: 'Send answer' })]) {
      expect(control.closest('[data-agent-user-only]')).not.toBeNull()
    }
  })

  it('says where the answer is once it is not pending', () => {
    const cases: [Partial<Question>, string][] = [
      [{ state: 'sent' }, 'Sending your answer…'],
      [{ state: 'queued' }, 'Not connected: your answer goes first when the assistant reconnects.'],
      [{ state: 'answered', answers: ['Blue'], by: you }, 'Answered by You: Blue'],
      [{ state: 'cancelled', reason: 'interrupted by You' }, 'Not answered: interrupted by You.'],
    ]
    for (const [extra, text] of cases) {
      const { unmount } = ask([colour], extra)
      expect(screen.queryByRole('button', { name: 'Send answer' })).not.toBeInTheDocument()
      expect(screen.queryByRole('radio')).not.toBeInTheDocument()
      expect(screen.getByRole('status')).toHaveTextContent(text)
      unmount()
    }
  })
})
