import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import type { Place } from './places'

/** The Select's value for "no place": a Radix item cannot have an empty value. */
const NONE = '__none'

/** A place picker; "No place" clears it. */
export function PlaceSelect({ id, label, places, value, onChange }: { id?: string; label: string; places: Place[]; value: string | null; onChange: (id: string | null) => void }) {
  return (
    <Select value={value ?? NONE} onValueChange={(next) => onChange(next === NONE ? null : next)}>
      <SelectTrigger id={id} aria-label={label} className="w-56">
        <SelectValue />
      </SelectTrigger>
      <SelectContent position="popper">
        <SelectItem value={NONE}>No place</SelectItem>
        {places.map((place) => (
          <SelectItem key={place.id} value={place.id}>
            {place.name}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

/** The places' read failure, said where a place is shown or picked. */
export function PlacesError({ error }: { error: Error | undefined }) {
  if (!error) return null
  return (
    <p role="alert" className="text-[13px] text-warn">
      The places could not be loaded: {error.message}
    </p>
  )
}
