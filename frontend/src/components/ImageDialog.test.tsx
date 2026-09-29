import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ImageDialog } from './ImageDialog'

describe('ImageDialog', () => {
  function setup() {
    const captureImage = vi.fn(async () => new Blob(['png'], { type: 'image/png' }))
    URL.createObjectURL = vi.fn(() => 'blob:preview')
    URL.revokeObjectURL = vi.fn()
    render(
      <ImageDialog
        open
        slug="name-puzzle"
        captureImage={captureImage}
        viewSize={() => ({ width: 800, height: 500 })}
        onClose={() => undefined}
      />,
    )
    return captureImage
  }

  it('offers sizes from the view and previews the choices', async () => {
    const captureImage = setup()
    expect(screen.getByText('1600 × 1000')).toBeInTheDocument()
    expect(screen.getByText('3200 × 2000')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByTestId('image-preview')).toBeInTheDocument())
    expect(captureImage).toHaveBeenLastCalledWith({ scale: 1, plate: true, transparent: false })

    fireEvent.click(screen.getByTestId('image-plate'))
    await waitFor(() =>
      expect(captureImage).toHaveBeenLastCalledWith({ scale: 1, plate: false, transparent: false }),
    )
  })

  it('saves the image at the chosen size, without the plate', async () => {
    const captureImage = setup()
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
    fireEvent.click(screen.getByLabelText(/4×/))
    fireEvent.click(screen.getByTestId('image-plate'))
    fireEvent.click(screen.getByTestId('image-save'))
    await waitFor(() => expect(click).toHaveBeenCalled())
    expect(captureImage).toHaveBeenLastCalledWith({ scale: 4, plate: false, transparent: false })
    click.mockRestore()
  })
})
