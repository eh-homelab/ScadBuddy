import { screen, waitFor, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { resetMockState } from '../../mocks/handlers'
import { ITEM_APPROVED, ITEM_DRAFT, QUOTE_ACCEPTED, ORDER_ACCEPTED, ORDER_DRAFT, revisionsOf, state } from '../../mocks/features/orders/state'
import { OrderPage } from '../../pages/OrderPage'
import { choose } from '../../test/select'
import { renderPage } from '../../test/utils'

vi.mock('../print/PlateScene', () => ({ PlateScene: () => <div data-testid="plate-scene" /> }))

function render(orderId: string, itemId: string) {
  return renderPage(
    <Routes>
      <Route path="/orders/:id" element={<OrderPage />} />
    </Routes>,
    { route: `/orders/${orderId}?item=${itemId}` },
  )
}

const PNG = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]
const photo = (name = 'photo.png') => new File([new Uint8Array([...PNG, 0, 0, 0, 0])], name, { type: 'image/png' })

/** A second revision on the draft item, the first sent and superseded. */
function twoRevisions(note = 'bigger please') {
  const [first] = revisionsOf(ITEM_DRAFT)
  first!.sent_at = '2026-10-19T10:00:00Z'
  first!.answer = 'superseded'
  const second = { ...first!, id: 'rev-two', number: 2, note, sent_at: null, answer: null }
  state.revisions.push(second)
  state.items.find((i) => i.id === ITEM_DRAFT)!.current_revision_id = second.id
  return second
}

