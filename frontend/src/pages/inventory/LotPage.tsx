import { Navigate, useParams } from 'react-router'
import { ApiError } from '../../api/client'
import { inventoryPaths } from '../../lib/inventoryLinks'
import { useAsync } from '../../lib/useAsync'
import { inventoryApi } from './api'

/** #7067 — `/inventory/lots/:id`: a lot has no page of its own, so this opens its item's page at the lot's row. */
export function LotPage() {
  const { id = '' } = useParams()
  const lot = useAsync(() => inventoryApi.lot(id), [id])
  if (lot.data) return <Navigate to={inventoryPaths.itemLot(lot.data.item_id, lot.data.id)} replace />
  return (
    <div className="mx-auto max-w-6xl px-4 py-6 text-[13px]">
      <h1 className="text-lg font-semibold tracking-tight">Lot</h1>
      {lot.error ? (
        <p role="alert" className="mt-2 text-warn">
          {lot.error instanceof ApiError && lot.error.status === 404 ? 'No such lot.' : `The lot could not be loaded: ${lot.error.message}`}
        </p>
      ) : (
        <p className="mt-2 text-muted-foreground">Opening the lot…</p>
      )}
    </div>
  )
}
