import { ArchiveIcon, ArchiveRestoreIcon, ChevronLeftIcon } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { Link, useParams } from 'react-router'
import { ApiError } from '../../api/client'
import { listTypes, type SpoolType } from '../../api/filament'
import {
  createProduct,
  getPriceSheet,
  getSupplier,
  listProducts,
  patchSupplier,
  type PriceSheetRow,
  type Supplier,
  type SupplierProduct,
} from '../../api/filamentPurchasing'
import { PriceSheet } from '../../components/filament/purchasing/PriceSheet'
import { ProductsTable } from '../../components/filament/purchasing/ProductsTable'
import { BLANK_SUPPLIER, draftOfSupplier, supplierBody, supplierError } from '../../components/filament/purchasing/supplierDraft'
import { SupplierFields } from '../../components/filament/purchasing/SupplierFields'
import { useMoney } from '../../components/filament/purchasing/useMoney'
import { webUrlError } from '../../components/filament/purchasing/webUrl'
import { RecordDrawer } from '../../components/records/RecordDrawer'
import { AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useAdvanced } from '../../lib/advanced'
import { useLeaveGuard } from '../../lib/useLeaveGuard'

/** Remembered per browser: whether archived products are listed. */
export const SHOW_ARCHIVED_PRODUCTS_KEY = 'scadbuddy.filament.supplier.showArchivedProducts'

const message = (cause: unknown, fallback: string) => (cause instanceof ApiError ? cause.detail : fallback)

interface Loaded {
  supplier: Supplier
  products: SupplierProduct[]
  sheet: PriceSheetRow[]
}

/**
 * #6266 — one supplier at `/filament/suppliers/:id`: its details (unsaved edits hold
 * navigation), its products with their newest prices and an inline "Add price", and its price
 * sheet. A product is added by picking one of the types.
 */
