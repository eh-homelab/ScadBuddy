import { useAsync } from '../../lib/useAsync'
import { inventoryApi } from '../../pages/inventory/api'

/** A part an order item or a model rule may name: an inventory item of a kind tracked in lots (#7067). */
export type PartChoice = { id: string; name: string; archived: boolean }

/**
 * The inventory's items tracked in lots, archived ones too: a row or a rule that names an
 * archived item still shows its name. A picker offers the unarchived ones only (`PartSelect`).
 */
export function usePartItems() {
  return useAsync(
    async () =>
      (await inventoryApi.items({ archived: true })).filter((item) => item.tracking === 'lot').map((item): PartChoice => ({ id: item.id, name: item.name, archived: !!item.archived_at })),
    [],
  )
}
