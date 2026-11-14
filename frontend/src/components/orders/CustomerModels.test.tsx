import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { resetMockState } from '../../mocks/handlers'
import {
  ITEM_MODELS,
  ORDER_MODELS,
  SOURCE_FAILED,
  SOURCE_MAKERWORLD,
  SOURCE_MANUAL,
  SOURCE_PURCHASE,
  SOURCE_READY,
  SOURCE_STUCK,
  state,
} from '../../mocks/features/orders/state'
import { choose } from '../../test/select'
import { renderPage } from '../../test/utils'
import { useAsync } from '../../lib/useAsync'
import { CustomerModels, safeLink } from './CustomerModels'

function Host() {
  const view = useAsync(() => api.order(ORDER_MODELS), [])
  if (!view.data) return null
  return <CustomerModels detail={view.data} itemId={ITEM_MODELS} onChanged={view.refresh} />
}

const row = (id: string) => screen.findByTestId(`source-${id}`)
const sourceOf = (id: string) => state.sources.find((s) => s.id === id)!

describe('safeLink (#6133)', () => {
  it('takes an http or https link only, checked again in the browser', () => {
    expect(safeLink('https://makerworld.com/en/models/1')).toBe('https://makerworld.com/en/models/1')
    expect(safeLink('http://example.com/a')).toBe('http://example.com/a')
    expect(safeLink('javascript:alert(1)')).toBeNull()
    expect(safeLink('data:text/html,<b>x</b>')).toBeNull()
    expect(safeLink('not a link')).toBeNull()
    expect(safeLink(null)).toBeNull()
  })
})

