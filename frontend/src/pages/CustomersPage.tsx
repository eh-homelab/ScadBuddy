import { useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router'
import { api } from '../api/client'
import type { Customer } from '../api/types'
import { CustomerForm } from '../components/orders/CustomerForm'
import { refusalText } from '../components/orders/refusal'
import { DataTable, type Column } from '../components/records/DataTable'
import { FilterBar } from '../components/records/FilterBar'
import { RecordDrawer } from '../components/records/RecordDrawer'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { useAsync } from '../lib/useAsync'

/** `/customers` (spec 2026-10-18 §3.1, §9): the list, searched by `q`, and a new customer. */
export function CustomersPage() {
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const q = params.get('q') ?? ''
  const archived = params.get('archived') === '1'
  const list = useAsync(() => api.customers({ q, includeArchived: archived, limit: 500 }), [q, archived])
  const [adding, setAdding] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  function setQuery(next: { q?: string; archived?: boolean }, typing = false) {
    const out = new URLSearchParams()
    const text = (next.q ?? q).trim()
    if (text) out.set('q', text)
    if (next.archived ?? archived) out.set('archived', '1')
    setParams(out, { replace: typing })
  }

  const columns: Column<Customer>[] = [
    {
      key: 'name',
      header: 'Customer',
      className: 'break-words',
      cell: (c) => (
        <Link to={`/customers/${c.id}`} className="font-medium text-foreground underline-offset-4 hover:underline">
          {c.display_name}
          {c.archived_at && <span className="ml-1 text-xs text-muted-foreground">(archived)</span>}
        </Link>
      ),
    },
    { key: 'company', header: 'Company', className: 'break-words text-muted-foreground', cell: (c) => c.company ?? '' },
    {
      key: 'contacts',
      header: 'Contact',
      className: 'break-all text-muted-foreground',
      cell: (c) => (c.contacts.find((k) => k.primary) ?? c.contacts[0])?.value ?? '',
    },
    { key: 'tags', header: 'Tags', className: 'text-muted-foreground', cell: (c) => c.tags.join(', ') },
  ]

  return (
    <div className="h-full overflow-y-auto" data-testid="customers-page">
      <div className="mx-auto flex max-w-6xl flex-col gap-4 px-4 py-6">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h1 className="text-xl font-semibold tracking-tight">Customers</h1>
          <Button onClick={() => setAdding(true)}>New customer</Button>
        </div>
        <FilterBar
          label="Customer filters"
          search={{ value: q, onChange: (next) => setQuery({ q: next }, true), placeholder: 'Search customers' }}
          chips={[{ key: 'archived', label: 'Show', chips: [{ value: 'archived', label: 'Archived too' }], selected: archived ? ['archived'] : [], onChange: (values) => setQuery({ archived: values.includes('archived') }) }]}
          filtered={Boolean(q) || archived}
          onClear={() => setParams(new URLSearchParams())}
        />
        {list.error ? (
          <p role="alert" className="text-[13px] text-warn">
            Could not load customers: {list.error.message}
          </p>
        ) : list.loading ? (
          <div role="status" aria-label="Loading customers" className="flex flex-col gap-2">
            {Array.from({ length: 3 }, (_, i) => (
              <Skeleton key={i} className="h-9 w-full" />
            ))}
          </div>
        ) : (
          <DataTable label="Customers" columns={columns} rows={list.data ?? []} rowKey={(c) => c.id} rowTestId={(c) => `customer-row-${c.id}`} empty="No customers match." />
        )}
      </div>
      {adding && (
        <RecordDrawer title="New customer" onClose={() => setAdding(false)}>
          <CustomerForm
            submitLabel="Create customer"
            busy={busy}
            error={error}
            onSubmit={(value) => {
              setBusy(true)
              setError(null)
              api
                .createCustomer(value)
                .then((created) => {
                  setAdding(false)
                  void navigate(`/customers/${created.id}`)
                })
                .catch((err: unknown) => setError(refusalText(err)))
                .finally(() => setBusy(false))
            }}
          />
        </RecordDrawer>
      )}
    </div>
  )
}
