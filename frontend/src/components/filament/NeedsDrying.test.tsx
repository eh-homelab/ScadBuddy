import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { NeedsDrying } from './NeedsDrying'
import { outsideDryText } from './outsideDry'

describe('needs drying', () => {
  it('shows the badge only for a spool that needs drying', () => {
    const { rerender } = render(<NeedsDrying spool={{ needs_drying: true }} />)
    expect(screen.getByText('Needs drying')).toBeInTheDocument()
    rerender(<NeedsDrying spool={{ needs_drying: false }} />)
    expect(screen.queryByText('Needs drying')).toBeNull()
  })

  it('writes the hours outside dry storage in plain words', () => {
    expect(outsideDryText(61.5)).toBe('61.5 hours outside dry storage')
    expect(outsideDryText(61.54)).toBe('61.5 hours outside dry storage')
    expect(outsideDryText(0.04)).toBe('No time outside dry storage')
    expect(outsideDryText(1)).toBe('1 hour outside dry storage')
    expect(outsideDryText(0)).toBe('No time outside dry storage')
  })
})
