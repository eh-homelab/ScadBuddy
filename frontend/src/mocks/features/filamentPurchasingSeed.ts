import { M, spoolId, T } from './filament/seed'

/**
 * #6266 — the seed of `filamentPurchasing.ts`: 3 suppliers, 5 products with price
 * histories, two drafts and two confirmed invoices, and a shopping list of 4 with one
 * purchased. The ids are exported for the e2e (`e2e/page-titles.spec.ts`), so this file
 * imports only the Filament seed's ids. The module works out each invoice's tax, totals
 * and costs from these inputs, and writes the price points a confirmed line made.
 *
 * Every `.ts` under `features/` is loaded as a mock feature and must export `handlers`
 * (`mocks/features.ts`): this one answers no route of its own.
 */
export const handlers = []

const id = (prefix: string, n: number) => `${prefix}-0000-4000-8000-${String(n).padStart(12, '0')}`

export const SUPPLIER = {
  BAMBU: id('62660000', 1),
  MICRO: id('62660000', 2),
  AMAZON: id('62660000', 3),
}
export const PRODUCT = {
  JADE: id('62660001', 1),
  PINK: id('62660001', 2),
  ORANGE: id('62660001', 3),
  INLAND: id('62660001', 4),
  TPU: id('62660001', 5),
}
export const INVOICE = {
  DRAFT_A: id('62660003', 1),
  DRAFT_B: id('62660003', 2),
  CONFIRMED_A: id('62660003', 3),
  CONFIRMED_B: id('62660003', 4),
}
export const LINE = {
  DRAFT_A1: id('62660004', 1),
  DRAFT_A2: id('62660004', 2),
  DRAFT_B1: id('62660004', 3),
  DRAFT_B2: id('62660004', 4),
  CONFIRMED_A1: id('62660004', 5),
  CONFIRMED_A2: id('62660004', 6),
  CONFIRMED_B1: id('62660004', 7),
}
export const ITEM = {
  PINK: id('62660005', 1),
  ORANGE: id('62660005', 2),
  TPU: id('62660005', 3),
  CLEAR: id('62660005', 4),
}

export const AT = '2026-11-15T07:00:00Z'

/** The shop's currency the seed's invoices are in. */
export const SHOP_CURRENCY = 'USD'

export interface SeedLine {
  id: string
  description: string
  sku: string | null
  quantity: number
  unit_price: string
  type_id: string | null
  supplier_product_id: string | null
  /** A new type the line proposes (`SpoolTypeCreate`), made when the invoice is confirmed. */
  proposed_type?: Record<string, unknown>
  /** The spools a confirmed line made: seeded spools of the Filament mock. */
  spool_ids?: string[]
}

export interface SeedInvoice {
  id: string
  supplier_id: string
  number: string | null
  invoice_date: string
  subtotal: string
  shipping: string
  tax: string
  total: string
  status: 'draft' | 'confirmed'
  note: string | null
  file: { name: string; media_type: string; size: number } | null
  lines: SeedLine[]
}

