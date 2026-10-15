import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState, type ReactNode } from 'react'
import { describe, expect, it } from 'vitest'
import { Dialog } from '.'

/** A page with a button that opens the dialog, and a control after it. */
function Page({ children, description }: { children?: ReactNode; description?: string }) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Open it
      </button>
      <button type="button">Behind</button>
      <Dialog
        open={open}
        title="Name it"
        description={description}
        onClose={() => setOpen(false)}
        footer={
          <>
            <button type="button" onClick={() => setOpen(false)}>
              Cancel
            </button>
            <button type="button" onClick={() => setOpen(false)}>
              Save
            </button>
          </>
        }
      >
        {children ?? <input aria-label="Name" />}
      </Dialog>
    </>
  )
}

describe('Dialog focus (#351)', () => {
  it('focuses its first field on opening, so typing lands in it', async () => {
    const user = userEvent.setup()
    render(<Page />)
    await user.click(screen.getByRole('button', { name: 'Open it' }))

    const name = screen.getByRole('textbox', { name: 'Name' })
    expect(name).toHaveFocus()
    await user.keyboard('Emma')
    expect(name).toHaveValue('Emma')
  })

  it('keeps an autoFocus field focused rather than taking it back', async () => {
    const user = userEvent.setup()
    render(
      <Page>
        <button type="button">First</button>
        <input aria-label="Name" autoFocus />
      </Page>,
    )
    await user.click(screen.getByRole('button', { name: 'Open it' }))
    expect(screen.getByRole('textbox', { name: 'Name' })).toHaveFocus()
  })

  it('focuses the panel when it has nothing focusable', async () => {
    const user = userEvent.setup()
    function Bare() {
      const [open, setOpen] = useState(false)
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>
            Open it
          </button>
          {/* A footer of its own, with nothing to press: without one it would get Close (#2386). */}
          <Dialog open={open} title="Note" onClose={() => setOpen(false)} footer={<span>Read only</span>}>
            <p>Nothing to press.</p>
          </Dialog>
        </>
      )
    }
    render(<Bare />)
    await user.click(screen.getByRole('button', { name: 'Open it' }))
    expect(screen.getByRole('dialog', { name: 'Note' })).toHaveFocus()
  })

  it('traps Tab and Shift+Tab inside', async () => {
    const user = userEvent.setup()
    render(<Page />)
    await user.click(screen.getByRole('button', { name: 'Open it' }))
    const name = screen.getByRole('textbox', { name: 'Name' })
    const save = screen.getByRole('button', { name: 'Save' })

    await user.tab()
    expect(screen.getByRole('button', { name: 'Cancel' })).toHaveFocus()
    await user.tab()
    expect(save).toHaveFocus()
    await user.tab()
    expect(name).toHaveFocus()
    await user.tab({ shift: true })
    expect(save).toHaveFocus()
  })

  it.each([
    ['Escape', async (user: ReturnType<typeof userEvent.setup>) => user.keyboard('{Escape}')],
    ['Cancel', async (user: ReturnType<typeof userEvent.setup>) => user.click(screen.getByRole('button', { name: 'Cancel' }))],
    ['Save', async (user: ReturnType<typeof userEvent.setup>) => user.click(screen.getByRole('button', { name: 'Save' }))],
  ])('returns focus to what opened it after %s', async (_, close) => {
    const user = userEvent.setup()
    render(<Page />)
    const opener = screen.getByRole('button', { name: 'Open it' })
    await user.click(opener)
    await close(user)

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(opener).toHaveFocus()
  })

  it('ties its description to it', async () => {
    const user = userEvent.setup()
    render(<Page description="Shown in the preset list." />)
    await user.click(screen.getByRole('button', { name: 'Open it' }))
    expect(screen.getByRole('dialog', { name: 'Name it' })).toHaveAccessibleDescription('Shown in the preset list.')
  })
})

describe('Dialog placement', () => {
  it('right anchors a full-height drawer and keeps the dialog role', () => {
    render(
      <Dialog open title="Side" onClose={() => {}} placement="right">
        <p>Body</p>
      </Dialog>,
    )
    const panel = screen.getByRole('dialog', { name: 'Side' })
    expect(panel.className).toContain('h-full')
    expect(panel.className).toContain('max-w-[480px]')
    expect(panel.parentElement?.className).toContain('justify-end')
  })
})

// #2386 — a dialog with no actions of its own (Colours, the font picker, Media) still has a
// labelled way out that is not Esc or the backdrop, and it does not take the first focus.
describe('Dialog without a footer', () => {
  function Bare() {
    const [open, setOpen] = useState(true)
    return (
      <Dialog open={open} title="Choose a font" onClose={() => setOpen(false)}>
        <input aria-label="Search fonts" />
      </Dialog>
    )
  }

  it('has a Close button after its content, which closes it', async () => {
    const user = userEvent.setup()
    render(<Bare />)
    const dialog = screen.getByRole('dialog', { name: 'Choose a font' })
    expect(screen.getByLabelText('Search fonts')).toHaveFocus()
    await user.click(screen.getByRole('button', { name: 'Close' }))
    expect(dialog).not.toBeInTheDocument()
  })

  it('shows only the caller’s footer when it has one', async () => {
    const user = userEvent.setup()
    render(<Page />)
    await user.click(screen.getByRole('button', { name: 'Open it' }))
    expect(screen.queryByRole('button', { name: 'Close' })).toBeNull()
  })
})
