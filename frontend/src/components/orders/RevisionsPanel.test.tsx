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

  it('sets the approval in any open order, as the backend allows', async () => {
    const { user } = render(ORDER_ACCEPTED, ITEM_APPROVED)
    const select = await screen.findByRole('combobox', { name: 'Approval' })
    expect(select).toBeEnabled()
    const other = state.items.find((i) => i.id === ITEM_APPROVED)!.approval === 'design_first' ? 'Design and price together' : 'Design first'
    await choose(user, select, other)
    await waitFor(() => expect(state.items.find((i) => i.id === ITEM_APPROVED)!.approval).toBe(other === 'Design first' ? 'design_first' : 'together'))
  })

  it('revises an accepted item after a confirm, and keeps its accepted price (#5350)', async () => {
    const { user } = render(ORDER_ACCEPTED, ITEM_APPROVED)
    await user.click(await screen.findByRole('button', { name: 'New revision' }))
    await user.type(screen.getByRole('textbox', { name: 'Revision note' }), 'a bigger loop')
    await user.click(screen.getByRole('button', { name: 'Add revision (same design)' }))
    // The first click only asks.
    expect(screen.getByRole('alert', { name: 'Confirm the change' })).toHaveTextContent(
      /^ORD-2026-2 is accepted\. A new design changes what the customer gets\..*Until then the accepted price stays\.$/,
    )
    expect(revisionsOf(ITEM_APPROVED)).toHaveLength(1)
    await user.click(screen.getByRole('button', { name: 'Change the accepted item' }))
    await waitFor(() => expect(revisionsOf(ITEM_APPROVED)).toHaveLength(2))
    expect(revisionsOf(ITEM_APPROVED)[1]).toMatchObject({ number: 2, note: 'a bigger loop' })
    const item = state.items.find((i) => i.id === ITEM_APPROVED)!
    expect(item.revised_after_acceptance).toBe(true)
    expect(item.status).toBe('draft')
    // The accepted price stands until a quote of the new design is accepted.
    expect(state.quotes.find((q) => q.id === QUOTE_ACCEPTED)!.accepted).toBe(true)
    expect(await screen.findByTestId('revised-after-acceptance')).toHaveTextContent(/Until then the accepted price stays/)
    expect(state.events.filter((e) => e.kind === 'revision.added_after_acceptance')).toHaveLength(1)
  })

  it('asks again after a failed revision of an accepted item (#5350)', async () => {
    const { user } = render(ORDER_ACCEPTED, ITEM_APPROVED)
    await user.click(await screen.findByRole('button', { name: 'New revision' }))
    await user.click(screen.getByRole('button', { name: 'Add revision (same design)' }))
    // Held behind the page's back: the save is refused.
    state.orders.find((o) => o.id === ORDER_ACCEPTED)!.on_hold = true
    await user.click(screen.getByRole('button', { name: 'Change the accepted item' }))
    await waitFor(() => expect(screen.getAllByRole('alert').map((a) => a.textContent).join('|')).toMatch(/on hold/))
    expect(screen.queryByRole('alert', { name: 'Confirm the change' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Add revision (same design)' })).toBeInTheDocument()
    expect(revisionsOf(ITEM_APPROVED)).toHaveLength(1)
  })

  it('offers no revision of an accepted item on hold (#5350)', async () => {
    state.orders.find((o) => o.id === ORDER_ACCEPTED)!.on_hold = true
    render(ORDER_ACCEPTED, ITEM_APPROVED)
    await screen.findByRole('combobox', { name: 'Approval' })
    expect(screen.queryByRole('button', { name: 'New revision' })).toBeNull()
  })

  it('offers no revision once an attempt is accepted, and says what to do (#5592)', async () => {
    state.items.find((i) => i.id === ITEM_APPROVED)!.quantity = 3
    const attempt = await api.recordFulfilment(ORDER_ACCEPTED, ITEM_APPROVED, { quantity: 1, outcome: 'succeeded' })
    await api.acceptFulfilment(ORDER_ACCEPTED, ITEM_APPROVED, attempt.id)
    render(ORDER_ACCEPTED, ITEM_APPROVED)
    const panel = await screen.findByTestId('revisions-panel')
    expect(await within(panel).findByTestId('partly-fulfilled')).toHaveTextContent('cancel the item and add one with the new design')
    expect(within(panel).queryByRole('button', { name: 'New revision' })).toBeNull()
  })

  it.each([
    ['on hold', () => { state.orders.find((o) => o.id === ORDER_ACCEPTED)!.on_hold = true }],
    ['printing', () => { state.items.find((i) => i.id === ITEM_APPROVED)!.status = 'printing' }],
    ['queued', () => { state.items.find((i) => i.id === ITEM_APPROVED)!.status = 'queued' }],
  ])('gives no partly-fulfilled hint while the item is %s: that is the reason (#5598)', async (_, block) => {
    state.items.find((i) => i.id === ITEM_APPROVED)!.quantity = 3
    const attempt = await api.recordFulfilment(ORDER_ACCEPTED, ITEM_APPROVED, { quantity: 1, outcome: 'succeeded' })
    await api.acceptFulfilment(ORDER_ACCEPTED, ITEM_APPROVED, attempt.id)
    block()
    render(ORDER_ACCEPTED, ITEM_APPROVED)
    const panel = await screen.findByTestId('revisions-panel')
    await within(panel).findByRole('combobox', { name: 'Approval' })
    expect(within(panel).queryByRole('button', { name: 'New revision' })).toBeNull()
    expect(within(panel).queryByTestId('partly-fulfilled')).toBeNull()
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

  it('shows a sent revision\'s files read-only, and an unsent one\'s as editable', async () => {
    const second = twoRevisions()
    const [first] = revisionsOf(ITEM_DRAFT)
    await api.addRevisionAttachment(ORDER_DRAFT, ITEM_DRAFT, first!.id, photo('seen.png'), { role: 'render' })
    await api.addRevisionAttachment(ORDER_DRAFT, ITEM_DRAFT, second.id, photo('draft.png'), { role: 'render' })
    render(ORDER_DRAFT, ITEM_DRAFT)
    const seen = within(await screen.findByRole('group', { name: 'Files of revision 1' }))
    expect(seen.getByText('seen.png', { exact: false })).toBeInTheDocument()
    expect(seen.queryByRole('button', { name: 'Remove' })).toBeNull()
    expect(seen.queryByRole('combobox', { name: 'Role of seen.png' })).toBeNull()
    expect(seen.queryByRole('textbox', { name: 'Caption for seen.png' })).toBeNull()
    const draft = within(screen.getByRole('group', { name: 'Files of revision 2' }))
    expect(draft.getByRole('button', { name: 'Remove' })).toBeInTheDocument()
    expect(draft.getByRole('textbox', { name: 'Caption for draft.png' })).toBeInTheDocument()
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
