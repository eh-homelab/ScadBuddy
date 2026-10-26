import { screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { useLocation } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { resetMockState } from '../../mocks/handlers'
import { ORDER_DRAFT, OUTPUT_REAGAN } from '../../mocks/features/orders/state'
import { choose } from '../../test/select'
import { renderPage } from '../../test/utils'
import { AddToOrderDialog } from './AddToOrderDialog'

function Location() {
  const location = useLocation()
  return <output data-testid="location">{location.pathname}</output>
}

/** The dialog behind a toggle, as a page opens and closes it. */
function Toggled() {
  const [open, setOpen] = useState(true)
  return (
    <>
      <button type="button" onClick={() => setOpen((was) => !was)}>
        Toggle
      </button>
      <AddToOrderDialog open={open} outputId={OUTPUT_REAGAN} onClose={() => setOpen(false)} />
    </>
  )
}

describe('AddToOrderDialog (#4249)', () => {
  beforeEach(() => resetMockState())
  // Here, not at the end of a test: a failed assertion must not leak a spy into the next.
  afterEach(() => vi.restoreAllMocks())

  it("adds the output to the customer's open order and moves to it", async () => {
    const add = vi.spyOn(api, 'addOrderItem')
    const onClose = vi.fn()
    const { user } = renderPage(
      <>
        <AddToOrderDialog open outputId={OUTPUT_REAGAN} outputName="Reagan" onClose={onClose} />
        <Location />
      </>,
    )
    const dialog = screen.getByRole('dialog', { name: 'Add to order' })
    await choose(user, within(dialog).getByRole('combobox', { name: 'Customer' }), 'Ada Lovelace')
    await waitFor(() => expect(within(dialog).getByRole('combobox', { name: 'Order' })).toHaveTextContent('ORD-2026-3'))
    await user.clear(within(dialog).getByLabelText('Quantity'))
    await user.type(within(dialog).getByLabelText('Quantity'), '3')
    await user.click(within(dialog).getByRole('button', { name: 'Add to order' }))
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent(`/orders/${ORDER_DRAFT}`))
    expect(add).toHaveBeenCalledWith(ORDER_DRAFT, { output_id: OUTPUT_REAGAN, quantity: 3 })
    expect(onClose).toHaveBeenCalled()
  })

  it('makes a new customer and a new order when asked', async () => {
    const { user } = renderPage(
      <>
        <AddToOrderDialog open outputId={OUTPUT_REAGAN} onClose={() => {}} />
        <Location />
      </>,
    )
    const dialog = screen.getByRole('dialog', { name: 'Add to order' })
    await choose(user, within(dialog).getByRole('combobox', { name: 'Customer' }), 'New customer…')
    await user.type(within(dialog).getByLabelText('Customer name'), 'Grace Hopper')
    await user.type(within(dialog).getByLabelText('Email'), 'grace@example.com')
    await user.type(within(dialog).getByLabelText('Phone'), '+44 20 7946 0000')
    await user.click(within(dialog).getByRole('button', { name: 'Add to order' }))
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent(/^\/orders\/o-/))
    const customers = await api.customers({ q: 'grace' })
    expect(customers).toHaveLength(1)
    expect(customers[0]?.contacts).toEqual([
      expect.objectContaining({ kind: 'email', value: 'grace@example.com', primary: true }),
      expect.objectContaining({ kind: 'phone', value: '+44 20 7946 0000', primary: true }),
    ])
    const order = screen.getByTestId('location').textContent?.split('/').pop() ?? ''
    expect((await api.order(order)).order.customer_id).toBe(customers[0]?.id)
  })

  it('refuses a new customer with no name, and makes nothing (#4251)', async () => {
    const createCustomer = vi.spyOn(api, 'createCustomer')
    const { user } = renderPage(<AddToOrderDialog open outputId={OUTPUT_REAGAN} onClose={() => {}} />)
    const dialog = screen.getByRole('dialog', { name: 'Add to order' })
    await choose(user, within(dialog).getByRole('combobox', { name: 'Customer' }), 'New customer…')
    await user.click(within(dialog).getByRole('button', { name: 'Add to order' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Name the new customer.')
    expect(createCustomer).not.toHaveBeenCalled()
  })

  it('opens empty again after a half-typed new customer is closed away', async () => {
    const { user } = renderPage(<Toggled />)
    let dialog = screen.getByRole('dialog', { name: 'Add to order' })
    await choose(user, within(dialog).getByRole('combobox', { name: 'Customer' }), 'New customer…')
    await user.type(within(dialog).getByLabelText('Customer name'), 'Grace Hopper')
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Add to order' })).not.toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: 'Toggle' }))
    dialog = await screen.findByRole('dialog', { name: 'Add to order' })
    expect(within(dialog).queryByLabelText('Customer name')).toBeNull()
  })

  it('keeps the order it made when adding the item fails, so a retry makes no second (#4723)', async () => {
    const createOrder = vi.spyOn(api, 'createOrder')
    const add = vi.spyOn(api, 'addOrderItem').mockRejectedValueOnce(new Error('The server is away.'))
    const { user } = renderPage(
      <>
        <AddToOrderDialog open outputId={OUTPUT_REAGAN} onClose={() => {}} />
        <Location />
      </>,
    )
    const dialog = screen.getByRole('dialog', { name: 'Add to order' })
    await choose(user, within(dialog).getByRole('combobox', { name: 'Customer' }), 'Ada Lovelace')
    await waitFor(() => expect(within(dialog).getByRole('combobox', { name: 'Order' })).toHaveTextContent('ORD-2026-3'))
    await choose(user, within(dialog).getByRole('combobox', { name: 'Order' }), 'New order')
    await user.click(within(dialog).getByRole('button', { name: 'Add to order' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('The server is away.')
    expect(createOrder).toHaveBeenCalledTimes(1)
    const made = (await createOrder.mock.results[0]?.value) as { id: string; number: string }
    // The order it made is now the one picked, and the retry adds to it.
    await waitFor(() => expect(within(dialog).getByRole('combobox', { name: 'Order' })).toHaveTextContent(made.number))
    await user.click(within(dialog).getByRole('button', { name: 'Add to order' }))
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent(`/orders/${made.id}`))
    expect(createOrder).toHaveBeenCalledTimes(1)
    expect(add).toHaveBeenLastCalledWith(made.id, { output_id: OUTPUT_REAGAN, quantity: 1 })
  })

  it('opened again, never offers an order it made that is no longer open (#4743)', async () => {
    const createOrder = vi.spyOn(api, 'createOrder')
    vi.spyOn(api, 'addOrderItem').mockRejectedValueOnce(new Error('The server is away.'))
    const { user } = renderPage(<Toggled />)
    let dialog = screen.getByRole('dialog', { name: 'Add to order' })
    await choose(user, within(dialog).getByRole('combobox', { name: 'Customer' }), 'Ada Lovelace')
    await waitFor(() => expect(within(dialog).getByRole('combobox', { name: 'Order' })).toHaveTextContent('ORD-2026-3'))
    await choose(user, within(dialog).getByRole('combobox', { name: 'Order' }), 'New order')
    await user.click(within(dialog).getByRole('button', { name: 'Add to order' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('The server is away.')
    const made = (await createOrder.mock.results[0]?.value) as { id: string; number: string }
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Add to order' })).not.toBeInTheDocument())
    // Cancelled elsewhere while the dialog was closed.
    await api.cancelOrder(made.id)
    await user.click(screen.getByRole('button', { name: 'Toggle' }))
    dialog = await screen.findByRole('dialog', { name: 'Add to order' })
    await choose(user, within(dialog).getByRole('combobox', { name: 'Customer' }), 'Ada Lovelace')
    await waitFor(() => expect(within(dialog).getByRole('combobox', { name: 'Order' })).toHaveTextContent('ORD-2026-3'))
    await user.click(within(dialog).getByRole('combobox', { name: 'Order' }))
    // Waits for the reload that carries the cancel, not a cached list (#4749 review): the
    // list is shown, and the cancelled order is not in it (#4751 review).
    await waitFor(() => {
      expect(screen.getByRole('option', { name: /\bORD-2026-3\b/ })).toBeInTheDocument()
      expect(screen.queryByRole('option', { name: new RegExp(`${made.number}\\b`) })).toBeNull()
    })
  })

  it('kept open, drops an order it made once a reload lists it as no longer open (#4746)', async () => {
    const createOrder = vi.spyOn(api, 'createOrder')
    vi.spyOn(api, 'addOrderItem').mockRejectedValueOnce(new Error('The server is away.'))
    const { user } = renderPage(<AddToOrderDialog open outputId={OUTPUT_REAGAN} onClose={() => {}} />)
    const dialog = screen.getByRole('dialog', { name: 'Add to order' })
    await choose(user, within(dialog).getByRole('combobox', { name: 'Customer' }), 'Ada Lovelace')
    await waitFor(() => expect(within(dialog).getByRole('combobox', { name: 'Order' })).toHaveTextContent('ORD-2026-3'))
    await choose(user, within(dialog).getByRole('combobox', { name: 'Order' }), 'New order')
    await user.click(within(dialog).getByRole('button', { name: 'Add to order' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('The server is away.')
    const made = (await createOrder.mock.results[0]?.value) as { id: string; number: string }
    await waitFor(() => expect(within(dialog).getByRole('combobox', { name: 'Order' })).toHaveTextContent(made.number))
    // Cancelled elsewhere while the dialog stays open; re-picking the customer reloads their orders.
    await api.cancelOrder(made.id)
    await choose(user, within(dialog).getByRole('combobox', { name: 'Customer' }), 'Bob Builder')
    await choose(user, within(dialog).getByRole('combobox', { name: 'Customer' }), 'Ada Lovelace')
    await waitFor(() => expect(within(dialog).getByRole('combobox', { name: 'Order' })).toHaveTextContent('ORD-2026-3'))
    await user.click(within(dialog).getByRole('combobox', { name: 'Order' }))
    // Waits for the reload that carries the cancel, not a cached list (#4749 review): the
    // list is shown, and the cancelled order is not in it (#4751 review).
    await waitFor(() => {
      expect(screen.getByRole('option', { name: /\bORD-2026-3\b/ })).toBeInTheDocument()
      expect(screen.queryByRole('option', { name: new RegExp(`${made.number}\\b`) })).toBeNull()
    })
  })

  it('keeps the customer it made when the order fails, so a retry makes no second (#4723)', async () => {
    const createCustomer = vi.spyOn(api, 'createCustomer')
    vi.spyOn(api, 'createOrder').mockRejectedValueOnce(new Error('The server is away.'))
    const { user } = renderPage(
      <>
        <AddToOrderDialog open outputId={OUTPUT_REAGAN} onClose={() => {}} />
        <Location />
      </>,
    )
    const dialog = screen.getByRole('dialog', { name: 'Add to order' })
    await choose(user, within(dialog).getByRole('combobox', { name: 'Customer' }), 'New customer…')
    await user.type(within(dialog).getByLabelText('Customer name'), 'Grace Hopper')
    await user.click(within(dialog).getByRole('button', { name: 'Add to order' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('The server is away.')
    await user.click(within(dialog).getByRole('button', { name: 'Add to order' }))
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent(/^\/orders\/o-/))
    expect(createCustomer).toHaveBeenCalledTimes(1)
    expect(await api.customers({ q: 'grace' })).toHaveLength(1)
  })

  it('stays open while a submit is in flight (#4723)', async () => {
    let release: () => void = () => {}
    const held = new Promise<void>((resolve) => (release = resolve))
    const real = api.createOrder.bind(api)
    const createOrder = vi.spyOn(api, 'createOrder').mockImplementation(async (body) => {
      await held
      return real(body)
    })
    const { user } = renderPage(
      <>
        <Toggled />
        <Location />
      </>,
    )
    const dialog = screen.getByRole('dialog', { name: 'Add to order' })
    await choose(user, within(dialog).getByRole('combobox', { name: 'Customer' }), 'Ada Lovelace')
    await waitFor(() => expect(within(dialog).getByRole('combobox', { name: 'Order' })).toHaveTextContent('ORD-2026-3'))
    await choose(user, within(dialog).getByRole('combobox', { name: 'Order' }), 'New order')
    await user.click(within(dialog).getByRole('button', { name: 'Add to order' }))
    await waitFor(() => expect(createOrder).toHaveBeenCalled())
    await user.keyboard('{Escape}')
    expect(screen.getByRole('dialog', { name: 'Add to order' })).toBeInTheDocument()
    release()
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent(/^\/orders\/o-/))
  })

  it('shows a rough estimate once a filament is picked, and records nothing (#4250)', async () => {
    const add = vi.spyOn(api, 'addOrderItem')
    const { user } = renderPage(<AddToOrderDialog open outputId={OUTPUT_REAGAN} outputName="Reagan" onClose={() => {}} />)
    const dialog = screen.getByRole('dialog', { name: 'Add to order' })
    await choose(user, await within(dialog).findByRole('combobox', { name: 'Estimate with' }), /Sunflower Yellow/)
    const estimate = await within(dialog).findByTestId('order-estimate')
    expect(estimate).toHaveTextContent(/An estimate: about [\d.]+ g/)
    expect(estimate).toHaveTextContent('$')
    expect(add).not.toHaveBeenCalled()
  })
})
