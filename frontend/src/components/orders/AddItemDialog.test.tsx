import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { MAZE_SLUG } from '../../mocks/fixtures'
import { resetMockState } from '../../mocks/handlers'
import { ORDER_ACCEPTED, ORDER_DRAFT } from '../../mocks/features/orders/state'
import { choose } from '../../test/select'
import { renderPage } from '../../test/utils'
import { AddItemDialog } from './AddItemDialog'

const NOVA = 'c'.repeat(32)

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
})
