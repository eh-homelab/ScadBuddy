import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { Link } from 'react-router'
import { createSupplier, getSupplierStats, listSuppliers, patchSupplier, type Supplier, type SupplierStats } from '../../api/filamentPurchasing'
import { TD, TH } from '../../components/filament/formClasses'
import { SafeLink } from '../../components/filament/purchasing/SafeLink'
import { BLANK_SUPPLIER, supplierBody, supplierError } from '../../components/filament/purchasing/supplierDraft'
import { SupplierFields } from '../../components/filament/purchasing/SupplierFields'
import { useMoney } from '../../components/filament/purchasing/useMoney'
import { RecordDrawer } from '../../components/records/RecordDrawer'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { useAdvanced } from '../../lib/advanced'
import { message } from '../../components/filament/purchasing/priceFormat'

/** Remembered per browser: whether archived suppliers are listed. */
export const SHOW_ARCHIVED_KEY = 'scadbuddy.filament.suppliers.showArchived'


interface Loaded {
  suppliers: Supplier[]
  stats: Map<string, SupplierStats>
}

/**
 * #6266 — the Suppliers tab: where filament is bought, how many products each has, and the
 * spools and spend of its confirmed invoices. A row opens the supplier's page. "Show
 * archived" is remembered.
 */
export function SuppliersTab() {
  const money = useMoney()
  const [showArchived, toggleArchived] = useAdvanced(SHOW_ARCHIVED_KEY)
  const [data, setData] = useState<Loaded | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)
  const latest = useRef(0)

  const load = useCallback(async () => {
    const mine = ++latest.current
    try {
      const [suppliers, stats] = await Promise.all([listSuppliers({ includeArchived: true }), getSupplierStats()])
      if (mine !== latest.current) return
      setData({ suppliers, stats: new Map(stats.map((row) => [row.supplier_id, row])) })
      setError(null)
    } catch (cause) {
      if (mine === latest.current) setError(message(cause, 'The suppliers could not be loaded. Try again.'))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const shown = useMemo(
    () => (data?.suppliers ?? []).filter((supplier) => showArchived || !supplier.archived_at).sort((a, b) => a.name.localeCompare(b.name)),
    [data, showArchived],
  )

  async function archive(supplier: Supplier) {
    setError(null)
    try {
      await patchSupplier(supplier.id, { archived: !supplier.archived_at })
      await load()
    } catch (cause) {
      setError(message(cause, 'The supplier could not be changed. Try again.'))
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3 text-[13px]">
        <label className="flex items-center gap-2 text-muted-foreground">
          <Checkbox checked={showArchived} aria-label="Show archived suppliers" onCheckedChange={toggleArchived} />
          Show archived
        </label>
        <Button type="button" size="sm" className="ml-auto" onClick={() => setAdding(true)}>
          Add supplier
        </Button>
      </div>
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
      {!data && !error && <p className="text-[13px] text-muted-foreground">Loading suppliers…</p>}
      {data && shown.length === 0 && <p className="text-[13px] text-muted-foreground">No suppliers yet. Add the shops you buy filament from.</p>}
      {data && shown.length > 0 && (
        <div className="relative overflow-x-auto rounded-md border">
          <table aria-label="Suppliers" className="w-full text-left text-[13px]">
            <thead className="bg-muted/50 text-muted-foreground">
              <tr>
                <th scope="col" className={TH}>Name</th>
                <th scope="col" className={TH}>Website</th>
                <th scope="col" className={TH}>Customer number</th>
                <th scope="col" className={`${TH} text-right`}>Products</th>
                <th scope="col" className={`${TH} text-right`}>Spools bought</th>
                <th scope="col" className={`${TH} text-right`}>Spend</th>
                <th scope="col" className={TH}>
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {shown.map((supplier) => {
                const stat = data.stats.get(supplier.id)
                return (
                  <tr key={supplier.id} data-supplier={supplier.id} className={`border-t ${supplier.archived_at ? 'text-muted-foreground' : ''}`}>
                    <td className={TD}>
                      <Link to={`/filament/suppliers/${supplier.id}`} className="font-medium underline-offset-4 hover:underline">
                        {supplier.name}
                      </Link>
                      {supplier.archived_at && <span className="ml-2 text-[12px]">Archived</span>}
                    </td>
                    <td className={TD}>
                      <SafeLink url={supplier.website} />
                    </td>
                    <td className={TD}>{supplier.customer_number ?? <span className="text-muted-foreground">—</span>}</td>
                    <td className={`${TD} text-right tabular-nums`}>{supplier.product_count}</td>
                    <td className={`${TD} text-right tabular-nums`}>{stat?.spool_count ?? 0}</td>
                    <td className={`${TD} text-right tabular-nums`}>{stat ? money.format(stat.spend) : <span className="text-muted-foreground">—</span>}</td>
                    <td className={`${TD} text-right`}>
                      <Button type="button" variant="ghost" size="sm" aria-label={`${supplier.archived_at ? 'Restore' : 'Archive'} ${supplier.name}`} onClick={() => void archive(supplier)}>
                        {supplier.archived_at ? 'Restore' : 'Archive'}
                      </Button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
      {adding && (
        <AddSupplierSheet
          onClose={() => setAdding(false)}
          onAdded={() => {
            setAdding(false)
            void load()
          }}
        />
      )}
    </div>
  )
}

function AddSupplierSheet({ onClose, onAdded }: { onClose: () => void; onAdded: () => void }) {
  const [draft, setDraft] = useState(BLANK_SUPPLIER)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(event: FormEvent) {
    event.preventDefault()
    const refused = supplierError(draft)
    if (refused) {
      setError(refused)
      return
    }
    setBusy(true)
    setError(null)
    try {
      await createSupplier(supplierBody(draft))
      onAdded()
    } catch (cause) {
      setError(message(cause, 'The supplier could not be added. Try again.'))
      setBusy(false)
    }
  }

  return (
    <RecordDrawer
      title="Add supplier"
      onClose={onClose}
      footer={
        <Button type="submit" form="add-supplier" disabled={busy}>
          Add supplier
        </Button>
      }
    >
      <form id="add-supplier" className="flex flex-col gap-3" onSubmit={(event) => void submit(event)} noValidate>
        <SupplierFields
          draft={draft}
          onChange={(next) => {
            setDraft(next)
            setError(null)
          }}
        />
        {error && (
          <p role="alert" className="text-[13px] text-warn">
            {error}
          </p>
        )}
      </form>
    </RecordDrawer>
  )
}
