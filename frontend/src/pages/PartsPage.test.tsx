import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import { api } from '../api/client'
import { resetMockState } from '../mocks/handlers'
import { seedPart } from '../mocks/features/orders/parts'
import { renderPage } from '../test/utils'
import { PartsPage } from './PartsPage'

describe('PartsPage (#5543)', () => {
  beforeEach(() => resetMockState())

  it('lists the parts with their unit cost and a Low badge', async () => {
    const hook = seedPart({ name: 'Silver hook', pack_size: 100, pack_price_minor: 599, on_hand: 3, low_stock: 5, supplier: 'AliExpress', link: 'https://example.com/hook' })
    seedPart({ name: 'Ring', pack_size: 10, pack_price_minor: 100, on_hand: 50 })
    renderPage(<PartsPage />)

    const row = await screen.findByTestId(`part-row-${hook.id}`)
    expect(within(row).getByText('Low')).toBeInTheDocument()
    expect(within(row).getByRole('link', { name: 'AliExpress' })).toHaveAttribute('href', 'https://example.com/hook')
    // 5.99 cents a hook: the fraction of a cent is kept.
    expect(within(row).getByText(/0\.0599/)).toBeInTheDocument()
    expect(screen.getAllByText('Low')).toHaveLength(1)
  })

  it('adds a part', async () => {
    const user = userEvent.setup()
    renderPage(<PartsPage />)
    await user.click(await screen.findByRole('button', { name: 'Add part' }))
    const form = await screen.findByRole('form', { name: 'Add part' })
    await user.type(within(form).getByLabelText('Name'), 'Magnet')
    await user.type(within(form).getByLabelText('Pack size'), '50')
    await user.type(within(form).getByLabelText(/Pack price/), '4.00')
    await user.type(within(form).getByLabelText('On hand now'), '20')
    await user.click(within(form).getByRole('button', { name: 'Save part' }))

    expect(await screen.findByText('Magnet')).toBeInTheDocument()
    const [magnet] = await api.parts()
    expect([magnet?.on_hand, magnet?.unit_cost_minor]).toEqual([20, '8'])
  })

  it('adds stock, and an adjust needs a note', async () => {
    const user = userEvent.setup()
    const hook = seedPart({ name: 'Silver hook', pack_size: 100, pack_price_minor: 500, on_hand: 100 })
    renderPage(<PartsPage />)

    await user.click(await screen.findByRole('button', { name: 'Add stock of Silver hook' }))
    const add = await screen.findByRole('region', { name: 'Silver hook: add stock' })
    await user.type(within(add).getByLabelText(/Price a pack/), '7.00')
    await user.click(within(add).getByRole('button', { name: 'Add stock' }))
    await waitFor(async () => expect((await api.parts())[0]?.unit_cost_minor).toBe('6'))

    await user.click(await screen.findByRole('button', { name: 'Adjust Silver hook' }))
    const adjust = await screen.findByRole('region', { name: 'Silver hook: adjust' })
    const countField = within(adjust).getByLabelText('Count on the shelf')
    await user.clear(countField)
    await user.type(countField, '190')
    await user.click(within(adjust).getByRole('button', { name: 'Save count' }))
    expect(await within(adjust).findByRole('alert')).toHaveTextContent('Say why the count changed.')
    await user.type(within(adjust).getByLabelText('Why it changed'), 'counted')
    await user.click(within(adjust).getByRole('button', { name: 'Save count' }))
    await waitFor(async () => expect((await api.parts())[0]?.on_hand).toBe(190))

    await user.click(await screen.findByRole('button', { name: 'Stock line of Silver hook' }))
    const line = await screen.findByRole('list', { name: 'Stock line' })
    await waitFor(() => expect(within(line).getAllByRole('listitem')).toHaveLength(3))
    expect(within(line).getAllByRole('listitem')[0]).toHaveTextContent('counted')
    expect(hook.id).toBeTruthy()
  })

  it('saves a model rule', async () => {
    const user = userEvent.setup()
    const hook = seedPart({ name: 'Silver hook', pack_size: 100, pack_price_minor: 599 })
    renderPage(<PartsPage />)
    await screen.findByTestId(`part-row-${hook.id}`)
    const rules = screen.getByRole('region', { name: 'Model rules' })

    await user.type(within(rules).getByLabelText('Model slug'), 'keychain')
    await user.click(within(rules).getByRole('button', { name: 'Open rule' }))
    expect(await within(rules).findByText(/keychain has no rule/)).toBeInTheDocument()
    await user.click(within(rules).getByRole('button', { name: 'Add line' }))
    await user.click(within(rules).getByRole('button', { name: 'Save rule' }))

    expect(await within(rules).findByText('Saved.')).toBeInTheDocument()
    expect((await api.partRule('keychain')).lines).toEqual([{ per_unit: 1, part_id: hook.id, when: [] }])
  })
})