describe('RevisionsPanel (#5280)', () => {
  beforeEach(() => resetMockState())
  afterEach(() => vi.restoreAllMocks())

  it('lists revisions newest first with their answers', async () => {
    twoRevisions()
    render(ORDER_DRAFT, ITEM_DRAFT)
    const panel = await screen.findByTestId('revisions-panel')
    const rows = within(panel).getAllByTestId('revision')
    expect(rows.map((r) => r.dataset.number)).toEqual(['2', '1'])
    expect(within(rows[1]!).getByText('Superseded')).toBeInTheDocument()
    expect(within(rows[0]!).getByText('Not sent')).toBeInTheDocument()
  })

  it('shows a note as text', async () => {
    twoRevisions('<b>bold</b>')
    render(ORDER_DRAFT, ITEM_DRAFT)
    expect(await screen.findByText('<b>bold</b>')).toBeInTheDocument()
    expect(document.querySelector('b')).toBeNull()
  })

  it('refuses to send without an attachment and says why', async () => {
    const { user } = render(ORDER_DRAFT, ITEM_DRAFT)
    await user.click(await screen.findByRole('button', { name: 'Send to customer' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/attachment/i)
    expect(revisionsOf(ITEM_DRAFT)[0]!.sent_at).toBeNull()
  })

  it('uploads a render and sends the revision', async () => {
    const { user } = render(ORDER_DRAFT, ITEM_DRAFT)
    await user.upload(await screen.findByLabelText('Add render or photo'), photo('render.png'))
    const row = await screen.findByTestId('revision')
    expect(await within(row).findByRole('listitem', { name: 'render.png' })).toBeInTheDocument()
    expect(state.attachments[0]).toMatchObject({ role: 'render', revision_id: revisionsOf(ITEM_DRAFT)[0]!.id })
    await api.quoteOrderItem(ORDER_DRAFT, ITEM_DRAFT, { unit_price_minor: 1000 })
    await user.click(within(row).getByRole('button', { name: 'Send to customer' }))
    await waitFor(() => expect(revisionsOf(ITEM_DRAFT)[0]!.sent_at).not.toBeNull())
    expect(await within(row).findByText(/^Sent /)).toBeInTheDocument()
  })

  it('records the customer asking for changes', async () => {
    revisionsOf(ITEM_DRAFT)[0]!.sent_at = '2026-10-19T10:00:00Z'
    const { user } = render(ORDER_DRAFT, ITEM_DRAFT)
    await user.click(await screen.findByRole('button', { name: 'Record answer' }))
    await choose(user, screen.getByRole('combobox', { name: "The customer's answer" }), 'Changes requested')
    await user.click(screen.getByRole('button', { name: "Record the customer's answer" }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/what the customer wants changed/i)
    await user.type(screen.getByRole('textbox', { name: 'What should change' }), 'bigger')
    await user.click(screen.getByRole('button', { name: "Record the customer's answer" }))
    expect(await screen.findByText('Changes requested')).toBeInTheDocument()
    expect(revisionsOf(ITEM_DRAFT)[0]).toMatchObject({ answer: 'changes_requested', answer_note: 'bigger' })
    expect(await screen.findByText('Customer: bigger')).toBeInTheDocument()
  })

  it('adds a revision of the same design with a note', async () => {
    const { user } = render(ORDER_DRAFT, ITEM_DRAFT)
    await user.click(await screen.findByRole('button', { name: 'New revision' }))
    await user.type(screen.getByRole('textbox', { name: 'Revision note' }), 'thinner')
    await user.click(screen.getByRole('button', { name: 'Add revision (same design)' }))
    await waitFor(() => expect(revisionsOf(ITEM_DRAFT)).toHaveLength(2))
    expect(revisionsOf(ITEM_DRAFT)[1]).toMatchObject({ number: 2, note: 'thinner' })
    expect((await screen.findAllByTestId('revision')).map((r) => r.dataset.number)).toEqual(['2', '1'])
  })

  it('sets the approval while the order is open, and not after', async () => {
    const draft = render(ORDER_DRAFT, ITEM_DRAFT)
    const select = await screen.findByRole('combobox', { name: 'Approval' })
    expect(select).toHaveTextContent('Design and price together')
    await choose(draft.user, select, 'Design first')
    await waitFor(() => expect(state.items.find((i) => i.id === ITEM_DRAFT)!.approval).toBe('design_first'))
  })

  it('sets the approval in any open order, as the backend allows; revising stays locked', async () => {
    const { user } = render(ORDER_ACCEPTED, ITEM_APPROVED)
    const select = await screen.findByRole('combobox', { name: 'Approval' })
    expect(select).toBeEnabled()
    expect(screen.queryByRole('button', { name: 'New revision' })).toBeNull()
    const other = state.items.find((i) => i.id === ITEM_APPROVED)!.approval === 'design_first' ? 'Design and price together' : 'Design first'
    await choose(user, select, other)
    await waitFor(() => expect(state.items.find((i) => i.id === ITEM_APPROVED)!.approval).toBe(other === 'Design first' ? 'design_first' : 'together'))
  })

  it('locks the approval for a settled item', async () => {
    state.items.find((i) => i.id === ITEM_APPROVED)!.status = 'fulfilled'
    render(ORDER_ACCEPTED, ITEM_APPROVED)
    expect(await screen.findByRole('combobox', { name: 'Approval' })).toBeDisabled()
  })

  it('has no revisions panel for a custom item', async () => {
    const custom = await api.addOrderItem(ORDER_DRAFT, { kind: 'custom', title: 'Rush fee', quantity: 1, unit_price_minor: 500 })
    render(ORDER_DRAFT, custom.id)
    await screen.findByRole('dialog', { name: 'Rush fee' })
    expect(screen.queryByTestId('revisions-panel')).toBeNull()
  })

  it('offers add to model photos only on an accepted revision', async () => {
    const first = revisionsOf(ITEM_DRAFT)[0]!
    await api.addRevisionAttachment(ORDER_DRAFT, ITEM_DRAFT, first.id, photo('printed.png'), { role: 'test_print' })
    const { user } = render(ORDER_DRAFT, ITEM_DRAFT)
    await screen.findByRole('listitem', { name: 'printed.png' })
    expect(screen.queryByRole('button', { name: 'Add to model photos' })).toBeNull()
    first.answer = 'accepted'
    first.sent_at = '2026-10-19T10:00:00Z'
    // The panel reads the order again on any change.
    await user.click(screen.getByRole('button', { name: 'Close' }))
    render(ORDER_DRAFT, ITEM_DRAFT)
    const buttons = await screen.findAllByRole('button', { name: 'Add to model photos' })
    await user.click(buttons[0]!)
    expect(await screen.findByText("Added to the template's photos.")).toBeInTheDocument()
  })

  it('keeps a test print out of the fulfilment attempts and lists it on its revision', async () => {
    const first = revisionsOf(ITEM_DRAFT)[0]!
    state.fulfilments.push({
      ...state.fulfilments[0]!,
      id: 'ft',
      order_item_id: ITEM_DRAFT,
      attempt: 1,
      quantity: 0,
      outcome: 'succeeded',
      purpose: 'test',
      revision_id: first.id,
    })
    render(ORDER_DRAFT, ITEM_DRAFT)
    expect(await screen.findByText('Test print, attempt 1: succeeded')).toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Attempts' })).toBeNull()
  })

  it('test prints the newest revision from its slice quote, before the order is accepted', async () => {
    const first = revisionsOf(ITEM_DRAFT)[0]!
    expect(screen.queryByTestId('test-print-item')).toBeNull()
    state.quotes.push({ ...state.quotes.find((q) => q.id === QUOTE_ACCEPTED)!, id: 'q-slice', order_item_id: ITEM_DRAFT, revision_id: first.id, source: 'slice', accepted: false })
    const { user } = render(ORDER_DRAFT, ITEM_DRAFT)
    await user.click(await screen.findByRole('button', { name: 'Test print' }))
    const dialog = await screen.findByRole('dialog', { name: 'Print the line item' })
    const run = within(dialog).getByTestId('run-item-print')
    await waitFor(() => expect(run).toBeEnabled())
    await user.click(run)
    await waitFor(() => expect(state.fulfilments.some((f) => f.order_item_id === ITEM_DRAFT && f.purpose === 'test')).toBe(true))
    expect(await screen.findByText(/^Test print, attempt 1: /)).toBeInTheDocument()
    expect(state.items.find((i) => i.id === ITEM_DRAFT)!.status).toBe('draft')
  })

  it('offers no delivered mark on a revision image, and no resend once sent', async () => {
    const first = revisionsOf(ITEM_DRAFT)[0]!
    await api.addRevisionAttachment(ORDER_DRAFT, ITEM_DRAFT, first.id, photo('render.png'), { role: 'render' })
    first.sent_at = '2026-10-19T10:00:00Z'
    render(ORDER_DRAFT, ITEM_DRAFT)
    await screen.findByRole('listitem', { name: 'render.png' })
    expect(screen.queryByRole('button', { name: /delivered/i })).toBeNull()
    expect(screen.getByRole('button', { name: 'Send to customer' })).toBeDisabled()
  })
})
