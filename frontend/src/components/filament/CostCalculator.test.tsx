import { screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { costPerKg } from '../../mocks/features/filament/metrics'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { CostCalculator } from './CostCalculator'

/** The query of every `GET /filament/cost-per-kg` the calculator sent. */
function costRequests(): URLSearchParams[] {
  const seen: URLSearchParams[] = []
  server.events.on('request:start', ({ request }) => {
    const url = new URL(request.url)
    if (url.pathname === '/api/v1/filament/cost-per-kg') seen.push(url.searchParams)
  })
  return seen
}

afterEach(() => server.events.removeAllListeners())

async function open(labelWeight = '1000') {
  const use = vi.fn()
  const rendered = renderPage(<CostCalculator labelWeight={labelWeight} onUseAsCost={use} />)
  await rendered.user.click(screen.getByRole('button', { name: 'Work out the cost per kg' }))
  return { ...rendered, use }
}

describe('the cost calculator (#7152)', () => {
  it('works out the cost per kg with tax and shipping in, and hands it to the type', async () => {
    const seen = costRequests()
    const { user, use } = await open('750')
    expect(screen.getByLabelText('Net weight per spool (g)')).toHaveValue('750')
    await user.type(screen.getByLabelText(/^Price paid/), '40')
    await user.clear(screen.getByLabelText('Spools in the price'))
    await user.type(screen.getByLabelText('Spools in the price'), '2')
    await user.type(screen.getByLabelText('Tax'), '3,20')
    await user.type(screen.getByLabelText('Shipping'), '5')
    await user.click(screen.getByRole('button', { name: 'Work it out' }))
    expect(await screen.findByTestId('cost-per-kg')).toHaveTextContent('$32.1333')
    expect(screen.getByText(/\$48\.20 for 2 spools of 750 g/)).toBeInTheDocument()
    expect(screen.getByText(/Without shipping, \$28\.80 per kg/)).toBeInTheDocument()
    expect(Object.fromEntries(seen[0]!)).toEqual({ price: '40', tax: '3.20', shipping: '5', quantity: '2', net_weight_g: '750' })
    await user.click(screen.getByRole('button', { name: 'Use as default cost' }))
    expect(use).toHaveBeenCalledWith('32.1333')
  })

  it('says what to fix and asks nothing for a price that is not an amount', async () => {
    const seen = costRequests()
    const { user } = await open()
    await user.type(screen.getByLabelText(/^Price paid/), '12.345678')
    await user.click(screen.getByRole('button', { name: 'Work it out' }))
    expect(screen.getByRole('alert')).toHaveTextContent('Enter the price paid')
    expect(seen).toEqual([])
    expect(screen.queryByRole('button', { name: 'Use as default cost' })).toBeNull()
  })

  it("shows the server's refusal", async () => {
    const { user } = await open('1')
    await user.type(screen.getByLabelText(/^Price paid/), '99999999.9999')
    await user.click(screen.getByRole('button', { name: 'Work it out' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('more than a cost per kg can hold')
  })

  it('drops the result when an input changes', async () => {
    const { user } = await open()
    await user.type(screen.getByLabelText(/^Price paid/), '20')
    await user.click(screen.getByRole('button', { name: 'Work it out' }))
    await screen.findByTestId('cost-per-kg')
    await user.type(screen.getByLabelText('Shipping'), '1')
    expect(screen.queryByTestId('cost-per-kg')).toBeNull()
  })
})

describe("the mock's cost per kg", () => {
  it('rounds as the backend does (tests/filament/test_cost_calc.py)', () => {
    const url = (query: string) => new URL(`http://x/api/v1/filament/cost-per-kg?${query}`)
    expect(costPerKg(url('price=1&net_weight_g=3000'))).toMatchObject({ cost_per_kg: '0.3333' })
    expect(costPerKg(url('price=2&net_weight_g=3000'))).toMatchObject({ cost_per_kg: '0.6667' })
    expect(costPerKg(url('price=40&tax=3.20&shipping=5&quantity=2&net_weight_g=750'))).toMatchObject({ total: '48.20', cost_per_kg: '32.1333', price_per_kg: '28.8000' })
    expect(costPerKg(url('price=1&net_weight_g=1000&currency=EUR'))).toBeInstanceOf(Response)
  })
})