export function seedInput() {
  const suppliers = [
    { id: SUPPLIER.BAMBU, name: 'Bambu Lab', website: 'https://us.store.bambulab.com', customer_number: 'BL-208841', note: null },
    { id: SUPPLIER.MICRO, name: 'Micro Center', website: 'https://www.microcenter.com', customer_number: null, note: 'Pick up in store; they price-match.' },
    { id: SUPPLIER.AMAZON, name: 'Amazon', website: 'https://www.amazon.com', customer_number: null, note: null },
  ]
  const products = [
    { id: PRODUCT.JADE, supplier_id: SUPPLIER.BAMBU, type_id: T.JADE, sku: 'A00-W1', product_url: 'https://us.store.bambulab.com/products/pla-basic-filament', pack_size: 1, note: null },
    { id: PRODUCT.PINK, supplier_id: SUPPLIER.BAMBU, type_id: T.PINK, sku: 'A00-R3', product_url: null, pack_size: 1, note: null },
    { id: PRODUCT.ORANGE, supplier_id: SUPPLIER.BAMBU, type_id: T.ORANGE, sku: 'G02-A0', product_url: null, pack_size: 1, note: null },
    { id: PRODUCT.INLAND, supplier_id: SUPPLIER.MICRO, type_id: T.INLAND_BLACK, sku: 'INL-PLA-BLK', product_url: null, pack_size: 1, note: null },
    { id: PRODUCT.TPU, supplier_id: SUPPLIER.AMAZON, type_id: T.TPU_RED, sku: 'B0SUNLUTPU95', product_url: null, pack_size: 2, note: 'Sold as a pack of two 500 g spools.' },
  ]
  /** Manual price points, per pack with tax; the confirmed invoices' lines add theirs. */
  const manualPrices = [
    { product_id: PRODUCT.JADE, price: '14.99', on_date: '2026-08-02' },
    { product_id: PRODUCT.JADE, price: '15.99', on_date: '2026-09-14' },
    { product_id: PRODUCT.PINK, price: '14.99', on_date: '2026-08-02' },
    { product_id: PRODUCT.ORANGE, price: '18.99', on_date: '2026-09-14' },
    { product_id: PRODUCT.INLAND, price: '13.49', on_date: '2026-09-01' },
    { product_id: PRODUCT.TPU, price: '28.99', on_date: '2026-09-20' },
  ]
  const invoices: SeedInvoice[] = [
    {
      id: INVOICE.DRAFT_A,
      supplier_id: SUPPLIER.BAMBU,
      number: 'US-452318',
      invoice_date: '2026-11-12',
      subtotal: '87.95',
      shipping: '0',
      tax: '7.25',
      total: '95.20',
      status: 'draft',
      note: null,
      file: { name: 'bambu-US-452318.pdf', media_type: 'application/pdf', size: 48213 },
      lines: [
        { id: LINE.DRAFT_A1, description: 'PETG HF Translucent Orange', sku: 'G02-A0', quantity: 2, unit_price: '19.99', type_id: T.ORANGE, supplier_product_id: PRODUCT.ORANGE },
        { id: LINE.DRAFT_A2, description: 'PLA Basic Jade White', sku: 'A00-W1', quantity: 3, unit_price: '15.99', type_id: T.JADE, supplier_product_id: PRODUCT.JADE },
      ],
    },
    {
      id: INVOICE.DRAFT_B,
      supplier_id: SUPPLIER.MICRO,
      number: null,
      invoice_date: '2026-11-15',
      subtotal: '41.97',
      shipping: '0',
      tax: '0',
      total: '41.97',
      status: 'draft',
      note: 'Photo of the receipt; no number printed.',
      file: null,
      lines: [
        { id: LINE.DRAFT_B1, description: 'Inland PLA Black 1 kg', sku: 'INL-PLA-BLK', quantity: 2, unit_price: '13.99', type_id: T.INLAND_BLACK, supplier_product_id: PRODUCT.INLAND },
        {
          id: LINE.DRAFT_B2,
          description: 'Inland PLA Blue 1 kg',
          sku: 'INL-PLA-BLU',
          quantity: 1,
          unit_price: '13.99',
          type_id: null,
          supplier_product_id: null,
          proposed_type: {
            code: 'IN-PLA-BLUE',
            material_id: M.PLA,
            rgba: '#1F5FBFFF',
            colour_name: 'Blue',
            label_weight_g: 1000,
            diameter_mm: '1.75',
          },
        },
      ],
    },
    {
      id: INVOICE.CONFIRMED_A,
      supplier_id: SUPPLIER.BAMBU,
      number: 'US-441907',
      invoice_date: '2026-10-08',
      subtotal: '31.98',
      shipping: '0',
      tax: '2.63',
      total: '34.61',
      status: 'confirmed',
      note: null,
      file: { name: 'bambu-US-441907.pdf', media_type: 'application/pdf', size: 39870 },
      lines: [
        { id: LINE.CONFIRMED_A1, description: 'PLA Basic Jade White', sku: 'A00-W1', quantity: 1, unit_price: '15.99', type_id: T.JADE, supplier_product_id: PRODUCT.JADE, spool_ids: [spoolId(1)] },
        { id: LINE.CONFIRMED_A2, description: 'PLA Basic Hot Pink', sku: 'A00-R3', quantity: 1, unit_price: '15.99', type_id: T.PINK, supplier_product_id: PRODUCT.PINK, spool_ids: [spoolId(3)] },
      ],
    },
    {
      id: INVOICE.CONFIRMED_B,
      supplier_id: SUPPLIER.AMAZON,
      number: '112-4471902-5530',
      invoice_date: '2026-10-21',
      subtotal: '14.99',
      shipping: '4.99',
      tax: '1.24',
      total: '21.22',
      status: 'confirmed',
      note: null,
      file: null,
      lines: [
        { id: LINE.CONFIRMED_B1, description: 'SUNLU TPU 95A Red, 2 x 500 g', sku: 'B0SUNLUTPU95', quantity: 1, unit_price: '14.99', type_id: T.TPU_RED, supplier_product_id: PRODUCT.TPU, spool_ids: [spoolId(10)] },
      ],
    },
  ]
  const shopping = [
    { id: ITEM.PINK, type_id: T.PINK, material: 'PLA', subtype: 'Basic', brand: 'Bambu Lab', colour_name: 'Hot Pink', quantity_spools: 2, note: 'Spool #3 ran out on 2026-11-14 18:42 UTC.', status: 'pending' as const, purchased_at: null, empty_spool_id: spoolId(3) },
    { id: ITEM.ORANGE, type_id: T.ORANGE, material: 'PETG', subtype: 'HF', brand: 'Bambu Lab', colour_name: 'Translucent Orange', quantity_spools: 1, note: 'Waiting for the sale.', status: 'pending' as const, purchased_at: null, empty_spool_id: null },
    { id: ITEM.TPU, type_id: null, material: 'TPU', subtype: '95A', brand: null, colour_name: 'White', quantity_spools: 1, note: null, status: 'pending' as const, purchased_at: null, empty_spool_id: null },
    { id: ITEM.CLEAR, type_id: T.POLY_CLEAR, material: 'PETG', subtype: null, brand: 'Polymaker', colour_name: 'Clear', quantity_spools: 1, note: null, status: 'purchased' as const, purchased_at: '2026-11-10T16:20:00Z', empty_spool_id: null },
  ]
  return { suppliers, products, manualPrices, invoices, shopping }
}
