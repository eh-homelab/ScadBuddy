import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { TowerCheck } from '../../api/types'
import { TowerConflictNotice } from './TowerConflictNotice'

const conflict = {
  plate_id: 2,
  reason: 'on-objects' as const,
  x: 165,
  y: 150,
  message: 'Plate 2: the prime tower stands on the objects. Move the prime tower to a free spot.',
}

describe('TowerConflictNotice (#6758)', () => {
  it('offers Re-arrange for an arranged output, and no Move with no free spot', () => {
    const rearrange = vi.fn()
    const check: TowerCheck = { conflicts: [{ ...conflict, move_x: null, move_y: null }], fix: null }
    render(<TowerConflictNotice check={check} moved={false} onMove={vi.fn()} onUndo={vi.fn()} onRearrange={rearrange} />)

    expect(screen.queryByRole('button', { name: 'Move the prime tower' })).not.toBeInTheDocument()
    expect(screen.queryByText(/Free spot/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Re-arrange with room for the tower' }))
    expect(rearrange).toHaveBeenCalledOnce()
  })

  it('hands the fix to Move', () => {
    const move = vi.fn()
    const fix = { wipe_tower_x: ['200', '40'], wipe_tower_y: ['150', '10'] }
    render(
      <TowerConflictNotice
        check={{ conflicts: [{ ...conflict, move_x: 40, move_y: 10 }], fix }}
        moved={false}
        onMove={move}
        onUndo={vi.fn()}
      />,
    )
    expect(screen.getByText('Free spot: x 40, y 10.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Re-arrange/ })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Move the prime tower' }))
    expect(move).toHaveBeenCalledWith(fix)
  })

  it('shows nothing with no conflict and no move', () => {
    const { container } = render(
      <TowerConflictNotice check={{ conflicts: [], fix: null }} moved={false} onMove={vi.fn()} onUndo={vi.fn()} />,
    )
    expect(container).toBeEmptyDOMElement()
  })
})
