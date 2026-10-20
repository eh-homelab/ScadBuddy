import { screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { renderPage } from '../../test/utils'
import { RecordDrawer } from './RecordDrawer'

describe('RecordDrawer (#4249)', () => {
  it('shows the title, the actions and the body, and closes from the footer', async () => {
    const onClose = vi.fn()
    const { user } = renderPage(
      <RecordDrawer title="ORD-2026-1" onClose={onClose} actions={<button type="button">Back</button>} footer={<button type="button">Accept</button>}>
        <p>Two items</p>
      </RecordDrawer>,
    )
    const dialog = screen.getByRole('dialog', { name: 'ORD-2026-1' })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    expect(screen.getByRole('button', { name: 'Back' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Accept' })).toBeInTheDocument()
    expect(screen.getByText('Two items')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Close' }))
    expect(onClose).toHaveBeenCalled()
  })

  it('closes on Escape', async () => {
    const onClose = vi.fn()
    const { user } = renderPage(
      <RecordDrawer title="Ada" onClose={onClose}>
        <p>Customer</p>
      </RecordDrawer>,
    )
    await user.keyboard('{Escape}')
    expect(onClose).toHaveBeenCalled()
  })
})
