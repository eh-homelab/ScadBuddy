import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
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

  it('describes the enabled button itself, not its wrapper (review of #3727)', async () => {
    render(
      <Tip wrap={false} content="Try it earlier">
        <button type="button">Up</button>
      </Tip>,
    )
    await userEvent.setup().tab()
    const button = screen.getByRole('button', { name: 'Up' })
    expect(button).toHaveFocus()
    expect(await screen.findByRole('tooltip')).toHaveTextContent('Try it earlier')
    expect(button).toHaveAccessibleDescription('Try it earlier')
    expect(button.parentElement).not.toHaveAttribute('aria-describedby')
  })
})
