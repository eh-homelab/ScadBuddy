import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderPage } from '../../test/utils'
import { SessionFlowRuns } from './SessionFlowRuns'

// #1057 — the panel's link to the flow runs a session started (plan 2026-10-09 Task F2),
// over the msw runs (src/mocks/features/flows.ts: `run-ask` was started by `sess-1`).

describe('SessionFlowRuns', () => {
  it('links to the runs the session started, and shows nothing for a session that started none', async () => {
    const { unmount } = renderPage(<SessionFlowRuns sessionId="sess-1" />)
    expect(await screen.findByRole('link', { name: 'Flow runs started here (1)' })).toHaveAttribute(
      'href',
      '/workflows?session=sess-1',
    )
    unmount()
    const { container } = renderPage(<SessionFlowRuns sessionId="sess-none" />)
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(container).toBeEmptyDOMElement()
  })
})
