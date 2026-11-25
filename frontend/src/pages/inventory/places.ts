import { listLocations, type LocationNode } from '../../api/filament'
import { useAsync } from '../../lib/useAsync'

/** A place a lot or a unit can be kept in, by its path ("Shop / Shelf A"). */
export type Place = { id: string; name: string }

/** Every place by its path, trays too (a nozzle can sit in a printer's place). */
function flatten(nodes: LocationNode[], path: string[] = []): Place[] {
  return nodes.flatMap((node) => [{ id: node.id, name: [...path, node.name].join(' / ') }, ...flatten(node.children ?? [], [...path, node.name])])
}

/** #7067, R-P2-38 — the Filament section's places (`GET /filament/locations`): stock shares them. */
export function usePlaces() {
  return useAsync(async () => flatten(await listLocations()), [])
}

/** The place's path, or a short id when the list does not hold it (an archived place). */
export function placeName(places: Place[] | undefined, id: string | null): string {
  if (!id) return 'none'
  return places?.find((place) => place.id === id)?.name ?? `place ${id.slice(0, 8)}`
}
