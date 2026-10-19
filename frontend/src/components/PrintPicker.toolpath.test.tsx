import { fireEvent, screen } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import type { Output } from '../api/types'
import type * as PreviewSliceModule from '../lib/usePreviewSlice'
import type { PreviewSliceState } from '../lib/usePreviewSlice'
import * as fixtures from '../mocks/fixtures'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { PrintPicker } from './PrintPicker'

// #2072 — what the background slice came to, set per test.
const slice = vi.hoisted(() => ({
  state: {
    result: null,
    plateId: 1,
    plan: null,
    stale: false,
    slicing: false,
    progress: null,
    toolpathUrl: null,
    error: null,
  } satisfies PreviewSliceState as PreviewSliceState,
}))
vi.mock('../lib/usePreviewSlice', async (actual) => ({
  ...(await actual<typeof PreviewSliceModule>()),
  usePreviewSlice: () => slice.state,
}))
vi.mock('./print/PlateScene', () => ({ PlateScene: () => <div data-testid="plate-scene" /> }))
vi.mock('./print/PlatePreviewDialog', () => ({
  PlatePreviewDialog: ({ toolpathUrl }: { toolpathUrl?: string | null }) => (
    <div role="dialog" aria-label="Plate in 3D" data-toolpath={String(toolpathUrl ?? null)} />
  ),
}))

const output = fixtures.outputs[0] as Output

async function openPreview(): Promise<HTMLElement> {
  renderPage(<PrintPicker open source={{ kind: 'output', output }} onClose={vi.fn()} onRan={vi.fn()} />)
  fireEvent.click(await screen.findByTestId('plate-preview-open'))
  return screen.getByRole('dialog', { name: 'Plate in 3D' })
}

const done = { job_id: 42, status: 'completed', slicer: 'bambuddy' as const, slots: [] }
/** What the hook hands the picker for `done` (its own test covers how it is built). */
const doneToolpath = `/api/v1/print/outputs/${output.id}/preview-slices/42/toolpath`

describe('PrintPicker · the slice in the 3D preview (#2072)', () => {
  it('hands the preview the finished slice’s toolpath', async () => {
    slice.state = { ...slice.state, result: done, toolpathUrl: doneToolpath, stale: false }
    const dialog = await openPreview()
    expect(dialog).toHaveAttribute('data-toolpath', `/api/v1/print/outputs/${output.id}/preview-slices/42/toolpath`)
  })

  it('hands it none for a multi-plate output, whose preview is every plate (review on #2791)', async () => {
    server.use(
      http.get('/api/v1/outputs/:id/plates', () =>
        HttpResponse.json([
          { index: 1, has_thumbnail: true },
          { index: 2, has_thumbnail: true },
        ]),
      ),
    )
    slice.state = { ...slice.state, result: done, toolpathUrl: doneToolpath, stale: false }
    const dialog = await openPreview()
    expect(dialog).toHaveAttribute('data-toolpath', 'null')
  })

  it('hands it none while the slice shown is of older choices', async () => {
    slice.state = { ...slice.state, result: done, toolpathUrl: doneToolpath, stale: true }
    const dialog = await openPreview()
    expect(dialog).toHaveAttribute('data-toolpath', 'null')
  })
})

describe('PrintPicker · the native slicer\u2019s progress (Task 14/15 review M5)', () => {
  it('hands the pane the slice\u2019s progress', async () => {
    slice.state = { ...slice.state, result: null, toolpathUrl: null, stale: false, slicing: true, progress: 40 }
    renderPage(<PrintPicker open source={{ kind: 'output', output }} onClose={vi.fn()} onRan={vi.fn()} />)
    expect(await screen.findByTestId('slice-progress')).toHaveAttribute('value', '40')
  })
})
