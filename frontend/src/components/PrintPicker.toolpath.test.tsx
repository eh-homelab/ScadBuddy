import { fireEvent, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { Output } from '../api/types'
import type * as PreviewSliceModule from '../lib/usePreviewSlice'
import type { PreviewSliceState } from '../lib/usePreviewSlice'
import * as fixtures from '../mocks/fixtures'
import { renderPage } from '../test/utils'
import { PrintPicker } from './PrintPicker'

// #2072 — what the background slice came to, set per test.
const slice = vi.hoisted(() => ({
  state: { result: null, plan: null, stale: false, slicing: false, error: null } as PreviewSliceState,
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

const done = { job_id: 42, status: 'completed', slots: [] }

describe('PrintPicker · the slice in the 3D preview (#2072)', () => {
  it('hands the preview the finished slice’s toolpath', async () => {
    slice.state = { ...slice.state, result: done, stale: false }
    const dialog = await openPreview()
    expect(dialog).toHaveAttribute('data-toolpath', `/api/v1/print/outputs/${output.id}/preview-slices/42/toolpath`)
  })

  it('hands it none while the slice shown is of older choices', async () => {
    slice.state = { ...slice.state, result: done, stale: true }
    const dialog = await openPreview()
    expect(dialog).toHaveAttribute('data-toolpath', 'null')
  })
})
