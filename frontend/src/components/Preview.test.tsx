import { fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Diagnostic, Job, Plate } from '../api/types'
import { CANCELLED_ERROR, JOB_WARNINGS, TEMPLATE_NOTES } from '../mocks/fixtures'
import { Preview, type PreviewCapture } from './Preview'

// WebGL does not exist in jsdom: the scene is dropped and only the overlays render.
const scene = vi.hoisted(() => ({ failure: null as Error | null, clear: vi.fn() }))
vi.mock('@react-three/fiber', () => ({
  // A GLB that fails to load throws out of the Canvas, as r3f rethrows it (#361).
  Canvas: () => {
    if (scene.failure) throw scene.failure
    return null
  },
  useLoader: Object.assign(() => ({ scene: { clone: () => ({}) } }), { clear: scene.clear }),
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
  afterEach(() => {
    scene.failure = null
    scene.clear.mockClear()
    // The console.error spies below must not silence React's warnings in later tests.
    vi.restoreAllMocks()
  })

  it('keeps a preview that fails to load to the viewer, with a retry (#361)', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    scene.failure = new Error('Could not load /api/v1/jobs/aaaa/preview.glb: 422')
    render(<Preview job={job({})} rendering={false} controls={<button type="button">Full screen</button>} />)

    expect(screen.getByTestId('preview-failed')).toHaveTextContent('Could not load the preview.')
    // The rest of the viewer stays: its readouts and the page's own buttons.
    expect(screen.getByTestId('bbox-readout')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Full screen' })).toBeInTheDocument()

    // The failed load is dropped from the loader's cache at once, or any remount of the
    // scene — Try again, or coming back to this render later — would rethrow it.
    expect(scene.clear).toHaveBeenCalledWith(expect.anything(), '/api/v1/jobs/aaaa/preview.glb')

    scene.failure = null
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(screen.queryByTestId('preview-failed')).not.toBeInTheDocument()
  })

  it('leaves the page nothing to capture while the preview has failed (#361)', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    // What the scene's onCreated left behind, closing over a renderer now disposed.
    const captureRef: React.RefObject<PreviewCapture | null> = {
      current: {
        capturePng: async () => null,
        captureImage: async () => null,
        viewSize: () => ({ width: 1, height: 1 }),
        cameraView: () => ({ position: [0, 0, 0], target: [0, 0, 0], fov: 35 }),
      },
    }
    scene.failure = new Error('boom')
    render(<Preview job={job({})} rendering={false} captureRef={captureRef} />)
    expect(screen.getByTestId('preview-failed')).toBeInTheDocument()
    expect(captureRef.current).toBeNull()
  })

  it('tries again by itself when the next render arrives (#361)', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    scene.failure = new Error('boom')
    const { rerender } = render(<Preview job={job({})} rendering={false} />)
    expect(screen.getByTestId('preview-failed')).toBeInTheDocument()

    scene.failure = null
    rerender(
      <Preview job={job({ id: 'b'.repeat(32), preview_url: '/api/v1/jobs/bbbb/preview.glb' })} rendering={false} />,
    )
    expect(screen.queryByTestId('preview-failed')).not.toBeInTheDocument()
  })

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

  it('does not blame OpenSCAD for a failure that has no OpenSCAD log (#952)', () => {
    const error = 'building the per-colour solids failed: BadZipFile: File is not a zip file'
    render(<Preview job={job({ status: 'failed', error, log_tail: [] })} rendering={false} />)
    expect(screen.getByText(/ScadBuddy could not finish this render/i)).toBeInTheDocument()
    expect(screen.getByTestId('render-log')).toHaveTextContent(error)
    expect(screen.queryByText(/OpenSCAD could not render these parameters/i)).not.toBeInTheDocument()
  })

  it('tells a cancelled render apart from a failure: it keeps the log but not the OpenSCAD copy', () => {
    const { rerender } = render(<Preview job={job({ notes: TEMPLATE_NOTES })} rendering={false} />)
    rerender(
      <Preview
        job={job({
          id: 'b'.repeat(32),
          status: 'cancelled',
          error: CANCELLED_ERROR,
          log_tail: [CANCELLED_ERROR],
        })}
        rendering={false}
      />,
    )
    expect(screen.queryByTestId('render-notes')).not.toBeInTheDocument()
    expect(screen.getByTestId('render-log')).toHaveTextContent(CANCELLED_ERROR)
    expect(screen.getByText(/this render was cancelled/i)).toBeInTheDocument()
    expect(screen.getByText(/were not the problem/i)).toBeInTheDocument()
    expect(screen.queryByText(/OpenSCAD could not render these parameters/i)).not.toBeInTheDocument()
  })

  it('invites a parameter change before the first render', () => {
    render(<Preview job={undefined} rendering={false} />)
    expect(screen.getByText('Change a parameter to render.')).toBeInTheDocument()
  })

  it('does not invite a parameter change over a render the server refused (#367)', () => {
    render(<Preview job={undefined} rendering={false} rejected />)
    expect(screen.queryByText('Change a parameter to render.')).not.toBeInTheDocument()
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

  it("shows OpenSCAD's warnings from a render that finished, with their lines (#937)", () => {
    render(
      <Preview
        job={job({
          diagnostics: [
            {
              severity: 'warning',
              message: 'module cube() does not support child modules',
              file: 'model.scad',
              line: 6,
            },
            { severity: 'trace', message: "called by 'assert'", file: 'model.scad', line: 2 },
          ],
        })}
        rendering={false}
        sourceLink={<a href="/m/name-puzzle/source">Edit source</a>}
      />,
    )

    const region = screen.getByRole('region', { name: 'OpenSCAD warnings' })
    expect(within(region).getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      'Line 6module cube() does not support child modules',
    ])
    expect(within(region).getByRole('link', { name: 'Edit source' })).toHaveAttribute(
      'href',
      '/m/name-puzzle/source',
    )
  })

  it('shows no OpenSCAD warnings box when the render logged none', () => {
    render(<Preview job={job({ diagnostics: [] })} rendering={false} />)
    expect(screen.queryByRole('region', { name: 'OpenSCAD warnings' })).not.toBeInTheDocument()
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

const H2C = { model: 'H2C', name: 'H2C', size: [330, 320], height: 325 } as unknown as Plate
const OPENSCAD_WARNING: Diagnostic = {
  severity: 'warning',
  message: 'module cube() does not support child modules',
  file: 'model.scad',
  line: 6,
}

/** jsdom lays nothing out: every box reads as `width` × `height`, as a preview that size would. */
function previewSized(width: number, height: number) {
  vi.spyOn(Element.prototype, 'getBoundingClientRect').mockReturnValue(
    DOMRect.fromRect({ x: 0, y: 0, width, height }),
  )
}

describe('Preview overlays on a short preview (#1743, #1744)', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('keeps the full readouts on a preview with room for them', () => {
    previewSized(1080, 582)
    render(<Preview job={job({})} rendering={false} plate={H2C} />)
    expect(screen.getByTestId('preview')).toHaveClass('overflow-hidden')
    expect(screen.getByTestId('plate-badge')).toHaveTextContent('H2C · 330 × 320 mm plate')
    expect(within(screen.getByTestId('bbox-readout')).getByText('Bounding box')).toBeVisible()
  })

  it('shrinks the plate badge and the bounding box to one-line chips below 200 px tall', () => {
    previewSized(390, 120)
    render(<Preview job={job({ plates: [{}, {}] as Job['plates'] })} rendering={false} plate={H2C} />)
    expect(screen.getByTestId('plate-badge')).toHaveTextContent(/^330 × 320 mm$/)
    const bbox = screen.getByTestId('bbox-readout')
    expect(bbox).toHaveTextContent('10.0 × 10.0 × 5.0 mm')
    expect(bbox).toHaveTextContent('2 plates')
    // Named for a screen reader, but not drawn as a heading.
    expect(bbox).toHaveAccessibleName('Bounding box')
    expect(within(bbox).queryByText('Bounding box')).not.toBeInTheDocument()
  })

  it('compacts a preview narrower than 360 px, however tall', () => {
    previewSized(340, 600)
    render(<Preview job={job({})} rendering={false} plate={H2C} />)
    expect(screen.getByTestId('plate-badge')).toHaveTextContent(/^330 × 320 mm$/)
  })

  it('folds the notes and warnings into one chip on a short preview, which opens them', () => {
    previewSized(390, 120)
    render(
      <Preview
        job={job({ notes: TEMPLATE_NOTES, warnings: JOB_WARNINGS, diagnostics: [OPENSCAD_WARNING] })}
        rendering={false}
        sourceLink={<a href="/m/name-puzzle/source">Edit source</a>}
      />,
    )
    expect(screen.queryByRole('region', { name: 'Notes from the template' })).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Render warnings' })).not.toBeInTheDocument()

    const chip = screen.getByRole('button', { name: '2 warnings · 2 notes' })
    expect(chip).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(chip)
    expect(chip).toHaveAttribute('aria-expanded', 'true')

    const notes = screen.getByRole('region', { name: 'Notes from the template' })
    expect(within(notes).getAllByRole('listitem').map((item) => item.textContent)).toEqual(TEMPLATE_NOTES)
    expect(screen.getByRole('region', { name: 'Render warnings' })).toHaveTextContent(JOB_WARNINGS[0]!)
    const openscad = screen.getByRole('region', { name: 'OpenSCAD warnings' })
    expect(within(openscad).getByRole('link', { name: 'Edit source' })).toHaveAttribute(
      'href',
      '/m/name-puzzle/source',
    )

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(chip).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('region', { name: 'Notes from the template' })).not.toBeInTheDocument()
    expect(chip).toHaveFocus()
  })

  it('names a chip of notes alone by its notes', () => {
    previewSized(390, 120)
    render(<Preview job={job({ notes: TEMPLATE_NOTES.slice(0, 1) })} rendering={false} />)
    expect(screen.getByRole('button', { name: '1 note' })).toBeInTheDocument()
  })

  it('folds the panels into the chip when together they would take over a third of the preview', () => {
    previewSized(920, 385)
    vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(180)
    render(<Preview job={job({ notes: TEMPLATE_NOTES, warnings: JOB_WARNINGS })} rendering={false} />)
    expect(screen.queryByRole('region', { name: 'Notes from the template' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '1 warning · 2 notes' })).toBeInTheDocument()
  })

  it('leaves the panels open when they fit, and lets a drag over them reach the scene', () => {
    previewSized(1080, 582)
    vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(120)
    render(<Preview job={job({ notes: TEMPLATE_NOTES })} rendering={false} />)
    const notes = screen.getByRole('region', { name: 'Notes from the template' })
    expect(notes).toHaveClass('pointer-events-none')
    expect(screen.queryByRole('button', { name: /notes?$/ })).not.toBeInTheDocument()
  })
})
