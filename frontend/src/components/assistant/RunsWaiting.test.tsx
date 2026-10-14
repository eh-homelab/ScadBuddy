import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { RunsWaitingContext, type RunWaiting } from '../../agent/attention'
import { renderPage } from '../../test/utils'
import { RunsWaiting } from './RunsWaiting'

// #2482 — the badge counts a flow run's own calls waiting for the person; the panel it
// opens lists those runs, read from the badge's own poll, since a browser-started run has
// no session to list them under.
const shown = (runs: readonly RunWaiting[] | null) =>
  renderPage(
    <RunsWaitingContext.Provider value={runs}>
      <RunsWaiting />
    </RunsWaitingContext.Provider>,
  )

describe('RunsWaiting', () => {
  it('lists each run with calls waiting for you, linking to its page', () => {
    shown([
      { id: 'run-1', name: 'swap', waiting: 2 },
      { id: 'run-3', name: 'tidy', waiting: 1 },
    ])
    const list = screen.getByRole('list', { name: 'Flow runs waiting for you' })
    expect(within(list).getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      'swap waits for you on 2 calls',
      'tidy waits for you on a call',
    ])
    expect(within(list).getByRole('link', { name: 'swap' })).toHaveAttribute('href', '/workflows/runs/run-1')
  })

  it('shows nothing when no run waits for you, or outside the shell', () => {
    expect(shown([]).container).toBeEmptyDOMElement()
    expect(shown(null).container).toBeEmptyDOMElement()
  })
})
