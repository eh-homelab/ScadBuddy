import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { WebhookError } from './WebhookError'

const AT = '2026-11-08T04:40:00Z'

describe('WebhookError (#5684)', () => {
  it.each([
    ['signature', /a request with a bad signature arrived — if this repeats, check the signing secret/],
    ['provider_unavailable', /did not answer/],
    ['out_of_order', /an event arrived before one it depends on; the provider sends it again/],
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
