import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { MAZE_SLUG } from '../../mocks/fixtures'
import { resetMockState } from '../../mocks/handlers'
import { ORDER_ACCEPTED, ORDER_DRAFT } from '../../mocks/features/orders/state'
import { renderPage } from '../../test/utils'
import { AddItemDialog } from './AddItemDialog'

const NOVA = 'c'.repeat(32)

describe('AddItemDialog (#4251)', () => {
  beforeEach(() => resetMockState())

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
    expect(added?.reference.commit).toBe(model.version)
    expect(added?.quantity).toBe(2)
    add.mockRestore()
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
    add.mockRestore()
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

  it('refuses a quantity under 1 before sending anything', async () => {
    const add = vi.spyOn(api, 'addOrderItem')
    const { user } = renderPage(<AddItemDialog open orderId={ORDER_DRAFT} onAdded={() => {}} onClose={() => {}} />)
    const dialog = screen.getByRole('dialog', { name: 'Add item' })
    await user.type(within(dialog).getByLabelText('Template'), 'maze')
    await user.click(await within(dialog).findByRole('button', { name: /Ball Maze/ }))
    await user.clear(within(dialog).getByLabelText('Quantity'))
    await user.type(within(dialog).getByLabelText('Quantity'), '0')
    await user.click(within(dialog).getByRole('button', { name: 'Add item' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('1 or more')
    expect(add).not.toHaveBeenCalled()
    add.mockRestore()
  })
})
