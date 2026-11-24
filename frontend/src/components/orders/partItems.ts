import { useAsync } from '../../lib/useAsync'
import { inventoryApi } from '../../pages/inventory/api'

/** A part an order item or a model rule may name: an inventory item of a kind tracked in lots (#7067). */
export type PartChoice = { id: string; name: string }

/** The inventory's items tracked in lots, by name: the parts an order item or a rule may name. */
export function usePartItems() {
  return useAsync(async () => (await inventoryApi.items()).filter((item) => item.tracking === 'lot').map((item): PartChoice => ({ id: item.id, name: item.name })), [])
}
