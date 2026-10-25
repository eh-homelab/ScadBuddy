import { screen, waitFor, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it } from 'vitest'
import { resetMockState } from '../../mocks/handlers'
import { ITEM_APPROVED, ORDER_ACCEPTED, OUTPUT_REAGAN, state } from '../../mocks/features/orders/state'
import { renderPage } from '../../test/utils'
import { OrderPage } from '../../pages/OrderPage'

function render() {
  return renderPage(
    <Routes>
      <Route path="/orders/:id" element={<OrderPage />} />
    </Routes>,
    { route: `/orders/${ORDER_ACCEPTED}` },
  )
}

const PNG = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]
const photo = (name = 'final.png') => new File([new Uint8Array([...PNG, 0, 0, 0, 0])], name, { type: 'image/png' })
const page = () => new File(['<html><script>1</script></html>'], 'notes.html', { type: 'text/html' })
const pdf = () => new File(['%PDF-1.7\n0000'], 'drawing.pdf', { type: 'application/pdf' })

/** The one file the mock holds. */
function only() {
  const [attachment] = state.attachments
  if (!attachment || state.attachments.length !== 1) throw new Error(`expected one attachment, found ${state.attachments.length}`)
  return attachment
}

async function openItem(user: ReturnType<typeof render>['user']) {
  await user.click(within(screen.getByTestId(`order-item-${ITEM_APPROVED}`)).getByRole('button', { name: 'Name Keychain' }))
  return await screen.findByRole('dialog', { name: 'Name Keychain' })
}

describe("an order's files (#4251)", () => {
  beforeEach(() => resetMockState())

  it("shows an item's render as its picture until a photo is marked delivered", async () => {
    const { user } = render()
    await screen.findByRole('heading', { name: 'ORD-2026-2' })
    const row = screen.getByTestId(`order-item-${ITEM_APPROVED}`)
    expect(within(row).getByRole('img', { name: 'The render of Name Keychain' })).toHaveAttribute('src', `/api/v1/outputs/${OUTPUT_REAGAN}/thumbnail`)

    const drawer = await openItem(user)
    const files = within(drawer).getByRole('region', { name: 'Item files' })
    expect(within(files).getByText('No files yet.')).toBeInTheDocument()
    await user.upload(within(files).getByLabelText('Attach item files'), photo())
    const card = await within(files).findByRole('listitem', { name: 'final.png' })
    expect(within(card).getByRole('combobox', { name: 'Role of final.png' })).toHaveTextContent('Finished print')
    const added = only()
    expect(added).toMatchObject({ item_id: ITEM_APPROVED, kind: 'image', media_type: 'image/png', role: 'finished' })

    await user.click(within(card).getByRole('button', { name: 'Mark as delivered' }))
    await waitFor(() => expect(within(card).getByRole('button', { name: 'Delivered (unmark)' })).toHaveAttribute('aria-pressed', 'true'))
    expect(state.items.find((i) => i.id === ITEM_APPROVED)?.delivered_attachment_id).toBe(added.id)
    expect(within(drawer).getByRole('img', { name: 'What was delivered: Name Keychain' })).toHaveAttribute(
      'src',
      `/api/v1/orders/${ORDER_ACCEPTED}/attachments/${added.id}/thumbnail`,
    )
    expect(within(drawer).getByText('Delivered', { selector: 'figcaption' })).toBeInTheDocument()
    await user.click(within(drawer).getByRole('button', { name: 'Close' }))
    await waitFor(() =>
      expect(within(screen.getByTestId(`order-item-${ITEM_APPROVED}`)).getByRole('img', { name: 'What was delivered: Name Keychain' })).toBeInTheDocument(),
    )
  })

  it('keeps any file on the order, opening only what a browser shows', async () => {
    const { user } = render()
    await screen.findByRole('heading', { name: 'ORD-2026-2' })
    const files = screen.getByRole('region', { name: 'Order files' })
    expect(within(files).getByRole('combobox', { name: 'Add order files as' })).toHaveTextContent('Customer reference')
    await user.upload(within(files).getByLabelText('Attach order files'), [page(), pdf()])
    const html = await within(files).findByRole('listitem', { name: 'notes.html' })
    const drawing = await within(files).findByRole('listitem', { name: 'drawing.pdf' })
    expect(within(html).queryByRole('link', { name: 'Open' })).not.toBeInTheDocument()
    expect(within(html).getByRole('button', { name: 'Download notes.html' })).toBeInTheDocument()
    expect(within(drawing).getByRole('link', { name: 'Open' })).toHaveAttribute('target', '_blank')
    // Not an item's: nothing on the order is marked delivered.
    expect(within(files).queryByRole('button', { name: 'Mark as delivered' })).not.toBeInTheDocument()
    expect(state.attachments.map((a) => [a.filename, a.kind, a.item_id])).toEqual([
      ['notes.html', 'other', null],
      ['drawing.pdf', 'document', null],
    ])
  })

  it('captions, re-roles and removes a file, clearing the delivered mark', async () => {
    const { user } = render()
    await screen.findByRole('heading', { name: 'ORD-2026-2' })
    const drawer = await openItem(user)
    const files = within(drawer).getByRole('region', { name: 'Item files' })
    await user.upload(within(files).getByLabelText('Attach item files'), photo('warp.png'))
    const card = await within(files).findByRole('listitem', { name: 'warp.png' })
    await user.type(within(card).getByLabelText('Caption for warp.png'), 'corner lifted')
    await user.click(within(card).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(only().caption).toBe('corner lifted'))
    await user.click(within(card).getByRole('button', { name: 'Mark as delivered' }))
    await waitFor(() => expect(state.items.find((i) => i.id === ITEM_APPROVED)?.delivered_attachment_id).toBe(only().id))

    const named = await within(files).findByRole('listitem', { name: 'corner lifted' })
    await user.click(within(named).getByRole('button', { name: 'Remove' }))
    const confirm = await screen.findByRole('alertdialog', { name: 'Remove corner lifted?' })
    expect(confirm).toHaveTextContent('the item loses its delivered photo')
    await user.click(within(confirm).getByRole('button', { name: 'Remove' }))
    await waitFor(() => expect(state.attachments).toHaveLength(0))
    expect(state.items.find((i) => i.id === ITEM_APPROVED)?.delivered_attachment_id).toBeNull()
    expect(await within(files).findByText('No files yet.')).toBeInTheDocument()
  })
})
