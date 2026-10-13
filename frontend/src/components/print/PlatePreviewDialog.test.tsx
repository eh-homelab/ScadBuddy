import { fireEvent, render, screen } from '@testing-library/react'
import { useEffect } from 'react'
import { describe, expect, it, vi } from 'vitest'
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
})
