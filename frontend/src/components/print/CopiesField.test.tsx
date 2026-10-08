import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { CopiesField } from './CopiesField'

function typed(raw: string) {
  const onChange = vi.fn()
  render(<CopiesField value={null} remembered={null} onChange={onChange} />)
  fireEvent.change(screen.getByLabelText('Copies'), { target: { value: raw } })
  return onChange
}

describe('CopiesField (#1046)', () => {
  it('agrees with the API and Options → Quantity on its bounds', () => {
    render(<CopiesField value={null} remembered={null} onChange={vi.fn()} />)
    const box = screen.getByLabelText('Copies')
    expect(box).toHaveAttribute('min', '1')
    expect(box).toHaveAttribute('max', '1000')
    expect(box).toHaveAttribute('step', '1')
  })

  it.each([
    ['7', 7],
    ['0', 1],
    ['-3', 1],
    ['1000', 1000],
    ['1001', 1000],
    ['99999', 1000],
    ['2.5', 3],
    ['2.4', 2],
  ])('typing %s queues %d', (raw, sent) => {
    expect(typed(raw)).toHaveBeenLastCalledWith(sent)
  })

  it('a cleared box sends null, so the remembered quantity is queued', () => {
    const onChange = vi.fn()
    render(<CopiesField value={4} remembered={null} onChange={onChange} />)
    fireEvent.change(screen.getByLabelText('Copies'), { target: { value: '' } })
    expect(onChange).toHaveBeenLastCalledWith(null)
  })
})
