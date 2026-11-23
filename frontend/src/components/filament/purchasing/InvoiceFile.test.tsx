import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { invoiceFileUrl } from '../../../api/filamentPurchasing'
import type * as embed from '../../../lib/embed'
import { isEmbedded } from '../../../lib/embed'
import { InvoiceFile } from './InvoiceFile'

vi.mock('../../../lib/embed', async (original) => ({
  ...(await original<typeof embed>()),
  isEmbedded: vi.fn(() => false),
}))

afterEach(() => vi.mocked(isEmbedded).mockReturnValue(false))

const ID = '00000000-0000-4000-8000-000000000001'

const show = (media_type: string, name = 'receipt') => render(<InvoiceFile invoiceId={ID} file={{ name, media_type, size: 10 }} onChanged={() => {}} />)

const photo = () => screen.getByRole('img', { name: /Photo of the invoice/ })
const scaleOf = () => Number(/scale\(([\d.]+)\)/.exec(photo().style.transform)?.[1])
// jsdom 31 serializes `translate(x, 0px)` as `translate(x)`: a missing y is 0.
const offsetOf = () =>
  /translate\((-?[\d.]+)px(?:, (-?[\d.]+)px)?\)/.exec(photo().style.transform)!.slice(1).map((n) => Number(n ?? 0))

/** jsdom lays nothing out: give the viewport a 400 x 300 box at the page's origin. */
function sizeViewport() {
  const viewport = screen.getByTestId('invoice-photo-viewport')
  vi.spyOn(viewport, 'getBoundingClientRect').mockReturnValue({ left: 0, top: 0, width: 400, height: 300, right: 400, bottom: 300, x: 0, y: 0, toJSON: () => ({}) })
  return viewport
}

describe('the invoice file preview (#6889)', () => {
  it('embeds a PDF in a same-origin iframe, not an object the page CSP blocks', () => {
    const { container } = show('application/pdf', 'receipt.pdf')
    const frame = screen.getByTitle('Invoice PDF, receipt.pdf')
    expect(frame.tagName).toBe('IFRAME')
    expect(frame).toHaveAttribute('src', invoiceFileUrl(ID))
    expect(container.querySelector('object')).toBeNull()
    expect(screen.getByRole('link', { name: 'Open' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Download' })).toBeInTheDocument()
  })

  it('points a PDF at Open and Download inside Bambuddy, where the frame has no PDF viewer', () => {
    vi.mocked(isEmbedded).mockReturnValue(true)
    const { container } = show('application/pdf', 'receipt.pdf')
    expect(container.querySelector('iframe, object')).toBeNull()
    expect(screen.getByText(/PDF cannot be shown inside Bambuddy\. Use Open or Download/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open' })).toBeInTheDocument()
  })

  it('points another kind of file at Open and Download', () => {
    const { container } = show('text/plain', 'receipt.txt')
    expect(container.querySelector('iframe, object, img')).toBeNull()
    expect(screen.getByText(/cannot be shown here\. Use Open or Download/)).toBeInTheDocument()
  })

  it('zooms a photo with the buttons, clamps it and resets it', async () => {
    const user = userEvent.setup()
    show('image/jpeg', 'receipt.jpg')
    expect(photo()).toHaveAttribute('src', invoiceFileUrl(ID))
    expect(scaleOf()).toBe(1)
    expect(screen.getByRole('button', { name: 'Zoom out' })).toBeDisabled()

    await user.click(screen.getByRole('button', { name: 'Zoom in' }))
    expect(scaleOf()).toBe(1.5)
    expect(screen.getByLabelText('Zoom level')).toHaveTextContent('150%')
    await user.click(screen.getByRole('button', { name: 'Zoom out' }))
    expect(scaleOf()).toBe(1)

    for (let i = 0; i < 10; i++) await user.click(screen.getByRole('button', { name: 'Zoom in' }))
    expect(scaleOf()).toBe(8)
    expect(screen.getByRole('button', { name: 'Zoom in' })).toBeDisabled()

    await user.click(screen.getByRole('button', { name: 'Fit to view' }))
    expect(scaleOf()).toBe(1)
    expect(offsetOf()).toEqual([0, 0])
  })

  it('zooms on the wheel and a double-click, and pans a zoomed photo only inside its bounds', () => {
    show('image/png')
    const viewport = sizeViewport()

    fireEvent.wheel(viewport, { deltaY: -200, clientX: 200, clientY: 150 })
    expect(scaleOf()).toBeGreaterThan(1)
    fireEvent.doubleClick(viewport, { clientX: 200, clientY: 150 })
    expect(scaleOf()).toBe(1)
    fireEvent.doubleClick(viewport, { clientX: 300, clientY: 150 })
    expect(scaleOf()).toBe(2.5)
    // Zoomed about x = +100 from the centre: the point under the pointer stays put.
    expect(offsetOf()).toEqual([-150, 0])

    // A long drag stops where the image's edge meets the viewport's: (2.5 * 400 - 400) / 2.
    fireEvent.pointerDown(viewport, { pointerId: 1, clientX: 100, clientY: 100, button: 0, pointerType: 'mouse' })
    fireEvent.pointerMove(viewport, { pointerId: 1, clientX: 2000, clientY: 2000, pointerType: 'mouse' })
    fireEvent.pointerUp(viewport, { pointerId: 1, pointerType: 'mouse' })
    expect(offsetOf()).toEqual([300, 225])
  })

  it('pinches to zoom with two touches', () => {
    show('image/png')
    const viewport = sizeViewport()
    fireEvent.pointerDown(viewport, { pointerId: 1, clientX: 150, clientY: 150, pointerType: 'touch' })
    fireEvent.pointerDown(viewport, { pointerId: 2, clientX: 250, clientY: 150, pointerType: 'touch' })
    fireEvent.pointerMove(viewport, { pointerId: 2, clientX: 350, clientY: 150, pointerType: 'touch' })
    fireEvent.pointerUp(viewport, { pointerId: 1, pointerType: 'touch' })
    fireEvent.pointerUp(viewport, { pointerId: 2, pointerType: 'touch' })
    expect(scaleOf()).toBe(2)
  })

  it('keeps a tall receipt inside its letterboxed bounds', () => {
    show('image/png')
    const viewport = sizeViewport()
    // 100 x 400 in a 400 x 300 box: fitted 75 x 300, so there is no vertical slack at 1x.
    Object.defineProperty(photo(), 'naturalWidth', { value: 100 })
    Object.defineProperty(photo(), 'naturalHeight', { value: 400 })
    fireEvent.load(photo())
    fireEvent.doubleClick(viewport, { clientX: 200, clientY: 150 })
    expect(scaleOf()).toBe(2.5)
    fireEvent.pointerDown(viewport, { pointerId: 1, clientX: 100, clientY: 100, button: 0, pointerType: 'mouse' })
    fireEvent.pointerMove(viewport, { pointerId: 1, clientX: 2000, clientY: 2000, pointerType: 'mouse' })
    fireEvent.pointerUp(viewport, { pointerId: 1, pointerType: 'mouse' })
    // x: (75 * 2.5 - 400) / 2 < 0, so none; y: (300 * 2.5 - 300) / 2 = 225.
    expect(offsetOf()).toEqual([0, 225])
  })
})
