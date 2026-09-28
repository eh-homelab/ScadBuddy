import { render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { Job } from '../api/types'
import { JOB_WARNINGS, TEMPLATE_NOTES } from '../mocks/fixtures'
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

  it('gives a cancelled render the log instead, same as a failure', () => {
    const { rerender } = render(<Preview job={job({ notes: TEMPLATE_NOTES })} rendering={false} />)
    rerender(
      <Preview
        job={job({
          id: 'b'.repeat(32),
          status: 'cancelled',
          error: 'superseded by a newer request',
          log_tail: ['Render cancelled: superseded by a newer request'],
        })}
        rendering={false}
      />,
    )
    expect(screen.queryByTestId('render-notes')).not.toBeInTheDocument()
    expect(screen.getByTestId('render-log')).toHaveTextContent(
      'Render cancelled: superseded by a newer request',
    )
  })

  it('names the step a running render is on (#267)', () => {
    render(<Preview job={undefined} rendering stage="solids" />)
    expect(screen.getByTestId('render-stage')).toHaveTextContent('building each colour')
  })

  it("shows ScadBuddy's own job warnings next to the template notes, apart from them (#383)", () => {
    render(<Preview job={job({ notes: TEMPLATE_NOTES, warnings: JOB_WARNINGS })} rendering={false} />)

    const warnings = screen.getByRole('region', { name: 'Render warnings' })
    expect(within(warnings).getAllByRole('listitem').map((item) => item.textContent)).toEqual(
      JOB_WARNINGS,
    )
    expect(within(warnings).getByText('From ScadBuddy')).toBeInTheDocument()
    // Not folded into the template's own notes.
    const notes = screen.getByRole('region', { name: 'Notes from the template' })
    expect(within(notes).queryByText(JOB_WARNINGS[0]!)).not.toBeInTheDocument()
    expect(screen.getByTestId('bbox-readout')).toBeInTheDocument()
  })

  it('shows no warnings box when the job has none', () => {
    render(<Preview job={job({ warnings: [] })} rendering={false} />)
    expect(screen.queryByTestId('render-warnings')).not.toBeInTheDocument()
  })

  it('keeps the warnings of the render on screen while the next one runs', () => {
    const { rerender } = render(<Preview job={job({ warnings: JOB_WARNINGS })} rendering={false} />)
    rerender(
      <Preview job={job({ id: 'b'.repeat(32), status: 'running', warnings: null })} rendering />,
    )
    expect(screen.getByTestId('render-warnings')).toHaveTextContent(JOB_WARNINGS[0]!)
  })

  it('shows a failed render its warnings above the log (#383)', () => {
    render(
      <Preview
        job={job({ status: 'failed', warnings: JOB_WARNINGS, log_tail: ['ERROR: boom'] })}
        rendering={false}
      />,
    )
    expect(screen.getByRole('region', { name: 'Render warnings' })).toHaveTextContent(
      JOB_WARNINGS[0]!,
    )
    expect(screen.getByTestId('render-log')).toHaveTextContent('ERROR: boom')
  })

  it("does not carry an earlier render's warnings onto a failure", () => {
    const { rerender } = render(<Preview job={job({ warnings: JOB_WARNINGS })} rendering={false} />)
    rerender(
      <Preview
        job={job({ id: 'b'.repeat(32), status: 'failed', warnings: null, log_tail: ['ERROR: boom'] })}
        rendering={false}
      />,
    )
    expect(screen.queryByTestId('render-warnings')).not.toBeInTheDocument()
  })
})
