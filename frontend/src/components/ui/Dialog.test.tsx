import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState, type ReactNode } from 'react'
import { describe, expect, it } from 'vitest'
import { Dialog } from './Dialog'

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
          <Dialog open={open} title="Note" onClose={() => setOpen(false)}>
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
