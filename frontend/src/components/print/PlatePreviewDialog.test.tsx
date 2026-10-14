import { fireEvent, render, screen, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { useEffect } from 'react'
import { describe, expect, it, vi } from 'vitest'
import type { Toolpath } from '../../lib/toolpath'
import { server } from '../../mocks/server'
import { demoToolpath, encodeToolpath } from '../../mocks/toolpath'
import { PlatePreviewDialog } from './PlatePreviewDialog'

// jsdom draws no WebGL: the scene reports a 10 mm model and shows what it was asked for.
vi.mock('./PlateScene', () => ({
  PlateScene: ({
    url,
    colors,
    cutAt,
    onHeight,
  }: {
    url: string
    colors: Map<string, string>
    cutAt: number | null
    onHeight: (height: number) => void
  }) => {
    useEffect(() => onHeight(10), [onHeight])
    return (
      <div data-testid="scene" data-url={url} data-cut={String(cutAt)} data-colors={JSON.stringify([...colors])} />
    )
  },
}))

vi.mock('./ToolpathScene', () => ({
  ToolpathScene: ({ path, view, layer }: { path: Toolpath; view: string; layer: number | null }) => (
    <div
      data-testid="toolpath"
      data-view={view}
      data-layer={String(layer)}
      data-segments={String(path.header.segment_count)}
    />
  ),
}))

const TOOLPATH = '/api/v1/print/outputs/abc/preview-slices/9/toolpath'

function openSliced(body: ArrayBuffer | null = demoToolpath()) {
  const read = vi.fn()
  server.use(
    http.get(TOOLPATH, () => {
      read()
      return body
        ? new HttpResponse(body, { headers: { 'Content-Type': 'application/octet-stream' } })
        : HttpResponse.json({ detail: 'nope' }, { status: 422 })
    }),
  )
  render(
    <PlatePreviewDialog
      open
      onClose={vi.fn()}
      url="/api/v1/outputs/abc/preview.glb"
      label="Lid"
      colors={new Map()}
      toolpathUrl={TOOLPATH}
    />,
  )
  return read
}

function open(onClose = vi.fn()) {
  render(
    <PlatePreviewDialog
      open
      onClose={onClose}
      url="/api/v1/outputs/abc/preview.glb"
      label="Lid"
      colors={new Map([['#FF0000', '#0000FF']])}
    />,
  )
  return onClose
}

describe('PlatePreviewDialog (#1723)', () => {
  it('opens on the Colors view: the whole model, in the chosen spools', () => {
    open()
    expect(screen.getByRole('dialog', { name: 'Lid in 3D' })).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: 'Colors' })).toHaveAttribute('aria-checked', 'true')
    const scene = screen.getByTestId('scene')
    expect(scene).toHaveAttribute('data-url', '/api/v1/outputs/abc/preview.glb')
    expect(scene).toHaveAttribute('data-cut', 'null')
    expect(scene).toHaveAttribute('data-colors', JSON.stringify([['#FF0000', '#0000FF']]))
    expect(screen.queryByRole('slider', { name: 'Layer height' })).toBeNull()
  })

  it('switches to Layers, cuts the model at the slider, and back', () => {
    open()
    fireEvent.click(screen.getByRole('radio', { name: 'Layers' }))
    const slider = screen.getByRole('slider', { name: 'Layer height' })
    // Starts with the whole model showing: the cut at its (rounded-up) top.
    expect(screen.getByTestId('scene')).toHaveAttribute('data-cut', '10')
    expect(slider).toHaveAttribute('max', '10')
    fireEvent.change(slider, { target: { value: '4' } })
    expect(screen.getByTestId('scene')).toHaveAttribute('data-cut', '4')
    fireEvent.click(screen.getByRole('radio', { name: 'Colors' }))
    expect(screen.getByTestId('scene')).toHaveAttribute('data-cut', 'null')
    expect(screen.queryByRole('slider', { name: 'Layer height' })).toBeNull()
  })

  it('closes from its own button', () => {
    const onClose = open()
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(onClose).toHaveBeenCalled()
  })

  it('offers Speed and Movement only once the background slice has finished (#2072)', () => {
    open()
    expect(screen.getByRole('radio', { name: 'Speed' })).toBeDisabled()
    expect(screen.getByRole('radio', { name: 'Movement' })).toBeDisabled()
    expect(screen.getByText(/appear once it has finished/)).toBeInTheDocument()
    // Layers without a slice is still the model cut.
    fireEvent.click(screen.getByRole('radio', { name: 'Layers' }))
    expect(screen.getByTestId('scene')).toHaveAttribute('data-cut', '10')
  })
})

