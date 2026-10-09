import { fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState, type KeyboardEvent, type ReactNode } from 'react'
import { describe, expect, it } from 'vitest'
import { blobUrl, type SentImage } from '../../agent/chat/protocol'
import { ModalCompanionContext } from '../../lib/modal'
import { SentImages } from './SentImages'

// #1891 — a sent image, opened large from the transcript.

const preview = (name: string): SentImage => ({ mediaType: 'image/jpeg', data: btoa(`preview of ${name}`) })
/** A preview whose full image the agent stored under a name. */
const stored = (name: string, hex: string): SentImage => ({ ...preview(name), name: `${hex.repeat(64)}.png` })
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

  it('loads the stored image, showing the preview until it has, in a modal that traps focus and closes on Esc', async () => {
    const user = userEvent.setup()
    const image = stored('a', 'a')
    render(
      <Panel>
        <SentImages images={[image]} sessionId="sess-1" />
      </Panel>,
    )
    const thumb = screen.getByRole('button', { name: 'View image 1 of 1 larger' })
    await user.click(thumb)

    const dialog = screen.getByRole('dialog', { name: 'Image 1 of 1' })
    // Modal even though it opens from the assistant panel, which a dialog otherwise sits beside.
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    // While the full image loads, its preview stands in for it.
    expect(within(dialog).getByRole('img', { name: 'Image 1 of 1, preview' })).toHaveAttribute('src', previewUrl('a'))
    const full = within(dialog).getByAltText('Image 1 of 1, as sent')
    expect(full).toHaveAttribute('src', blobUrl('sess-1', `${'a'.repeat(64)}.png`))
    expect(full).not.toBeVisible()
    fireEvent.load(full)
    expect(within(dialog).getByRole('img', { name: 'Image 1 of 1, as sent' })).toBeVisible()
    expect(within(dialog).queryByRole('img', { name: 'Image 1 of 1, preview' })).not.toBeInTheDocument()
    expect(within(dialog).queryByText(/preview/i)).not.toBeInTheDocument()

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

  it('falls back to the preview, saying so, when the stored image cannot be loaded', async () => {
    const user = userEvent.setup()
    render(<SentImages images={[stored('a', 'a'), stored('b', 'b')]} sessionId="sess-gone" />)
    await user.click(screen.getByRole('button', { name: 'View image 2 of 2 larger' }))

    const dialog = screen.getByRole('dialog', { name: 'Image 2 of 2' })
    fireEvent.error(within(dialog).getByAltText('Image 2 of 2, as sent'))
    expect(within(dialog).queryByAltText('Image 2 of 2, as sent')).not.toBeInTheDocument()
    expect(within(dialog).getByRole('img', { name: 'Image 2 of 2, preview' })).toHaveAttribute('src', previewUrl('b'))
    expect(dialog).toHaveTextContent(/the full image could not be loaded/i)
    expect(within(dialog).getByRole('button', { name: 'Close' })).toHaveFocus()

    // Opening another image tries its own full image afresh.
    await user.keyboard('{Escape}')
    await user.click(screen.getByRole('button', { name: 'View image 1 of 2 larger' }))
    const again = screen.getByRole('dialog', { name: 'Image 1 of 2' })
    expect(within(again).getByAltText('Image 1 of 2, as sent')).toHaveAttribute('src', blobUrl('sess-gone', `${'a'.repeat(64)}.png`))
    expect(again).not.toHaveTextContent(/could not be loaded/i)
  })

  it('opens from the keyboard and shows the preview enlarged for a turn that names no stored image', async () => {
    const user = userEvent.setup()
    render(<SentImages images={[preview('a'), preview('b')]} />)
    screen.getByRole('button', { name: 'View image 2 of 2 larger' }).focus()
    await user.keyboard('{Enter}')

    const dialog = screen.getByRole('dialog', { name: 'Image 2 of 2' })
    expect(within(dialog).getByRole('img', { name: 'Image 2 of 2, preview' })).toHaveAttribute('src', previewUrl('b'))
    expect(dialog).toHaveTextContent(/only a preview of this image is kept/i)
    expect(within(dialog).queryByAltText(/as sent/)).not.toBeInTheDocument()

    await user.click(within(dialog).getByRole('button', { name: 'Close' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'View image 2 of 2 larger' })).toHaveFocus()
  })
})
