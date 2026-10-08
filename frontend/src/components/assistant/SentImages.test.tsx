import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState, type KeyboardEvent, type ReactNode } from 'react'
import { afterEach, describe, expect, it } from 'vitest'
import { forgetFullSizes, rememberFullSize } from '../../agent/chat/images'
import type { ImagePreview } from '../../agent/chat/protocol'
import { ModalCompanionContext } from '../../lib/modal'
import { SentImages } from './SentImages'

// #1891 — a sent image, opened large from the transcript.

const preview = (name: string): ImagePreview => ({ mediaType: 'image/jpeg', data: btoa(`preview of ${name}`) })
const previewUrl = (name: string) => `data:image/jpeg;base64,${btoa(`preview of ${name}`)}`

/**
 * The thumbnails inside a stand-in for the assistant panel: a companion region whose own
 * Escape closes it, as AppShell's aside does.
 */
function Panel({ children }: { children: ReactNode }) {
  const [element, setElement] = useState<HTMLElement | null>(null)
  const [open, setOpen] = useState(true)
  const onKey = (event: KeyboardEvent) => {
    if (event.key === 'Escape' && !event.defaultPrevented) setOpen(false)
  }
  return (
    <ModalCompanionContext.Provider value={element}>
      <button type="button">Page behind</button>
      <aside ref={setElement} aria-label="Assistant" hidden={!open} onKeyDown={onKey}>
        {children}
      </aside>
    </ModalCompanionContext.Provider>
  )
}

afterEach(() => forgetFullSizes())

describe('sent images (#1891)', () => {
  it('shows each preview as a button that names the image, at least 24 px', () => {
    render(<SentImages images={[preview('a'), preview('b')]} />)
    const list = screen.getByRole('list', { name: 'Images sent' })
    const buttons = within(list).getAllByRole('button')
    expect(buttons.map((b) => b.getAttribute('aria-label'))).toEqual([
      'View image 1 of 2 larger',
      'View image 2 of 2 larger',
    ])
    for (const button of buttons) {
      expect(button).toHaveAttribute('aria-haspopup', 'dialog')
      expect(button.className).toMatch(/\bmin-h-6\b/)
      expect(button.className).toMatch(/\bmin-w-6\b/)
    }
    expect(within(list).getByRole('img', { name: 'Image 1 of 2' })).toHaveAttribute('src', previewUrl('a'))
  })

  it('opens the image this tab sent, full size, in a modal that traps focus and closes on Esc', async () => {
    const user = userEvent.setup()
    rememberFullSize({ mediaType: 'image/png', data: btoa('the whole of a'), preview: preview('a') })
    render(
      <Panel>
        <SentImages images={[preview('a')]} />
      </Panel>,
    )
    const thumb = screen.getByRole('button', { name: 'View image 1 of 1 larger' })
    await user.click(thumb)

    const dialog = screen.getByRole('dialog', { name: 'Image 1 of 1' })
    // Modal even though it opens from the assistant panel, which a dialog otherwise sits beside.
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    expect(within(dialog).getByRole('img', { name: 'Image 1 of 1, as sent' })).toHaveAttribute(
      'src',
      `data:image/png;base64,${btoa('the whole of a')}`,
    )
    expect(within(dialog).queryByText(/only a preview/i)).not.toBeInTheDocument()

    const close = within(dialog).getByRole('button', { name: 'Close' })
    expect(close).toHaveFocus()
    expect(close.className).toMatch(/\bh-9\b/)
    // Tab stays inside.
    await user.tab()
    expect(dialog.contains(document.activeElement)).toBe(true)
    await user.tab({ shift: true })
    expect(dialog.contains(document.activeElement)).toBe(true)

    // Esc closes the image, not the panel, and focus goes back to the thumbnail.
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByRole('complementary', { name: 'Assistant' })).toBeVisible()
    expect(thumb).toHaveFocus()
  })

  it('opens from the keyboard and shows the preview enlarged when this tab has no full image', async () => {
    const user = userEvent.setup()
    render(<SentImages images={[preview('a'), preview('b')]} />)
    screen.getByRole('button', { name: 'View image 2 of 2 larger' }).focus()
    await user.keyboard('{Enter}')

    const dialog = screen.getByRole('dialog', { name: 'Image 2 of 2' })
    expect(within(dialog).getByRole('img', { name: 'Image 2 of 2, preview' })).toHaveAttribute('src', previewUrl('b'))
    expect(dialog).toHaveTextContent(/only a preview of this image is kept/i)

    await user.click(within(dialog).getByRole('button', { name: 'Close' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'View image 2 of 2 larger' })).toHaveFocus()
  })
})
