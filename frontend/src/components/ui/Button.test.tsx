import { render, screen } from '@testing-library/react'
import { Button } from './Button'

describe('Button', () => {
  // #1488 — a pressed ghost toggle looked exactly like an unpressed one.
  it('styles a pressed ghost button differently from an unpressed one', () => {
    render(
      <>
        <Button variant="ghost" aria-pressed={false}>
          Off
        </Button>
        <Button variant="ghost" aria-pressed={true}>
          On
        </Button>
      </>,
    )
    const off = screen.getByRole('button', { name: 'Off' }).className
    const on = screen.getByRole('button', { name: 'On' }).className
    expect(on).not.toEqual(off)
    expect(on).toContain('bg-surface-3')
    expect(on).not.toContain('bg-transparent')
  })
})