export function SupplierPage() {
  const { id = '' } = useParams()
  const money = useMoney()
  const [showArchived, toggleArchived] = useAdvanced(SHOW_ARCHIVED_PRODUCTS_KEY)
  const [data, setData] = useState<Loaded | null>(null)
  const [types, setTypes] = useState<SpoolType[]>([])
  const [draft, setDraft] = useState(BLANK_SUPPLIER)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [adding, setAdding] = useState(false)
  const latest = useRef(0)

  const read = useCallback(
    async (resetDraft: boolean) => {
      const mine = ++latest.current
      try {
        const [supplier, products, sheet] = await Promise.all([getSupplier(id), listProducts(id, { includeArchived: showArchived }), getPriceSheet(id)])
        if (mine !== latest.current) return
        setData({ supplier, products, sheet })
        if (resetDraft) setDraft(draftOfSupplier(supplier))
        setLoadError(null)
      } catch (cause) {
        if (mine !== latest.current) return
        setLoadError(cause instanceof ApiError && cause.status === 404 ? 'That supplier is gone. Pick another from the Suppliers tab.' : message(cause, 'The supplier could not be loaded. Try again.'))
      }
    },
    [id, showArchived],
  )

  // The first read fills the form; a read after a price or a product keeps the person's edits.
  const filled = useRef<string | null>(null)
  useEffect(() => {
    const first = filled.current !== id
    filled.current = id
    if (first) setData(null)
    void read(first)
  }, [id, read])

  useEffect(() => {
    let live = true
    listTypes({ includeArchived: true }).then(
      (rows) => live && setTypes(rows),
      () => undefined,
    )
    return () => {
      live = false
    }
  }, [])

  const typeMap = useMemo(() => new Map(types.map((type) => [type.id, type])), [types])
  const base = useMemo(() => (data ? draftOfSupplier(data.supplier) : BLANK_SUPPLIER), [data])
  const dirty = JSON.stringify(draft) !== JSON.stringify(base)
  const guard = useLeaveGuard(dirty)

  async function save() {
    const refused = supplierError(draft)
    if (refused) {
      setError(refused)
      return
    }
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const body = supplierBody(draft)
      // Only what changed; an emptied field is cleared with null.
      const patch: Record<string, string | null> = {}
      if (body.name !== base.name) patch.name = body.name
      for (const key of ['website', 'customer_number', 'note'] as const) {
        if (draft[key].trim() !== base[key]) patch[key] = body[key] ?? null
      }
      const saved = await patchSupplier(id, patch)
      setData((was) => (was ? { ...was, supplier: saved } : was))
      setDraft(draftOfSupplier(saved))
      setNotice('Saved')
    } catch (cause) {
      setError(message(cause, 'The supplier could not be saved. Try again.'))
    } finally {
      setBusy(false)
    }
  }

  async function archive(next: boolean) {
    setBusy(true)
    setError(null)
    try {
      const saved = await patchSupplier(id, { archived: next })
      setData((was) => (was ? { ...was, supplier: saved } : was))
      setNotice(next ? 'Archived' : 'Restored')
    } catch (cause) {
      setError(message(cause, 'The supplier could not be changed. Try again.'))
    } finally {
      setBusy(false)
    }
  }

  if (loadError) {
    return (
      <Shell>
        <h1 className="text-lg font-semibold tracking-tight">Supplier</h1>
        <p role="alert" className="mt-2 text-[13px] text-warn">
          {loadError}
        </p>
      </Shell>
    )
  }
  if (!data) {
    return (
      <Shell>
        <h1 className="text-lg font-semibold tracking-tight">Supplier</h1>
        <p className="mt-2 text-[13px] text-muted-foreground">Loading the supplier…</p>
      </Shell>
    )
  }

  const { supplier } = data
  return (
    <Shell>
      <header className="mb-5 flex flex-wrap items-center gap-3">
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-lg font-semibold tracking-tight">{supplier.name}</h1>
          <p className="text-[13px] text-muted-foreground">
            {supplier.archived_at ? 'Archived. Restore it to buy from it again.' : `${supplier.product_count} ${supplier.product_count === 1 ? 'product' : 'products'}.`}
          </p>
        </div>
        <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => void archive(!supplier.archived_at)}>
          {supplier.archived_at ? <ArchiveRestoreIcon /> : <ArchiveIcon />}
          {supplier.archived_at ? 'Restore' : 'Archive'}
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          disabled={busy || !dirty}
          onClick={() => {
            setDraft(base)
            setError(null)
          }}
        >
          Discard changes
        </Button>
        <Button type="button" size="sm" disabled={busy || !dirty} onClick={() => void save()}>
          Save supplier
        </Button>
      </header>
      <p role="status" className="mb-2 min-h-5 text-[13px] text-muted-foreground">
        {notice}
      </p>
      {error && (
        <p role="alert" className="mb-4 rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-[13px] text-warn">
          {error}
        </p>
      )}

      <section aria-label="Supplier details" className="mb-8">
        <SupplierFields
          draft={draft}
          onChange={(next) => {
            setNotice(null)
            setDraft(next)
          }}
        />
      </section>

      <section aria-label="Products" className="mb-8 flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-3">
          <h2 className="text-sm font-semibold tracking-tight">Products</h2>
          <label className="flex items-center gap-2 text-[13px] text-muted-foreground">
            <Checkbox checked={showArchived} aria-label="Show archived products" onCheckedChange={toggleArchived} />
            Show archived
          </label>
          <Button type="button" size="sm" className="ml-auto" onClick={() => setAdding(true)}>
            Add product
          </Button>
        </div>
        <ProductsTable products={data.products} types={typeMap} money={money} onChanged={() => void read(false)} />
      </section>

      <section aria-label="Price sheet" className="flex flex-col gap-2">
        <h2 className="text-sm font-semibold tracking-tight">Price sheet</h2>
        <p className="text-[12px] text-muted-foreground">The newest and the previous price of each product. Prices are per pack with tax in and shipping out; the change is in the price per kg.</p>
        <PriceSheet rows={data.sheet} types={typeMap} money={money} />
      </section>

      {adding && (
        <AddProductSheet
          supplierId={id}
          types={types.filter((type) => !type.archived_at)}
          onClose={() => setAdding(false)}
          onAdded={() => {
            setAdding(false)
            void read(false)
          }}
        />
      )}

      <AlertDialog open={guard.pending !== null} onOpenChange={(next) => !next && guard.stay()}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Leave without saving?</AlertDialogTitle>
            <AlertDialogDescription>Your changes to this supplier have not been saved, and will be lost. Save first to keep them.</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Stay</AlertDialogCancel>
            <Button type="button" variant="destructive" onClick={guard.leave}>
              Leave without saving
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Shell>
  )
}

