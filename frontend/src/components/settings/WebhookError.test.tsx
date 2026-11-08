import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { WebhookError } from './WebhookError'

const AT = '2026-11-08T04:40:00Z'

describe('WebhookError (#5684)', () => {
  it.each([
    ['signature', /signature did not match/],
    ['provider_unavailable', /did not answer/],
    ['handler_error', /could not record it/],
  ] as const)('shows a fixed sentence for %s, with its time', (error, text) => {
    render(<WebhookError error={error} at={AT} />)
    const shown = screen.getByRole('status')
    expect(shown).toHaveTextContent(text)
    expect(shown.querySelector('time')).toHaveAttribute('datetime', new Date(AT).toISOString())
  })

  it('shows nothing when the last webhook succeeded', () => {
    const { container } = render(<WebhookError error={null} at={null} />)
    expect(container).toBeEmptyDOMElement()
  })
})
