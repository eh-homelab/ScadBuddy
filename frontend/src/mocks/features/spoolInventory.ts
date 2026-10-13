import { HttpResponse, http } from 'msw'
import type { InventorySpool } from '../../api/types'

/**
 * #2351 — Bambuddy's spool inventory, from the recording: a priced spool loaded in
 * the AMS, an unpriced one on the shelf, one in a storage location, and an archived
 * spool that is listed only when asked for.
 */
export const SPOOL_INVENTORY: InventorySpool[] = [
  {
    id: 2,
    material: 'PLA',
    subtype: 'Basic',
    brand: 'Bambu Lab',
    color_name: 'Hot Pink',
    rgba: 'F5547CFF',
    label_weight: 1000,
    weight_used: 70,
    remaining_g: 930,
    cost_per_kg: 15.91,
    archived: false,
    loaded: null,
  },
  {
    id: 15,
    material: 'PETG',
    brand: 'Insignia',
    color_name: 'Clear',
    rgba: '00000000',
    label_weight: 1000,
    weight_used: 600,
    remaining_g: 400,
    cost_per_kg: 15.96,
    archived: false,
    loaded: { printer_id: 1, printer_name: '3DP-31B-598', ams_id: 0, tray_id: 3 },
  },
  {
    id: 1,
    material: 'PLA',
    subtype: 'Basic',
    brand: 'Bambu Lab',
    color_name: 'Mistletoe Green',
    rgba: '3F8E43FF',
    label_weight: 1000,
    weight_used: 0,
    remaining_g: 1000,
    cost_per_kg: null,
    storage_location: 'Dry box 2',
    archived: false,
    loaded: null,
  },
  {
    id: 30,
    material: 'PLA',
    brand: 'Inland',
    color_name: 'Black',
    rgba: '1A1A1AFF',
    label_weight: 1000,
    weight_used: 1000,
    remaining_g: 0,
    cost_per_kg: 14,
    archived: true,
    loaded: null,
  },
]

export const handlers = [
  http.get('/api/v1/farm/inventory', ({ request }) => {
    const archived = new URL(request.url).searchParams.get('include_archived') === 'true'
    return HttpResponse.json({
      spools: SPOOL_INVENTORY.filter((spool) => archived || !spool.archived),
      slots: [],
    })
  }),
]
