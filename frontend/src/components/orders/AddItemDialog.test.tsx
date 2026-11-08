import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { BOOKMARK_SLUG, DOLLHOUSE_SLUG, MAZE_SLUG, outputs as fixtureOutputs } from '../../mocks/fixtures'
import { addMockOutput, resetMockState } from '../../mocks/handlers'
import { ORDER_ACCEPTED, ORDER_DRAFT } from '../../mocks/features/orders/state'
import { choose } from '../../test/select'
import { renderPage } from '../../test/utils'
import { AddItemDialog } from './AddItemDialog'

const NOVA = 'c'.repeat(32)
const REAGAN = 'a'.repeat(32)
const WORKSHOP = 'e'.repeat(32)

describe('AddItemDialog (#4251)', () => {
  beforeEach(() => resetMockState())
  // Here, not at the end of a test: a failed assertion must not leak a spy into the next.
  afterEach(() => vi.restoreAllMocks())

  it("adds a template's current version with its default settings, pinned to its full commit", async () => {
    const add = vi.spyOn(api, 'addOrderItem')
    const onAdded = vi.fn()
    const onClose = vi.fn()
    const { user } = renderPage(<AddItemDialog open orderId={ORDER_DRAFT} onAdded={onAdded} onClose={onClose} />)
    const dialog = screen.getByRole('dialog', { name: 'Add item' })
    await user.type(within(dialog).getByLabelText('Template'), 'maze')
    await user.click(await within(dialog).findByRole('button', { name: /Ball Maze/ }))
    const current = await within(dialog).findByRole('radio', { name: 'Current version, default settings' })
    await waitFor(() => expect(current).toBeChecked())
    await user.clear(within(dialog).getByLabelText('Quantity'))
    await user.type(within(dialog).getByLabelText('Quantity'), '2')
    await user.type(within(dialog).getByLabelText('Title (optional)'), 'Maze for Ada')
    await user.click(within(dialog).getByRole('button', { name: 'Add item' }))
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(onAdded).toHaveBeenCalled()
    const model = await api.getModel(MAZE_SLUG)
    expect(model.version).toMatch(/^[0-9a-f]{40}$/)
    expect(add).toHaveBeenCalledWith(ORDER_DRAFT, {
      slug: MAZE_SLUG,
      commit: model.version,
      inputs: { params: {}, v: 0 },
      quantity: 2,
      title: 'Maze for Ada',
    })
    const order = await api.order(ORDER_DRAFT)
    const added = order.items.find((i) => i.title === 'Maze for Ada')
    expect(added?.reference?.commit).toBe(model.version)
    expect(added?.quantity).toBe(2)
  })

  it('adds a library file found by the library search (#5544)', async () => {
    const add = vi.spyOn(api, 'addOrderItem')
    const search = vi.spyOn(api, 'searchLibrary')
    const onClose = vi.fn()
    const { user } = renderPage(<AddItemDialog open orderId={ORDER_DRAFT} onAdded={() => {}} onClose={onClose} />)
    const dialog = screen.getByRole('dialog', { name: 'Add item' })
    await user.click(within(dialog).getByRole('button', { name: 'Library file' }))
    expect(within(dialog).getByRole('button', { name: 'Add item' })).toBeDisabled()
    await user.type(within(dialog).getByLabelText('Library file'), 'drawer')
    const results = await within(dialog).findByRole('list', { name: 'Library files' })
    expect(search).toHaveBeenLastCalledWith({ q: 'drawer', all: false }, expect.any(AbortSignal))
    await user.click(within(results).getByRole('button', { name: /drawer-label\.3mf/ }))
    expect(within(dialog).getByTestId('add-item-library-file')).toHaveTextContent('drawer-label.3mf')
    await user.clear(within(dialog).getByLabelText('Quantity'))
    await user.type(within(dialog).getByLabelText('Quantity'), '3')
    await user.click(within(dialog).getByRole('button', { name: 'Add item' }))
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(add).toHaveBeenCalledWith(ORDER_DRAFT, { library_file_id: 120, quantity: 3 })
    const order = await api.order(ORDER_DRAFT)
    expect(order.items.find((i) => i.kind === 'library_file')?.library_file).toEqual({ id: 120, filename: 'drawer-label.3mf', library: 'bambuddy' })
  })

  it("does not send a design request's title as a library file's (#5544)", async () => {
    const add = vi.spyOn(api, 'addOrderItem')
    const onClose = vi.fn()
    const { user } = renderPage(<AddItemDialog open orderId={ORDER_DRAFT} onAdded={() => {}} onClose={onClose} />)
    const dialog = screen.getByRole('dialog', { name: 'Add item' })
    await user.click(within(dialog).getByRole('button', { name: 'Design request' }))
    await user.type(within(dialog).getByLabelText('Title'), 'A lamp shade')
    await user.click(within(dialog).getByRole('button', { name: 'Library file' }))
    await user.type(within(dialog).getByLabelText('Library file'), 'drawer')
    await user.click(await within(dialog).findByRole('button', { name: /drawer-label\.3mf/ }))
    expect(within(dialog).getByLabelText('Title (optional)')).toHaveValue('')
    await user.click(within(dialog).getByRole('button', { name: 'Add item' }))
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(add).toHaveBeenCalledWith(ORDER_DRAFT, { library_file_id: 120, quantity: 1 })
  })

  it('asks before it adds a library file to an accepted order (#5544)', async () => {
    const add = vi.spyOn(api, 'addOrderItemAfterAcceptance')
    const onClose = vi.fn()
    const { user } = renderPage(
      <AddItemDialog open orderId={ORDER_ACCEPTED} mode="after_acceptance" order={{ number: 'ORD-2026-2', status: 'accepted' }} onAdded={() => {}} onClose={onClose} />,
    )
    const dialog = screen.getByRole('dialog', { name: 'Add item' })
    await user.click(within(dialog).getByRole('button', { name: 'Library file' }))
    await user.type(within(dialog).getByLabelText('Library file'), 'drawer')
    await user.click(await within(dialog).findByRole('button', { name: /drawer-label\.3mf/ }))
    await user.type(within(dialog).getByLabelText('Title (optional)'), 'Label')
    await user.click(within(dialog).getByRole('button', { name: 'Add item' }))
    expect(within(dialog).getByRole('alert', { name: 'Confirm the change' })).toBeInTheDocument()
    expect(add).not.toHaveBeenCalled()
    await user.click(within(dialog).getByRole('button', { name: 'Change the accepted order' }))
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(add).toHaveBeenCalledWith(ORDER_ACCEPTED, { library_file_id: 120, quantity: 1, title: 'Label' })
  })

  it('adds one of the template\'s saved outputs by its id', async () => {
    const add = vi.spyOn(api, 'addOrderItem')
    const onClose = vi.fn()
    const { user } = renderPage(<AddItemDialog open orderId={ORDER_DRAFT} onAdded={() => {}} onClose={onClose} />)
    const dialog = screen.getByRole('dialog', { name: 'Add item' })
    await user.type(within(dialog).getByLabelText('Template'), 'keychain')
    await user.click(await within(dialog).findByRole('button', { name: /Name Keychain/ }))
    // This template has no revision to pin in the mock: its newest output is chosen.
    expect(await within(dialog).findByRole('radio', { name: 'Reagan' })).toBeChecked()
    expect(within(dialog).getByRole('radio', { name: 'Current version, default settings' })).toBeDisabled()
    await user.click(within(dialog).getByRole('radio', { name: 'Nova' }))
    await user.click(within(dialog).getByRole('button', { name: 'Add item' }))
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(add).toHaveBeenCalledWith(ORDER_DRAFT, { output_id: NOVA, quantity: 1 })
  })

  it('keeps the estimate\'s filament when another output is picked, and estimates that one', async () => {
    const priceBook = vi.spyOn(api, 'priceBook')
    const estimate = vi.spyOn(api, 'orderEstimate')
    const { user } = renderPage(<AddItemDialog open orderId={ORDER_DRAFT} onAdded={() => {}} onClose={() => {}} />)
    const dialog = screen.getByRole('dialog', { name: 'Add item' })
    await user.type(within(dialog).getByLabelText('Template'), 'keychain')
    await user.click(await within(dialog).findByRole('button', { name: /Name Keychain/ }))
    expect(await within(dialog).findByRole('radio', { name: 'Reagan' })).toBeChecked()
    const filament = await within(dialog).findByRole('combobox', { name: 'Estimate with' })
    await choose(user, filament, /Sunflower Yellow/)
    await waitFor(() => expect(within(dialog).getByTestId('order-estimate')).toBeInTheDocument())
    await user.click(within(dialog).getByRole('radio', { name: 'Nova' }))
    await waitFor(() => expect(estimate).toHaveBeenLastCalledWith(NOVA, expect.any(String)))
    expect(within(dialog).getByRole('combobox', { name: 'Estimate with' })).toHaveTextContent(/Sunflower Yellow/)
    expect(priceBook).toHaveBeenCalledTimes(1)
  })

  it("shows the order's refusal and stays open", async () => {
    const onClose = vi.fn()
    const { user } = renderPage(<AddItemDialog open orderId={ORDER_ACCEPTED} onAdded={() => {}} onClose={onClose} />)
    const dialog = screen.getByRole('dialog', { name: 'Add item' })
    await user.type(within(dialog).getByLabelText('Template'), 'maze')
    await user.click(await within(dialog).findByRole('button', { name: /Ball Maze/ }))
    await waitFor(() => expect(within(dialog).getByRole('radio', { name: 'Current version, default settings' })).toBeChecked())
    await user.click(within(dialog).getByRole('button', { name: 'Add item' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(/pinned once accepted/)
    expect(onClose).not.toHaveBeenCalled()
    expect(within(dialog).getByRole('button', { name: 'Add item' })).toBeEnabled()
  })

  it('asks before it changes an accepted order, then adds on the after-acceptance route (#5349)', async () => {
    const plain = vi.spyOn(api, 'addOrderItem')
    const after = vi.spyOn(api, 'addOrderItemAfterAcceptance')
    const onAdded = vi.fn()
    const onClose = vi.fn()
    const { user } = renderPage(
      <AddItemDialog open orderId={ORDER_ACCEPTED} mode="after_acceptance" order={{ number: 'ORD-2026-2', status: 'accepted' }} onAdded={onAdded} onClose={onClose} />,
    )
    const dialog = screen.getByRole('dialog', { name: 'Add item' })
    await user.type(within(dialog).getByLabelText('Template'), 'maze')
    await user.click(await within(dialog).findByRole('button', { name: /Ball Maze/ }))
    await waitFor(() => expect(within(dialog).getByRole('radio', { name: 'Current version, default settings' })).toBeChecked())
    await user.click(within(dialog).getByRole('button', { name: 'Add item' }))
    // The first click only asks.
    const warning = within(dialog).getByRole('alert', { name: 'Confirm the change' })
    expect(warning).toHaveTextContent(/ORD-2026-2 is accepted\. Adding an item changes what the customer gets/)
    expect(after).not.toHaveBeenCalled()
    // Back leaves the order alone.
    await user.click(within(dialog).getByRole('button', { name: 'Back' }))
    expect(within(dialog).queryByRole('alert', { name: 'Confirm the change' })).not.toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Add item' }))
    await user.click(within(dialog).getByRole('button', { name: 'Change the accepted order' }))
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(onAdded).toHaveBeenCalled()
    expect(plain).not.toHaveBeenCalled()
    const model = await api.getModel(MAZE_SLUG)
    expect(after).toHaveBeenCalledWith(ORDER_ACCEPTED, { slug: MAZE_SLUG, commit: model.version, inputs: { params: {}, v: 0 }, quantity: 1 })
    const order = await api.order(ORDER_ACCEPTED)
    expect(order.order.status).toBe('accepted')
    const added = order.items.find((i) => i.reference?.slug === MAZE_SLUG)
    expect(added?.status).toBe('draft')
    expect(order.totals.unpriced_items).toContain(added?.id)
    const events = await api.orderEvents(ORDER_ACCEPTED)
    expect(events.filter((e) => e.kind === 'item.added_after_acceptance')).toMatchObject([{ detail: { item_id: added?.id, order_status: 'accepted' } }])
  })

  it('asks again once the template, the pick or the quantity changes after the confirm (#5349)', async () => {
    const after = vi.spyOn(api, 'addOrderItemAfterAcceptance')
    const { user } = renderPage(
      <AddItemDialog open orderId={ORDER_ACCEPTED} mode="after_acceptance" order={{ number: 'ORD-2026-2', status: 'ready' }} onAdded={() => {}} onClose={() => {}} />,
    )
    const dialog = screen.getByRole('dialog', { name: 'Add item' })
    await user.type(within(dialog).getByLabelText('Template'), 'keychain')
    await user.click(await within(dialog).findByRole('button', { name: /Name Keychain/ }))
    expect(await within(dialog).findByRole('radio', { name: 'Reagan' })).toBeChecked()
    await user.click(within(dialog).getByRole('button', { name: 'Add item' }))
    // A ready order is named as such, and goes back to production.
    expect(within(dialog).getByRole('alert', { name: 'Confirm the change' })).toHaveTextContent(/ORD-2026-2 is ready\. .*goes back to in production/)
    // Another output picked: the confirm is gone, and the next click asks again.
    await user.click(within(dialog).getByRole('radio', { name: 'Nova' }))
    expect(within(dialog).queryByRole('alert', { name: 'Confirm the change' })).not.toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Add item' }))
    expect(within(dialog).getByRole('alert', { name: 'Confirm the change' })).toBeInTheDocument()
    // Another quantity: the same.
    await user.type(within(dialog).getByLabelText('Quantity'), '2')
    expect(within(dialog).queryByRole('alert', { name: 'Confirm the change' })).not.toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Add item' })).toBeEnabled()
    // Another template: the same.
    await user.click(within(dialog).getByRole('button', { name: 'Add item' }))
    await user.click(within(dialog).getByRole('button', { name: 'Change' }))
    expect(within(dialog).queryByRole('alert', { name: 'Confirm the change' })).not.toBeInTheDocument()
    expect(after).not.toHaveBeenCalled()
  })

  it.each(['0', '1e9', '2.5'])('refuses the quantity %s before sending anything (#4727)', async (typed) => {
    const add = vi.spyOn(api, 'addOrderItem')
    const { user } = renderPage(<AddItemDialog open orderId={ORDER_DRAFT} onAdded={() => {}} onClose={() => {}} />)
    const dialog = screen.getByRole('dialog', { name: 'Add item' })
    await user.type(within(dialog).getByLabelText('Template'), 'maze')
    await user.click(await within(dialog).findByRole('button', { name: /Ball Maze/ }))
    await user.clear(within(dialog).getByLabelText('Quantity'))
    await user.type(within(dialog).getByLabelText('Quantity'), typed)
    await user.click(within(dialog).getByRole('button', { name: 'Add item' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('a whole number of 1 or more')
    expect(add).not.toHaveBeenCalled()
  })

  it('offers no current version of a template whose inputs version is unknown (#4727)', async () => {
    const real = api.listModels.bind(api)
    vi.spyOn(api, 'listModels').mockImplementation(async () => (await real()).map(({ inputs_version: _, ...m }) => m))
    const { user } = renderPage(<AddItemDialog open orderId={ORDER_DRAFT} onAdded={() => {}} onClose={() => {}} />)
    const dialog = screen.getByRole('dialog', { name: 'Add item' })
    await user.type(within(dialog).getByLabelText('Template'), 'maze')
    await user.click(await within(dialog).findByRole('button', { name: /Ball Maze/ }))
    expect(await within(dialog).findByRole('radio', { name: 'Current version, default settings' })).toBeDisabled()
  })

  it('starts another template from its own default, not the last pick (#4727)', async () => {
    const { user } = renderPage(<AddItemDialog open orderId={ORDER_DRAFT} onAdded={() => {}} onClose={() => {}} />)
    const dialog = screen.getByRole('dialog', { name: 'Add item' })
    await user.type(within(dialog).getByLabelText('Template'), 'keychain')
    await user.click(await within(dialog).findByRole('button', { name: /Name Keychain/ }))
    expect(await within(dialog).findByRole('radio', { name: 'Reagan' })).toBeChecked()
    await user.click(within(dialog).getByRole('radio', { name: 'Nova' }))
    expect(within(dialog).getByRole('radio', { name: 'Nova' })).toBeChecked()
    await user.click(within(dialog).getByRole('button', { name: 'Change' }))
    await user.clear(within(dialog).getByLabelText('Template'))
    await user.type(within(dialog).getByLabelText('Template'), 'keychain')
    await user.click(await within(dialog).findByRole('button', { name: /Name Keychain/ }))
    expect(await within(dialog).findByRole('radio', { name: 'Reagan' })).toBeChecked()
  })

  it("adds a design request: a title and the customer's words, no template (#5280)", async () => {
    const add = vi.spyOn(api, 'addOrderItem')
    const onAdded = vi.fn()
    const { user } = renderPage(<AddItemDialog open orderId={ORDER_DRAFT} onAdded={onAdded} onClose={vi.fn()} />)
    const dialog = screen.getByRole('dialog', { name: 'Add item' })
    await user.click(within(dialog).getByRole('button', { name: 'Design request' }))
    expect(within(dialog).getByRole('button', { name: 'Add item' })).toBeDisabled()
    await user.type(within(dialog).getByLabelText('Title'), 'A dragon')
    await user.type(within(dialog).getByLabelText('What the customer asked for'), 'Green, about 10 cm')
    await user.click(within(dialog).getByRole('button', { name: 'Add item' }))
    await waitFor(() => expect(onAdded).toHaveBeenCalled())
    expect(add).toHaveBeenCalledWith(ORDER_DRAFT, { kind: 'request', title: 'A dragon', quantity: 1, notes: 'Green, about 10 cm' })
  })

  it("picks a revision's design when it is given an item to revise (#5280)", async () => {
    const revise = vi.spyOn(api, 'addOrderItemRevision')
    const onAdded = vi.fn()
    const { user } = renderPage(<AddItemDialog open orderId={ORDER_DRAFT} revise={{ itemId: 'item-1', slug: null }} onAdded={onAdded} onClose={vi.fn()} />)
    const dialog = screen.getByRole('dialog', { name: 'New revision' })
    expect(within(dialog).queryByRole('button', { name: 'Design request' })).toBeNull()
    await user.type(within(dialog).getByLabelText('Template'), 'maze')
    await user.click(await within(dialog).findByRole('button', { name: /Ball Maze/ }))
    await user.type(await within(dialog).findByLabelText('Note'), 'a maze instead')
    const current = await within(dialog).findByRole('radio', { name: 'Current version, default settings' })
    await waitFor(() => expect(current).toBeChecked())
    await user.click(within(dialog).getByRole('button', { name: 'Add revision' }))
    await waitFor(() => expect(revise).toHaveBeenCalled())
    const [, itemId, body] = revise.mock.calls[0]!
    expect(itemId).toBe('item-1')
    expect(body).toMatchObject({ note: 'a maze instead', reference: { slug: MAZE_SLUG } })
  })

  it("asks before it changes an accepted item's design, then revises on the after-acceptance route (#5350)", async () => {
    const plain = vi.spyOn(api, 'addOrderItemRevision')
    const after = vi.spyOn(api, 'addOrderItemRevisionAfterAcceptance').mockRejectedValueOnce(new Error('order ORD-2026-2 is on hold'))
    const onAdded = vi.fn()
    const { user } = renderPage(
      <AddItemDialog
        open
        orderId={ORDER_ACCEPTED}
        mode="after_acceptance"
        order={{ number: 'ORD-2026-2', status: 'in_production' }}
        revise={{ itemId: 'item-1', slug: null }}
        onAdded={onAdded}
        onClose={vi.fn()}
      />,
    )
    const dialog = screen.getByRole('dialog', { name: 'New revision' })
    await user.type(within(dialog).getByLabelText('Template'), 'maze')
    await user.click(await within(dialog).findByRole('button', { name: /Ball Maze/ }))
    await waitFor(() => expect(within(dialog).getByRole('radio', { name: 'Current version, default settings' })).toBeChecked())
    await user.click(within(dialog).getByRole('button', { name: 'Add revision' }))
    expect(within(dialog).getByRole('alert', { name: 'Confirm the change' })).toHaveTextContent(
      /^ORD-2026-2 is in production\. A new design changes what the customer gets\./,
    )
    expect(after).not.toHaveBeenCalled()
    // A failed save clears the confirm: the next try asks again.
    await user.click(within(dialog).getByRole('button', { name: 'Change the accepted order' }))
    expect(await within(dialog).findByText(/on hold/)).toBeInTheDocument()
    expect(within(dialog).queryByRole('alert', { name: 'Confirm the change' })).toBeNull()
    await user.click(within(dialog).getByRole('button', { name: 'Add revision' }))
    await user.click(within(dialog).getByRole('button', { name: 'Change the accepted order' }))
    await waitFor(() => expect(after).toHaveBeenCalledTimes(2))
    expect(after.mock.calls[1]![1]).toBe('item-1')
    expect(plain).not.toHaveBeenCalled()
  })

  it('adds several recent renders of any model in one request (#4777)', async () => {
    const add = vi.spyOn(api, 'addOrderItems')
    const onClose = vi.fn()
    const { user } = renderPage(<AddItemDialog open orderId={ORDER_DRAFT} onAdded={() => {}} onClose={onClose} />)
    const dialog = screen.getByRole('dialog', { name: 'Add item' })
    await user.click(within(dialog).getByRole('button', { name: 'Recent renders' }))
    const list = await within(dialog).findByRole('list', { name: 'Recent renders' })
    // Newest first, each with its model and the parameters that differ from the defaults.
    await waitFor(() =>
      expect(within(list).getAllByRole('checkbox').map((box) => box.getAttribute('aria-label'))).toEqual(['Reagan', 'Nova', 'Workshop']),
    )
    const nova = within(list).getByTestId(`recent-${NOVA}`)
    await waitFor(() => expect(nova).toHaveTextContent('Name Keychain'))
    await waitFor(() => expect(nova).toHaveTextContent('Name on the tag: Nova'))
    expect(within(dialog).getByRole('button', { name: 'Add item' })).toBeDisabled()
    await user.click(within(list).getByRole('checkbox', { name: 'Nova' }))
    await user.click(within(list).getByRole('checkbox', { name: 'Workshop' }))
    const quantity = within(list).getByLabelText('Quantity of Nova')
    await user.clear(quantity)
    await user.type(quantity, '2')
    await user.click(within(dialog).getByRole('button', { name: 'Add 2 items' }))
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(add).toHaveBeenCalledWith(ORDER_DRAFT, [
      { output_id: NOVA, quantity: 2 },
      { output_id: WORKSHOP, quantity: 1 },
    ])
    const order = await api.order(ORDER_DRAFT)
    expect(order.items.filter((i) => i.output_id === NOVA || i.output_id === WORKSHOP).map((i) => i.quantity)).toEqual([2, 1])
  })

  it("reads each listed model's schema once, however many models the page spans (#4777 review)", async () => {
    for (const [index, slug] of [MAZE_SLUG, DOLLHOUSE_SLUG, BOOKMARK_SLUG].entries()) {
      addMockOutput({ ...fixtureOutputs[0]!, id: String(index + 1).repeat(32), slug, name: `Other ${index}`, params: {} })
    }
    const read = api.getSchema.bind(api)
    // Answers that land one at a time, while the others are still in flight.
    const schema = vi.spyOn(api, 'getSchema').mockImplementation(async (slug, version) => {
      await new Promise((resolve) => setTimeout(resolve, 20 + 40 * [MAZE_SLUG, DOLLHOUSE_SLUG, BOOKMARK_SLUG].indexOf(slug)))
      return read(slug, version)
    })
    const { user } = renderPage(<AddItemDialog open orderId={ORDER_DRAFT} onAdded={() => {}} onClose={() => {}} />)
    const dialog = screen.getByRole('dialog', { name: 'Add item' })
    await user.click(within(dialog).getByRole('button', { name: 'Recent renders' }))
    const list = await within(dialog).findByRole('list', { name: 'Recent renders' })
    await waitFor(() => expect(within(list).getAllByRole('checkbox')).toHaveLength(6))
    await new Promise((resolve) => setTimeout(resolve, 300))
    await waitFor(() => expect(within(list).getByTestId(`recent-${NOVA}`)).toHaveTextContent('Name on the tag: Nova'))
    const slugs = schema.mock.calls.map(([slug]) => slug)
    expect([...slugs].sort()).toEqual([...new Set(slugs)].sort())
    expect(new Set(slugs).size).toBe(4)
  })

  it('filters recent renders by time, model, tag, text and whether an order has them (#4777)', async () => {
    const list = vi.spyOn(api, 'listRecentOutputs')
    const { user } = renderPage(<AddItemDialog open orderId={ORDER_DRAFT} onAdded={() => {}} onClose={() => {}} />)
    const dialog = screen.getByRole('dialog', { name: 'Add item' })
    await user.click(within(dialog).getByRole('button', { name: 'Recent renders' }))
    const rows = await within(dialog).findByRole('list', { name: 'Recent renders' })
    await waitFor(() => expect(within(rows).getAllByRole('checkbox')).toHaveLength(3))
    await choose(user, within(dialog).getByRole('combobox', { name: 'On an order' }), 'On an order')
    await waitFor(() => expect(within(rows).getAllByRole('checkbox').map((box) => box.getAttribute('aria-label'))).toEqual(['Reagan']))
    await choose(user, within(dialog).getByRole('combobox', { name: 'On an order' }), 'Not on an order')
    await waitFor(() => expect(within(rows).getAllByRole('checkbox')).toHaveLength(2))
    await user.type(within(dialog).getByLabelText('Search renders'), 'work')
    await waitFor(() => expect(within(rows).getAllByRole('checkbox').map((box) => box.getAttribute('aria-label'))).toEqual(['Workshop']))
    await user.click(within(dialog).getByRole('button', { name: '24 h' }))
    await waitFor(() => expect(within(dialog).getByText('No renders match.')).toBeInTheDocument())
    const last = list.mock.lastCall?.[0]
    expect(last).toMatchObject({ q: 'work', ordered: 'no' })
    expect(Date.now() - Date.parse(last?.from ?? '')).toBeLessThan(25 * 3600 * 1000)
    await choose(user, within(dialog).getByRole('combobox', { name: 'Model' }), 'Name Keychain')
    await waitFor(() => expect(list.mock.lastCall?.[0]).toMatchObject({ slug: ['name-keychain'] }))
    await user.type(within(dialog).getByLabelText('Tag'), 'gift')
    await waitFor(() => expect(list.mock.lastCall?.[0]).toMatchObject({ tag: 'gift' }))
  })

  it('asks before several renders change an accepted order, then adds them on the after-acceptance route (#4777)', async () => {
    const add = vi.spyOn(api, 'addOrderItemsAfterAcceptance')
    const onClose = vi.fn()
    const { user } = renderPage(
      <AddItemDialog open orderId={ORDER_ACCEPTED} mode="after_acceptance" order={{ number: 'ORD-2026-2', status: 'accepted' }} onAdded={() => {}} onClose={onClose} />,
    )
    const dialog = screen.getByRole('dialog', { name: 'Add item' })
    await user.click(within(dialog).getByRole('button', { name: 'Recent renders' }))
    const list = await within(dialog).findByRole('list', { name: 'Recent renders' })
    await user.click(await within(list).findByRole('checkbox', { name: 'Reagan' }))
    await user.click(within(dialog).getByRole('button', { name: 'Add 1 item' }))
    expect(within(dialog).getByRole('alert', { name: 'Confirm the change' })).toBeInTheDocument()
    expect(add).not.toHaveBeenCalled()
    await user.click(within(dialog).getByRole('button', { name: 'Change the accepted order' }))
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(add).toHaveBeenCalledWith(ORDER_ACCEPTED, [{ output_id: REAGAN, quantity: 1 }])
  })

})