function AddProductSheet({ supplierId, types, onClose, onAdded }: { supplierId: string; types: SpoolType[]; onClose: () => void; onAdded: () => void }) {
  const [typeId, setTypeId] = useState('')
  const [sku, setSku] = useState('')
  const [pack, setPack] = useState('1')
  const [url, setUrl] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(event: FormEvent) {
    event.preventDefault()
    const size = /^\d+$/.test(pack.trim()) ? Number(pack) : 0
    const refused = !typeId
      ? 'Pick the type this product is.'
      : !sku.trim()
        ? 'Enter the supplier’s SKU.'
        : size < 1
          ? 'The pack size is a whole number of spools, at least 1.'
          : webUrlError(url, 'The product page')
    if (refused) {
      setError(refused)
      return
    }
    setBusy(true)
    setError(null)
    try {
      await createProduct(supplierId, { type_id: typeId, sku: sku.trim(), pack_size: size, ...(url.trim() ? { product_url: url.trim() } : {}) })
      onAdded()
    } catch (cause) {
      setError(message(cause, 'The product could not be added. Try again.'))
      setBusy(false)
    }
  }

  return (
    <RecordDrawer
      title="Add product"
      onClose={onClose}
      footer={
        <Button type="submit" form="add-product" disabled={busy}>
          Add product
        </Button>
      }
    >
      <form id="add-product" className="flex flex-col gap-3" onSubmit={(event) => void submit(event)} noValidate>
        <div className="flex flex-col gap-1">
          <Label htmlFor="product-type" className="text-[13px]">Type</Label>
          <Select value={typeId} onValueChange={setTypeId}>
            <SelectTrigger id="product-type" aria-label="Type" className="w-full text-[13px]">
              <SelectValue placeholder="Pick a type" />
            </SelectTrigger>
            <SelectContent>
              {types.map((type) => (
                <SelectItem key={type.id} value={type.id}>
                  {type.code}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div className="flex flex-col gap-1">
            <Label htmlFor="product-sku" className="text-[13px]">SKU</Label>
            <Input id="product-sku" value={sku} onChange={(event) => setSku(event.target.value)} />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="product-pack" className="text-[13px]">Pack size</Label>
            <Input id="product-pack" inputMode="numeric" value={pack} onChange={(event) => setPack(event.target.value)} />
          </div>
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor="product-url" className="text-[13px]">Product page</Label>
          <Input id="product-url" inputMode="url" placeholder="https://" value={url} onChange={(event) => setUrl(event.target.value)} />
        </div>
        {error && (
          <p role="alert" className="text-[13px] text-warn">
            {error}
          </p>
        )}
      </form>
    </RecordDrawer>
  )
}

function Shell({ children }: { children: ReactNode }) {
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-6xl px-4 py-6">
        <Link to="/filament/suppliers" className="mb-3 inline-flex items-center gap-1 text-[13px] text-muted-foreground hover:text-foreground">
          <ChevronLeftIcon className="size-3.5" /> Suppliers
        </Link>
        {children}
      </div>
    </div>
  )
}
