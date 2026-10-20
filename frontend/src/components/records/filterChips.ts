/** The chip helpers of the records `FilterBar` (#4249), in a file of their own so the
 * component file exports components only (react-refresh). */

/** How many chips a group shows before "Show more" (the Activity page's `CHIPS_SHOWN`). */
export const CHIPS_SHOWN = 10

/** A chip: a toggle in a group, wrapping a long value rather than widening the page. */
export const CHIP =
  'h-auto min-h-7 max-w-full rounded-full px-2.5 py-0.5 text-left text-xs font-normal break-all whitespace-normal data-[state=on]:border-primary/60 data-[state=on]:bg-primary/15 data-[state=on]:text-foreground'

/** The first `limit` of `items`, and every selected one past them, so a choice in force is never hidden. */
export function shownChips<T>(items: T[], key: (item: T) => string, selected: string[], all: boolean, limit = CHIPS_SHOWN): T[] {
  if (all || items.length <= limit) return items
  return items.filter((item, i) => i < limit || selected.includes(key(item)))
}
