import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it } from 'vitest'
import { listPrices, listProducts, patchProduct } from '../../api/filamentPurchasing'
import { T } from '../../mocks/features/filament/seed'
import { seedItem, seedOffer } from '../../mocks/features/inventory'
import { PRODUCT, SUPPLIER } from '../../mocks/features/filamentPurchasingSeed'
import { server } from '../../mocks/server'
import { choose } from '../../test/select'
import { renderPage } from '../../test/utils'
import { SupplierPage } from './SupplierPage'

function render(id: string = SUPPLIER.BAMBU) {
  return renderPage(
    <Routes>
      <Route path="/filament/suppliers/:id" element={<SupplierPage />} />
      <Route path="*" element={<p>Elsewhere</p>} />
    </Routes>,
    { route: `/filament/suppliers/${id}` },
  )
}

const heading = (name: string) => screen.findByRole('heading', { level: 1, name })
const productRow = (id: string) => screen.getAllByRole('row').find((row) => row.dataset.product === id && row.closest('table')?.getAttribute('aria-label') === 'Products')!

function recorded(method: string, match: RegExp) {
  const bodies: unknown[] = []
  server.events.on('request:start', ({ request }) => {
    if (request.method === method && match.test(new URL(request.url).pathname)) void request.clone().json().then((body) => bodies.push(body))
  })
  return bodies
}

beforeEach(() => {
  window.localStorage.clear()
})