describe("CustomerModels, the customer's models panel (#6133)", () => {
  beforeEach(() => resetMockState())

  it('shows the colours asked for, each file with its check and facts, and its preview', async () => {
    renderPage(<Host />)
    const panel = await screen.findByRole('region', { name: "Customer's models" })
    expect(await within(panel).findByText(/Sunflower Yellow/)).toBeInTheDocument()
    const ready = await row(SOURCE_READY)
    expect(within(ready).getByText('cube.3mf')).toBeInTheDocument()
    expect(within(ready).getByText('Ready')).toBeInTheDocument()
    expect(within(ready).getByText(/3MF, 1 plate, 1,200 triangles, 40 × 30 × 12.5 mm/)).toBeInTheDocument()
    expect(within(ready).getByRole('img', { name: 'Preview of cube.3mf' })).toHaveAttribute(
      'src',
      `/api/v1/orders/${ORDER_MODELS}/sources/${SOURCE_READY}/preview`,
    )
    expect(within(ready).getByRole('button', { name: 'Download' })).toBeEnabled()
    // Only a failed check runs again.
    expect(within(ready).queryByRole('button', { name: 'Check again' })).toBeNull()
  })

  it('shows "Let the shop choose" when the customer left the colours to the shop', async () => {
    state.modelRequests = [{ item_id: ITEM_MODELS, shop_choice: true, filaments: [] }]
    renderPage(<Host />)
    const panel = await screen.findByRole('region', { name: "Customer's models" })
    expect(within(panel).getByText('Let the shop choose')).toBeInTheDocument()
  })

  it('downloads a staged file through its presigned GET, in a window opened before the fetch', async () => {
    const popup = { closed: false, opener: window, location: { href: '' }, close: vi.fn() }
    const open = vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window)
    const { user } = renderPage(<Host />)
    const ready = await row(SOURCE_READY)
    await user.click(within(ready).getByRole('button', { name: 'Download' }))
    expect(open).toHaveBeenCalledWith('', '_blank')
    await waitFor(() => expect(popup.location.href).toContain(`/staging/${SOURCE_READY}.frozen?`))
    expect(popup.opener).toBeNull()
    open.mockRestore()
  })

  it('says so when the download window is blocked', async () => {
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    const { user } = renderPage(<Host />)
    const ready = await row(SOURCE_READY)
    await user.click(within(ready).getByRole('button', { name: 'Download' }))
    expect(await screen.findByText(/Allow pop-ups/)).toBeInTheDocument()
    open.mockRestore()
  })

  it('checks a failed file again', async () => {
    const { user } = renderPage(<Host />)
    const failed = await row(SOURCE_FAILED)
    expect(within(failed).getByText(/Check failed/)).toBeInTheDocument()
    expect(within(failed).getByText(/The check timed out/)).toBeInTheDocument()
    await user.click(within(failed).getByRole('button', { name: 'Check again' }))
    await waitFor(() => expect(sourceOf(SOURCE_FAILED).check_status).toBe('checking'))
    expect(await within(await row(SOURCE_FAILED)).findByText('Checking')).toBeInTheDocument()
  })

  it('shows a stuck import and retries it', async () => {
    const { user } = renderPage(<Host />)
    const stuck = await row(SOURCE_STUCK)
    expect(within(stuck).getByText('Import stuck')).toBeInTheDocument()
    await user.click(within(stuck).getByRole('button', { name: 'Retry import' }))
    await waitFor(() => expect(state.events.some((e) => e.kind === 'source.import_retry')).toBe(true))
  })

  it('shows a link as text, and opens it only in a new tab with no referrer', async () => {
    renderPage(<Host />)
    const manual = await row(SOURCE_MANUAL)
    // The markup in the link is text, never HTML.
    expect(within(manual).getByText('https://www.printables.com/model/77-lid?<b>x</b>')).toBeInTheDocument()
    expect(manual.querySelector('b')).toBeNull()
    expect(within(manual).getByText('To process by hand')).toBeInTheDocument()
    const open = within(manual).getByRole('link', { name: 'Open link' })
    expect(open).toHaveAttribute('href', 'https://www.printables.com/model/77-lid?<b>x</b>')
    expect(open).toHaveAttribute('target', '_blank')
    expect(open).toHaveAttribute('rel', 'noopener noreferrer')
    expect(open).toHaveAttribute('referrerpolicy', 'no-referrer')
    const maker = await row(SOURCE_MAKERWORLD)
    expect(within(maker).getByText('MakerWorld')).toBeInTheDocument()
  })

  it('offers no anchor for a link whose scheme is not http or https', async () => {
    sourceOf(SOURCE_MANUAL).url = 'javascript:alert(1)'
    renderPage(<Host />)
    const manual = await row(SOURCE_MANUAL)
    expect(within(manual).getByText('javascript:alert(1)')).toBeInTheDocument()
    expect(within(manual).queryByRole('link')).toBeNull()
  })

  it('declines a link with a reason', async () => {
    const { user } = renderPage(<Host />)
    const manual = await row(SOURCE_MANUAL)
    await user.click(within(manual).getByRole('button', { name: 'Decline' }))
    await user.type(within(manual).getByLabelText('Why the shop declines it'), 'A paid model')
    await user.click(within(manual).getByRole('button', { name: 'Decline link' }))
    await waitFor(() => expect(sourceOf(SOURCE_MANUAL).status).toBe('declined'))
    expect(sourceOf(SOURCE_MANUAL).status_reason).toBe('A paid model')
    expect(await within(await row(SOURCE_MANUAL)).findByText(/A paid model/)).toBeInTheDocument()
  })

  it('gives each open decline form its own reason field', async () => {
    const { user } = renderPage(<Host />)
    const manual = await row(SOURCE_MANUAL)
    const makerworld = await row(SOURCE_MAKERWORLD)
    await user.click(within(manual).getByRole('button', { name: 'Decline' }))
    await user.click(within(makerworld).getByRole('button', { name: 'Decline' }))
    const first = within(manual).getByLabelText('Why the shop declines it')
    const second = within(makerworld).getByLabelText('Why the shop declines it')
    expect(first).not.toBe(second)
    expect(manual).toContainElement(first)
    expect(makerworld).toContainElement(second)
  })

  it('records what the shop paid for a thing to buy, changes it, and marks another not needed', async () => {
    const { user } = renderPage(<Host />)
    const purchase = await row(SOURCE_PURCHASE)
    expect(within(purchase).getByText('The 6 mm magnets, 20 of them')).toBeInTheDocument()
    expect(within(purchase).getByText('https://parts.example.com/magnets')).toBeInTheDocument()
    expect(within(purchase).getByRole('link', { name: 'Open link' })).toHaveAttribute('rel', 'noopener noreferrer')
    await user.click(within(purchase).getByRole('button', { name: 'Record cost' }))
    await user.type(within(purchase).getByLabelText('What the shop paid'), '4.50')
    await choose(user, within(purchase).getByRole('combobox', { name: 'Cost kind' }), 'Paid model file')
    await user.click(within(purchase).getByRole('button', { name: 'Record' }))
    await waitFor(() => expect(sourceOf(SOURCE_PURCHASE).status).toBe('recorded'))
    expect(sourceOf(SOURCE_PURCHASE)).toMatchObject({ amount_minor: 450, cost_kind: 'other' })
    const recorded = await row(SOURCE_PURCHASE)
    expect(await within(recorded).findByText(/\$4\.50/)).toBeInTheDocument()

    await user.click(within(recorded).getByRole('button', { name: 'Change' }))
    const amount = within(recorded).getByLabelText('What the shop paid')
    await user.clear(amount)
    await user.type(amount, '5')
    await user.click(within(recorded).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(sourceOf(SOURCE_PURCHASE).amount_minor).toBe(500))

    await user.click(within(await row(SOURCE_PURCHASE)).getByRole('button', { name: 'Not needed' }))
    await user.type(within(await row(SOURCE_PURCHASE)).getByLabelText('A note to the customer'), 'You have these')
    await user.click(within(await row(SOURCE_PURCHASE)).getByRole('button', { name: 'Mark not needed' }))
    await waitFor(() => expect(sourceOf(SOURCE_PURCHASE).status).toBe('not_needed'))
    expect(sourceOf(SOURCE_PURCHASE).shop_note).toBe('You have these')
  })
})
