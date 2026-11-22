/**
 * The colour picker's list filters (`FilamentColourPicker`), remembered in this browser, which
 * the filament quick find shares (#6894). A material no row has reads as all.
 */
export type PickerFilters = { material: string | null; loadedOnly: boolean }
export const PICKER_FILTERS_KEY = 'scadbuddy.colourPicker.filters'
const NO_FILTERS: PickerFilters = { material: null, loadedOnly: false }

export function readPickerFilters(): PickerFilters {
  try {
    const raw: unknown = JSON.parse(localStorage.getItem(PICKER_FILTERS_KEY) ?? 'null')
    if (raw && typeof raw === 'object') {
      const { material, loadedOnly } = raw as Record<string, unknown>
      return { material: typeof material === 'string' ? material : null, loadedOnly: loadedOnly === true }
    }
  } catch {
    // Blocked or broken storage: start with no filters.
  }
  return NO_FILTERS
}

export function writePickerFilters(filters: PickerFilters) {
  try {
    localStorage.setItem(PICKER_FILTERS_KEY, JSON.stringify(filters))
  } catch {
    // Blocked storage: the choice lasts until the list closes.
  }
}