describe('the supplier page', () => {
  it('names the supplier in its heading and fills the editor', async () => {
    render()
    await heading('Bambu Lab')
    expect(screen.getByLabelText('Customer number')).toHaveValue('BL-208841')
  })

  it("marks the product a link names, and links each product's type and price sheet row (#6869)", async () => {
    renderPage(
      <Routes>
        <Route path="/filament/suppliers/:id" element={<SupplierPage />} />
      </Routes>,
      { route: `/filament/suppliers/${SUPPLIER.BAMBU}#product-${PRODUCT.JADE}` },
    )
    await heading('Bambu Lab')
    await waitFor(() => expect(productRow(PRODUCT.JADE)).toHaveAttribute('aria-current', 'location'))
    expect(productRow(PRODUCT.ORANGE)).not.toHaveAttribute('aria-current')
    expect(within(productRow(PRODUCT.JADE)).getByRole('link', { name: /BL-PLA-BASIC|Jade|A00/i })).toHaveAttribute('href', `/filament/types/${T.JADE}`)
    const sheet = screen.getByRole('table', { name: 'Price sheet' })
    expect(within(sheet).getByRole('link', { name: 'A00-W1' })).toHaveAttribute('href', `/filament/suppliers/${SUPPLIER.BAMBU}#product-${PRODUCT.JADE}`)
  })

  it('shows an archived product a link names, with "Show archived" off, and no other archived one (#6869)', async () => {
    await patchProduct(PRODUCT.JADE, { archived: true })
    await patchProduct(PRODUCT.ORANGE, { archived: true })
    renderPage(
      <Routes>
        <Route path="/filament/suppliers/:id" element={<SupplierPage />} />
      </Routes>,
      { route: `/filament/suppliers/${SUPPLIER.BAMBU}#product-${PRODUCT.JADE}` },
    )
    await heading('Bambu Lab')
    expect(screen.getByRole('checkbox', { name: 'Show archived products' })).not.toBeChecked()
    await waitFor(() => expect(productRow(PRODUCT.JADE)).toHaveAttribute('aria-current', 'location'))
    expect(productRow(PRODUCT.ORANGE)).toBeUndefined()
  })

  it('renders a product page as a link that opens safely', async () => {
    render()
    await heading('Bambu Lab')
    const link = within(productRow(PRODUCT.JADE)).getByRole('link', { name: /us\.store\.bambulab\.com/ })
    expect(link).toHaveAttribute('href', 'https://us.store.bambulab.com/products/pla-basic-filament')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
  })

  it('shows the seeded rise on the price sheet and a dash for a one-point product', async () => {
    render()
    await heading('Bambu Lab')
    const sheet = screen.getByRole('table', { name: 'Price sheet' })
    const rows = within(sheet).getAllByRole('row').filter((row) => row.dataset.product)
    const jade = rows.find((row) => row.dataset.product === PRODUCT.JADE)!
    expect(within(jade).getByText('+8.2 %')).toBeInTheDocument()
    const orange = rows.find((row) => row.dataset.product === PRODUCT.ORANGE)!
    expect(within(orange).getByLabelText('No change to show')).toBeInTheDocument()
  })

  it('adds a price as a decimal string with its date', async () => {
    const posted = recorded('POST', /\/prices$/)
    const { user } = render()
    await heading('Bambu Lab')
    await user.click(within(productRow(PRODUCT.JADE)).getByRole('button', { name: 'Add price for A00-W1' }))
    await user.type(await screen.findByLabelText('Price with tax for A00-W1'), '21.40')
    expect(screen.getByLabelText('Currency')).toHaveTextContent('USD')
    await user.click(screen.getByRole('button', { name: 'Add price' }))
    await waitFor(() => expect(posted).toHaveLength(1))
    expect(posted[0]).toEqual({ price: '21.40', on_date: expect.stringMatching(/^\d{4}-\d{2}-\d{2}$/) })
    expect((await listPrices(PRODUCT.JADE)).some((price) => price.price === '21.4000' || price.price === '21.40')).toBe(true)
    // The row reads the new newest price.
    await waitFor(() => expect(within(productRow(PRODUCT.JADE)).getByText('$21.40')).toBeInTheDocument())
  })

  it('refuses a price that is not a positive number', async () => {
    const posted = recorded('POST', /\/prices$/)
    const { user } = render()
    await heading('Bambu Lab')
    await user.click(within(productRow(PRODUCT.JADE)).getByRole('button', { name: 'Add price for A00-W1' }))
    await user.type(await screen.findByLabelText('Price with tax for A00-W1'), 'abc')
    await user.click(screen.getByRole('button', { name: 'Add price' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Enter the price as a number above zero')
    expect(posted).toEqual([])
  })

  it('adds a product by picking a type', async () => {
    const { user } = render(SUPPLIER.MICRO)
    await heading('Micro Center')
    await user.click(screen.getByRole('button', { name: 'Add product' }))
    await choose(user, await screen.findByRole('combobox', { name: 'Type' }), /BL-PLA-BASIC-JADE-WHITE/)
    await user.type(screen.getByLabelText('SKU'), 'INL-PLA-GRN')
    await user.click(screen.getAllByRole('button', { name: 'Add product' }).at(-1)!)
    await waitFor(async () => expect((await listProducts(SUPPLIER.MICRO)).some((product) => product.sku === 'INL-PLA-GRN' && product.type_id === T.JADE)).toBe(true))
    expect((await screen.findAllByText('INL-PLA-GRN')).length).toBeGreaterThan(0)
  })

  it('refuses a product page that is not http or https', async () => {
    const posted = recorded('POST', /\/products$/)
    const { user } = render(SUPPLIER.MICRO)
    await heading('Micro Center')
    await user.click(screen.getByRole('button', { name: 'Add product' }))
    await choose(user, await screen.findByRole('combobox', { name: 'Type' }), /BL-PLA-BASIC-JADE-WHITE/)
    await user.type(screen.getByLabelText('SKU'), 'X')
    await user.type(screen.getByLabelText('Product page'), 'javascript:x')
    await user.click(screen.getAllByRole('button', { name: 'Add product' }).at(-1)!)
    expect(await screen.findByRole('alert')).toHaveTextContent('The product page must start with http:// or https://.')
    expect(posted).toEqual([])
  })

  it('saves only the fields that changed and refuses a bad website first', async () => {
    const patched = recorded('PATCH', /\/suppliers\/[^/]+$/)
    const { user } = render()
    await heading('Bambu Lab')
    const website = screen.getByLabelText('Website')
    await user.clear(website)
    await user.type(website, 'javascript:x')
    await user.click(screen.getByRole('button', { name: 'Save supplier' }))
    expect(await screen.findByText(/must start with http/)).toBeInTheDocument()
    expect(patched).toEqual([])
    await user.clear(website)
    await user.type(website, 'https://bambulab.com')
    await user.click(screen.getByRole('button', { name: 'Save supplier' }))
    await waitFor(() => expect(patched).toEqual([{ website: 'https://bambulab.com' }]))
    expect(await screen.findByRole('status')).toHaveTextContent('Saved')
  })

  it('holds navigation while there are unsaved edits', async () => {
    const { user } = render()
    await heading('Bambu Lab')
    await user.type(screen.getByLabelText('Note'), 'x')
    await user.click(screen.getByRole('link', { name: /Suppliers/ }))
    expect(await screen.findByText('Leave without saving?')).toBeInTheDocument()
    expect(screen.queryByText('Elsewhere')).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Leave without saving' }))
    expect(await screen.findByText('Elsewhere')).toBeInTheDocument()
  })

  it('treats whitespace typed in a field as no change: no leave prompt and nothing to save', async () => {
    const patched = recorded('PATCH', /\/suppliers\/[^/]+$/)
    const { user } = render()
    await heading('Bambu Lab')
    await user.type(screen.getByLabelText('Note'), '   ')
    await user.click(screen.getByRole('link', { name: /Suppliers/ }))
    expect(await screen.findByText('Elsewhere')).toBeInTheDocument()
    expect(screen.queryByText('Leave without saving?')).toBeNull()
    expect(patched).toEqual([])
  })

  it('lists the items it offers besides filament, each with its link (#7067)', async () => {
    const bearing = seedItem({ kind_id: 'hardware', name: '608 bearing' })
    const offer = seedOffer({ item_id: bearing.id, supplier_id: SUPPLIER.BAMBU, sku: 'BRG-608', url: 'https://us.store.bambulab.com/products/bearing', pack_size: '10' }, { price: '4.50' })
    render()
    await heading('Bambu Lab')
    const offers = await screen.findByRole('table', { name: 'Other offers' })
    expect(within(offers).getByRole('link', { name: '608 bearing' })).toHaveAttribute('href', `/inventory/items/${bearing.id}#offer-${offer.id}`)
    const link = within(offers).getByRole('link', { name: 'us.store.bambulab.com/products/bearing' })
    expect(link).toHaveAttribute('href', 'https://us.store.bambulab.com/products/bearing')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    expect(offers).toHaveTextContent('BRG-608')
  })

  it("says when the supplier's other offers cannot be read (#7067)", async () => {
    server.use(http.get('/api/v1/inventory/suppliers/:id/offers', () => HttpResponse.json({ detail: 'down' }, { status: 500 })))
    render()
    await heading('Bambu Lab')
    expect(await screen.findByText(/The other offers could not be loaded/)).toHaveAttribute('role', 'alert')
  })
})
