import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { Tip } from './Tip'

describe('Tip', () => {
  it('keeps the button it wraps, and its focus, while `wrap` flips (review of #3721)', () => {
    const view = render(
      <Tip wrap={false} content="Try it earlier">
        <button type="button">Up</button>
      </Tip>,
    )
    const button = screen.getByRole('button', { name: 'Up' })
    button.focus()
    view.rerender(
      <Tip wrap content="Try it earlier">
        <button type="button" disabled>
          Up
        </button>
      </Tip>,
    )
    expect(screen.getByRole('button', { name: 'Up' })).toBe(button)
    expect(button.parentElement).toHaveAttribute('tabindex', '0')
    view.rerender(
      <Tip wrap={false} content="Try it earlier">
        <button type="button">Up</button>
      </Tip>,
    )
    expect(screen.getByRole('button', { name: 'Up' })).toBe(button)
    expect(button.parentElement).not.toHaveAttribute('tabindex')
  })
})
