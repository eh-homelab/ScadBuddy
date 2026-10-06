import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { FeedItem } from '../../agent/chat/state'
import { FeedItemView } from './FeedItemView'

const you = { kind: 'browser' as const, id: 'browser', label: 'You' }

function card(state: Extract<FeedItem, { kind: 'approval' }>['state']) {
  const item: FeedItem = { kind: 'approval', id: 'a1', tool: 't1', summary: 'Send it?', state, by: you, ...(state === 'closed' ? { reason: 'it expired' } : {}) }
  return render(<FeedItemView item={item} onDecide={vi.fn()} onAnswer={vi.fn()} />)
}

describe('the approval card', () => {
  it('offers the buttons only while pending, and says where the answer is otherwise', () => {
    const cases: [Parameters<typeof card>[0], string | null][] = [
      ['pending', null],
      ['sent', 'Sending your answer…'],
      ['approved', 'Approved by You.'],
      ['denied', 'Denied by You.'],
      ['closed', 'Your decision was not taken: it expired.'],
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
        onAnswer={vi.fn()}
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
        onAnswer={vi.fn()}
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

  it('marks the user’s own words in a multi-select answer, after the picked labels', async () => {
    const user = userEvent.setup()
    const { onAnswer } = ask([extras])
    await user.click(screen.getByRole('checkbox', { name: /Hook/ }))
    await user.click(screen.getByRole('checkbox', { name: 'Other…' }))
    await user.type(screen.getByRole('textbox', { name: 'Your answer' }), 'a lanyard, too')
    await user.click(screen.getByRole('button', { name: 'Send answer' }))
    expect(onAnswer).toHaveBeenCalledWith('q1', ['Hook, Other: a lanyard, too'])
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

  it('will not send a multi-select answer longer than the agent takes, picked labels included', async () => {
    const user = userEvent.setup()
    const { onAnswer } = ask([extras])
    await user.click(screen.getByRole('checkbox', { name: /Magnets/ }))
    await user.click(screen.getByRole('checkbox', { name: 'Other…' }))
    const box = screen.getByRole('textbox', { name: 'Your answer' })
    // "Other: " plus this is exactly the cap; with "Magnets, " in front it goes over.
    await user.click(box)
    await user.paste('x'.repeat(20_000 - 'Other: '.length))
    expect(screen.getByRole('button', { name: 'Send answer' })).toBeDisabled()
    expect(screen.getByRole('alert')).toHaveTextContent(/longer than the assistant takes/)
    await user.click(screen.getByRole('checkbox', { name: /Magnets/ }))
    expect(screen.queryByRole('alert')).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Send answer' }))
    expect(onAnswer).toHaveBeenCalledWith('q1', [`Other: ${'x'.repeat(20_000 - 'Other: '.length)}`])
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
      [{ state: 'answered', answers: ['Blue'], by: you }, 'Answered by You: Blue'],
      [{ state: 'cancelled', reason: 'interrupted by You' }, 'Not answered: interrupted by You.'],
      [{ state: 'closed', reason: 'it was already resolved elsewhere' }, 'Your answer was not taken: it was already resolved elsewhere.'],
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

describe('the attention card (#815)', () => {
  type Question = Extract<FeedItem, { kind: 'question' }>
  const card = {
    question: 'The ScadBuddy tab closed. Reopen it so I can select the plate?',
    header: 'Tab disconnected',
    multiSelect: false,
    options: [
      { label: "I'm here", description: '' },
      { label: 'Carry on without me', description: '' },
    ],
  }
  const expiresAt = new Date(Date.UTC(2026, 9, 4, 9, 5)).toISOString()

  function raise(onTimeout: 'proceed' | 'wait' | 'stop', extra: Partial<Question> = {}) {
    const onAnswer = vi.fn()
    const item: Question = {
      kind: 'question',
      id: 'att1',
      tool: 't1',
      questions: [card],
      attention: { reason: 'tab_disconnected', onTimeout, expiresAt },
      state: 'pending',
      ...extra,
    }
    const view = render(<FeedItemView item={item} onDecide={vi.fn()} onAnswer={onAnswer} />)
    return { onAnswer, ...view }
  }

  it('says what it needs and what its timer does, and the user acknowledges it with a quick reply', async () => {
    const user = userEvent.setup()
    const { onAnswer } = raise('proceed')
    expect(screen.getByTestId('agent-attention')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'The assistant needs you: Tab disconnected' })).toBeInTheDocument()
    expect(screen.getByTestId('agent-attention-timer')).toHaveTextContent(/carries on with work that needs no approval\. A timeout never approves anything\./)
    await user.click(screen.getByRole('radio', { name: /I'm here/ }))
    await user.click(screen.getByRole('button', { name: 'Send reply' }))
    expect(onAnswer).toHaveBeenCalledWith('att1', ["I'm here"])
  })

  it('or with their own words', async () => {
    const user = userEvent.setup()
    const { onAnswer } = raise('stop')
    expect(screen.getByTestId('agent-attention-timer')).toHaveTextContent(/it stops\.$/)
    await user.click(screen.getByRole('radio', { name: 'Other…' }))
    await user.type(screen.getByRole('textbox', { name: 'Your answer' }), 'Back in five minutes')
    await user.click(screen.getByRole('button', { name: 'Send reply' }))
    expect(onAnswer).toHaveBeenCalledWith('att1', ['Back in five minutes'])
  })

  it("says how long 'wait' waits, naming the day when it is not today", () => {
    const tomorrow = new Date(Date.now() + 86_400_000)
    const { unmount } = raise('wait', { attention: { reason: 'blocked', onTimeout: 'wait', expiresAt: tomorrow.toISOString() } })
    const day = tomorrow.toLocaleString([], { weekday: 'short' })
    expect(screen.getByTestId('agent-attention-timer')).toHaveTextContent(new RegExp(`^It waits for you until ${day}.+, then stops\\.$`))
    unmount()
    raise('proceed', { attention: { reason: 'blocked', onTimeout: 'proceed', expiresAt: new Date(Date.now() + 60_000).toISOString() } })
    expect(screen.getByTestId('agent-attention-timer')).not.toHaveTextContent(day)
  })

  it('once timed out, says nobody replied, and offers nothing to answer', () => {
    raise('proceed', { state: 'cancelled', reason: 'nobody replied in time (on_timeout: proceed)' })
    expect(screen.queryByRole('button', { name: 'Send reply' })).not.toBeInTheDocument()
    expect(screen.queryByTestId('agent-attention-timer')).not.toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('No reply: nobody replied in time (on_timeout: proceed).')
  })
})

describe('the done summary (#815 §4)', () => {
  type Question = Extract<FeedItem, { kind: 'question' }>
  const summary = '**While nobody answered (attention request 01234567 timed out)**\n- created preset `night` of sign (save_preset)'

  function post(extra: Partial<Question> = {}) {
    const onAnswer = vi.fn()
    const item: Question = {
      kind: 'question',
      id: 'done1',
      tool: 't1',
      questions: [
        {
          question: 'Rendered the sign headlessly; the plate still needs your tab.',
          header: 'Done',
          multiSelect: false,
          options: [
            { label: 'Dismiss', description: '' },
            { label: 'Got it', description: '' },
          ],
        },
      ],
      attention: { reason: 'done', summary },
      state: 'pending',
      ...extra,
    }
    render(<FeedItemView item={item} onDecide={vi.fn()} onAnswer={onAnswer} />)
    return onAnswer
  }

  it('renders a name from a tool literally: the agent puts it in a code span, so no link or emphasis', () => {
    // As agent questions/doneSummary.ts writes a hostile model slug and tool name.
    post({ attention: { reason: 'done', summary: '**What this turn changed**\n- created preset `x` of `evil [click](http://e)` (`a*b*c`)' } })
    const record = screen.getByTestId('agent-done-summary')
    expect(record.querySelector('a, em')).toBeNull()
    expect(record).toHaveTextContent('created preset x of evil [click](http://e) (a*b*c)')
  })

  it("shows the agent's message and ScadBuddy's own record, with no timer and nothing to reply, and dismisses", async () => {
    const user = userEvent.setup()
    const onAnswer = post()
    expect(screen.getByRole('heading', { name: 'The assistant is done' })).toBeInTheDocument()
    expect(screen.getByText('Rendered the sign headlessly; the plate still needs your tab.')).toBeInTheDocument()
    expect(screen.getByTestId('agent-done-summary')).toHaveTextContent(/While nobody answered.*created preset night of sign \(save_preset\)/)
    expect(screen.queryByTestId('agent-attention-timer')).not.toBeInTheDocument()
    expect(screen.queryByRole('radio')).not.toBeInTheDocument()
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(onAnswer).toHaveBeenCalledWith('done1', ['Dismiss'])
  })

  it('once dismissed or replaced, says so and keeps the record', () => {
    post({ state: 'answered', answers: ['Dismiss'], by: you })
    expect(screen.queryByRole('button', { name: 'Dismiss' })).not.toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('Dismissed by You.')
    expect(screen.getByTestId('agent-done-summary')).toBeInTheDocument()
  })
})

describe('the tab-disconnected card (#815)', () => {
  it('says the tab is back once the agent resolved it as reconnected', () => {
    const item: FeedItem = {
      kind: 'question',
      id: 'att2',
      tool: 't2',
      questions: [
        {
          question: 'I need your ScadBuddy tab for browser_snapshot, but it is not connected.',
          header: 'Tab disconnected',
          multiSelect: false,
          options: [
            { label: "I'm back", description: '' },
            { label: 'Carry on without the tab', description: '' },
          ],
        },
      ],
      attention: { reason: 'tab_disconnected', onTimeout: 'proceed', expiresAt: new Date().toISOString() },
      state: 'cancelled',
      reason: 'the ScadBuddy tab is connected again',
      reconnected: true,
    }
    render(<FeedItemView item={item} onDecide={vi.fn()} onAnswer={vi.fn()} />)
    expect(screen.getByRole('status')).toHaveTextContent('The tab is back. The assistant re-reads the page before it changes anything.')
    expect(screen.queryByRole('button', { name: 'Send reply' })).not.toBeInTheDocument()
  })
})
