import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { mockSettings, resetMockState } from '../../mocks/handlers'
import { renderPage } from '../../test/utils'
import { ShippingOptionsEditor } from './ShippingOptionsEditor'

describe('ShippingOptionsEditor (#5281)', () => {
  beforeEach(() => resetMockState())

  it('refuses an id the backend would refuse, then saves the fixed one', async () => {
    const { user } = renderPage(<ShippingOptionsEditor />)
    const options = screen.getByRole('region', { name: 'Shipping options' })
    await waitFor(() => expect(within(options).getByRole('button', { name: 'Add option' })).toBeEnabled())
    await user.click(within(options).getByRole('button', { name: 'Add option' }))
    await user.type(within(options).getByLabelText('Option 1 id'), 'Tracked Post')
    await user.type(within(options).getByLabelText('Option 1 label'), 'Tracked post')
    await user.type(within(options).getByLabelText('Option 1 price (USD)'), '6.50')
    await user.click(within(options).getByRole('button', { name: 'Save shipping options' }))
    expect(await within(options).findByRole('alert')).toHaveTextContent('Option 1: enter an id of 1 to 40 lowercase letters')
    expect(mockSettings().storefront_shipping_options).toEqual([])

    await user.clear(within(options).getByLabelText('Option 1 id'))
    await user.type(within(options).getByLabelText('Option 1 id'), 'post')
    await user.click(within(options).getByRole('button', { name: 'Save shipping options' }))
    await waitFor(() => expect(mockSettings().storefront_shipping_options).toEqual([{ id: 'post', label: 'Tracked post', amount_minor: 650, ships: true }]))
  })

  it('refuses a repeated id and a label over 200 characters before it saves', async () => {
    const { user } = renderPage(<ShippingOptionsEditor />)
    const options = screen.getByRole('region', { name: 'Shipping options' })
    await waitFor(() => expect(within(options).getByRole('button', { name: 'Add option' })).toBeEnabled())
    for (const n of [1, 2]) {
      await user.click(within(options).getByRole('button', { name: 'Add option' }))
      await user.type(within(options).getByLabelText(`Option ${n} id`), 'post')
      await user.type(within(options).getByLabelText(`Option ${n} label`), 'Post')
      await user.type(within(options).getByLabelText(`Option ${n} price (USD)`), '1')
    }
    await user.click(within(options).getByRole('button', { name: 'Save shipping options' }))
    expect(await within(options).findByRole('alert')).toHaveTextContent('Option 2: the id post is already used by option 1.')
    await user.clear(within(options).getByLabelText('Option 2 id'))
    await user.type(within(options).getByLabelText('Option 2 id'), 'courier')
    await user.clear(within(options).getByLabelText('Option 2 label'))
    await user.click(within(options).getByLabelText('Option 2 label'))
    await user.paste('x'.repeat(201))
    await user.click(within(options).getByRole('button', { name: 'Save shipping options' }))
    expect(await within(options).findByRole('alert')).toHaveTextContent('Option 2: a label is at most 200 characters.')
    expect(mockSettings().storefront_shipping_options).toEqual([])
  })
})