describe('PlatePreviewDialog · the slice (#2072)', () => {
  it('reads the toolpath only when a view draws it, and draws Speed with its legend', async () => {
    const read = openSliced()
    expect(screen.getByTestId('scene')).toBeInTheDocument()
    expect(read).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('radio', { name: 'Speed' }))
    const scene = await screen.findByTestId('toolpath')
    expect(scene).toHaveAttribute('data-view', 'speed')
    expect(scene).toHaveAttribute('data-segments', '6')
    expect(screen.getByLabelText('Speed legend')).toHaveTextContent('200 mm/s')

    fireEvent.click(screen.getByRole('radio', { name: 'Movement' }))
    expect(screen.getByTestId('toolpath')).toHaveAttribute('data-view', 'movement')
    expect(within(screen.getByRole('list', { name: 'Movement legend' })).getByText('Travel')).toBeInTheDocument()
    expect(read).toHaveBeenCalledTimes(1)
  })

  it('shows the slice’s own layers, by feature, on a layer slider', async () => {
    openSliced()
    fireEvent.click(screen.getByRole('radio', { name: 'Layers' }))
    const scene = await screen.findByTestId('toolpath')
    expect(scene).toHaveAttribute('data-layer', '1')
    expect(screen.queryByRole('slider', { name: 'Layer height' })).toBeNull()
    const slider = screen.getByRole('slider', { name: 'Layer' })
    expect(slider).toHaveAttribute('max', '2')
    fireEvent.change(slider, { target: { value: '1' } })
    expect(screen.getByTestId('toolpath')).toHaveAttribute('data-layer', '0')
    expect(screen.getByText(/Layer/, { selector: 'span' }).parentElement).toHaveTextContent('Layer 1 of 2, at 0.20 mm')
    expect(within(screen.getByRole('list', { name: 'Features' })).getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      'Outer wall',
      'Sparse infill',
    ])
  })

  it('says when the slice was too large to draw whole', async () => {
    openSliced(
      encodeToolpath({ features: ['w'], layers: [{ z: 0.2, first_segment: 0 }], max_speed: 1, truncated: true }, [
        [0, 0, 0.2, 1, 0, 0.2, 1, 1],
      ]),
    )
    fireEvent.click(screen.getByRole('radio', { name: 'Speed' }))
    expect(await screen.findByText(/too large to draw whole: the first 1 layers/)).toBeInTheDocument()
  })

  it('says why when the toolpath cannot be read, in the problem’s own words', async () => {
    openSliced(null)
    fireEvent.click(screen.getByRole('radio', { name: 'Speed' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('nope')
  })

  it('reads a toolpath once, however often the views are switched', async () => {
    const read = openSliced()
    fireEvent.click(screen.getByRole('radio', { name: 'Speed' }))
    await screen.findByTestId('toolpath')
    fireEvent.click(screen.getByRole('radio', { name: 'Colors' }))
    fireEvent.click(screen.getByRole('radio', { name: 'Speed' }))
    expect(screen.getByTestId('toolpath')).toHaveAttribute('data-view', 'speed')
    expect(read).toHaveBeenCalledTimes(1)
  })

  it('reads it again on Retry after a busy answer', async () => {
    let busy = true
    server.use(
      http.get(TOOLPATH, () =>
        busy
          ? HttpResponse.json({ detail: 'Other toolpaths are being read; try again shortly.' }, { status: 503 })
          : new HttpResponse(demoToolpath(), { headers: { 'Content-Type': 'application/octet-stream' } }),
      ),
    )
    render(
      <PlatePreviewDialog
        open
        onClose={vi.fn()}
        url="/api/v1/outputs/abc/preview.glb"
        label="Lid"
        colors={new Map()}
        toolpathUrl={TOOLPATH}
      />,
    )
    fireEvent.click(screen.getByRole('radio', { name: 'Speed' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Other toolpaths are being read')
    busy = false
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByTestId('toolpath')).toHaveAttribute('data-view', 'speed')
    expect(screen.queryByRole('alert')).toBeNull()
  })
})
