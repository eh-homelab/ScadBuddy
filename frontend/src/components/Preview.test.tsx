import { render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { Job } from '../api/types'
import { TEMPLATE_NOTES } from '../mocks/fixtures'
import { Preview } from './Preview'

// WebGL does not exist in jsdom: the scene is dropped and only the overlays render.
vi.mock('@react-three/fiber', () => ({
  Canvas: () => null,
  useLoader: () => ({ scene: { clone: () => ({}) } }),
  useThree: () => null,
}))
vi.mock('@react-three/drei', () => ({ Grid: () => null, OrbitControls: () => null }))

function job(overrides: Partial<Job>): Job {
  return {
    id: 'a'.repeat(32),
    slug: 'name-puzzle',
    status: 'done',
    created_at: '2026-09-27T10:00:00Z',
    preview_url: '/api/v1/jobs/aaaa/preview.glb',
    bbox_mm: { min: [0, 0, 0], max: [10, 10, 5], size: [10, 10, 5] },
    colors: ['#FF0000'],
    log_tail: ['ECHO: "NOTE: letter_size reduced"'],
    ...overrides,
  }
}

describe('Preview', () => {
  it("shows a successful render's template notes (#285)", () => {
    render(<Preview job={job({ notes: TEMPLATE_NOTES })} rendering={false} />)

    const notes = screen.getByRole('region', { name: 'Notes from the template' })
    expect(within(notes).getAllByRole('listitem').map((item) => item.textContent)).toEqual(
      TEMPLATE_NOTES,
    )
    // Next to the render, not in place of it.
    expect(screen.getByTestId('bbox-readout')).toBeInTheDocument()
  })

  it('shows nothing extra when the template had nothing to say', () => {
    render(<Preview job={job({ notes: [] })} rendering={false} />)
    expect(screen.queryByTestId('render-notes')).not.toBeInTheDocument()
  })

  it('keeps the notes of the render on screen while the next one runs', () => {
    const { rerender } = render(<Preview job={job({ notes: TEMPLATE_NOTES })} rendering={false} />)
    rerender(
      <Preview job={job({ id: 'b'.repeat(32), status: 'running', notes: null })} rendering />,
    )
    expect(screen.getByTestId('render-notes')).toHaveTextContent(TEMPLATE_NOTES[0]!)
  })

  it('gives a failed render the log instead', () => {
    const { rerender } = render(<Preview job={job({ notes: TEMPLATE_NOTES })} rendering={false} />)
    rerender(
      <Preview
        job={job({ id: 'b'.repeat(32), status: 'failed', log_tail: ['ERROR: boom'] })}
        rendering={false}
      />,
    )
    expect(screen.queryByTestId('render-notes')).not.toBeInTheDocument()
    expect(screen.getByTestId('render-log')).toHaveTextContent('ERROR: boom')
  })
})
