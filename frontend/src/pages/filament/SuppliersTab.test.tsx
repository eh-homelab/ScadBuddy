import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { listSuppliers, patchSupplier } from '../../api/filamentPurchasing'
import { SUPPLIER } from '../../mocks/features/filamentPurchasingSeed'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { SuppliersTab } from './SuppliersTab'

const table = () => screen.findByRole('table', { name: 'Suppliers' })
const rowOf = (body: HTMLElement, id: string) => within(body).getAllByRole('row').find((row) => row.dataset.supplier === id)

/** The bodies of every request to `/filament/suppliers` with `method`, read as they leave. */
function recorded(method: string) {
  const bodies: unknown[] = []
  server.events.on('request:start', ({ request }) => {
    if (request.method === method && new URL(request.url).pathname.endsWith('/filament/suppliers')) void request.clone().json().then((body) => bodies.push(body))
  })
  return bodies
}

beforeEach(() => {
  window.localStorage.clear()
})

describe('the Suppliers tab', () => {
  it('lists each supplier with its products, spools bought and spend', async () => {
    renderPage(<SuppliersTab />)
    const body = await table()
    const bambu = rowOf(body, SUPPLIER.BAMBU)!
    expect(within(bambu).getByRole('link', { name: 'Bambu Lab' })).toHaveAttribute('href', `/filament/suppliers/${SUPPLIER.BAMBU}`)
    expect(within(bambu).getByText('BL-208841')).toBeInTheDocument()
    expect(within(bambu).getByText('$34.61')).toBeInTheDocument()
    // Micro Center has no confirmed invoice: no spend yet.
    expect(within(rowOf(body, SUPPLIER.MICRO)!).queryByText(/\$/)).toBeNull()
  })

  it('shows a website as a safe link', async () => {
    renderPage(<SuppliersTab />)
    const link = within(rowOf(await table(), SUPPLIER.BAMBU)!).getByRole('link', { name: 'us.store.bambulab.com' })
    expect(link).toHaveAttribute('href', 'https://us.store.bambulab.com/')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
  })

  it('refuses a website that is not http or https before it asks the API', async () => {
    const posted = recorded('POST')
    const { user } = renderPage(<SuppliersTab />)
    await table()
    await user.click(screen.getByRole('button', { name: 'Add supplier' }))
    await user.type(await screen.findByLabelText('Name'), 'Sketchy')
    await user.type(screen.getByLabelText('Website'), 'javascript:x')
    await user.click(screen.getAllByRole('button', { name: 'Add supplier' }).at(-1)!)
    expect(await screen.findByRole('alert')).toHaveTextContent('The website must start with http:// or https://.')
    expect(posted).toEqual([])
    expect((await listSuppliers()).some((one) => one.name === 'Sketchy')).toBe(false)
  })

  it('adds a supplier and lists it', async () => {
    const posted = recorded('POST')
    const { user } = renderPage(<SuppliersTab />)
    await table()
    await user.click(screen.getByRole('button', { name: 'Add supplier' }))
    await user.type(await screen.findByLabelText('Name'), 'Polymaker')
    await user.type(screen.getByLabelText('Website'), 'https://polymaker.com')
    await user.click(screen.getAllByRole('button', { name: 'Add supplier' }).at(-1)!)
    expect(await screen.findByRole('link', { name: 'Polymaker' })).toBeInTheDocument()
    await waitFor(() => expect(posted).toEqual([{ name: 'Polymaker', website: 'https://polymaker.com' }]))
  })

  it('hides an archived supplier until Show archived, and remembers the choice', async () => {
    await patchSupplier(SUPPLIER.AMAZON, { archived: true })
    const first = renderPage(<SuppliersTab />)
    const body = await table()
    expect(rowOf(body, SUPPLIER.AMAZON)).toBeUndefined()
    await first.user.click(screen.getByRole('checkbox', { name: 'Show archived suppliers' }))
    expect(within(rowOf(await table(), SUPPLIER.AMAZON)!).getByText('Archived')).toBeInTheDocument()
    first.unmount()
    renderPage(<SuppliersTab />)
    expect(await screen.findByRole('checkbox', { name: 'Show archived suppliers' })).toBeChecked()
  })

  it('archives a supplier from its row', async () => {
    const { user } = renderPage(<SuppliersTab />)
    await user.click(await screen.findByRole('button', { name: 'Archive Micro Center' }))
    await waitFor(() => expect(screen.queryByRole('link', { name: 'Micro Center' })).toBeNull())
    expect((await listSuppliers({ includeArchived: true })).find((one) => one.id === SUPPLIER.MICRO)?.archived_at).toBeTruthy()
  })
})
