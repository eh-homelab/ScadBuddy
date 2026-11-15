import { useEffect, useRef, useState } from 'react'
import { ArrowDownIcon, ArrowUpIcon, XIcon } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { MAX_MATERIALS, materialKey } from './listingDraft'

/**
 * #6258 — an ordered list of materials, first preferred: a listing's "Materials allowed" and
 * Settings' "Storefront materials". Each row moves up or down by its buttons (by keyboard
 * too: the moved row's button keeps the focus) or is removed; "Add a material" adds one of
 * `choices` at the end. It never holds fewer than one, nor more than `MAX_MATERIALS`
 * (backend `core/settings.py` `MAX_MATERIALS`).
 */
export function MaterialOrder({
  label,
  materials,
  choices,
  onChange,
}: {
  /** The list's accessible name. */
  label: string
  materials: readonly string[]
  /** The materials that can be added, such as the price book's; those on the list are left out. */
  choices: readonly string[]
  onChange: (materials: string[]) => void
}) {
  const buttons = useRef(new Map<string, HTMLButtonElement>())
  /** The button to focus once the list has moved: a moved row's node is re-inserted, which drops the focus. */
  const [focus, setFocus] = useState<string | null>(null)
  useEffect(() => {
    if (focus === null) return
    buttons.current.get(focus)?.focus()
    setFocus(null)
  }, [focus, materials])

  const present = new Set(materials.map(materialKey))
  const addable = choices.filter((choice) => !present.has(materialKey(choice)))

  function move(from: number, to: number) {
    const next = [...materials]
    const [moved] = next.splice(from, 1)
    if (moved === undefined) return
    next.splice(to, 0, moved)
    onChange(next)
    // At the top, "up" is disabled: the focus goes to "down", and the other way round.
    const edge = to === 0 ? 'down' : to === next.length - 1 ? 'up' : to < from ? 'up' : 'down'
    setFocus(`${edge}:${materialKey(moved)}`)
  }

  const keep = (key: string) => (node: HTMLButtonElement | null) => {
    if (node) buttons.current.set(key, node)
    else buttons.current.delete(key)
  }

  return (
    <div className="flex flex-col gap-2">
      <ol aria-label={label} className="flex flex-col gap-1.5">
        {materials.map((material, index) => {
          const key = materialKey(material)
          return (
            <li key={key} className="grid grid-cols-[1.5rem_minmax(0,1fr)_auto] items-center gap-2 rounded-md border px-2.5 py-1.5" data-testid="material-row" data-material={material}>
              <span className="text-xs tabular-nums text-muted-foreground" aria-hidden="true">
                {index + 1}
              </span>
              <span className="truncate text-sm font-medium">
                {material}
                {index === 0 && <span className="ml-2 text-xs font-normal text-muted-foreground">first choice</span>}
              </span>
              <span className="flex items-center gap-1">
                <Button ref={keep(`up:${key}`)} type="button" variant="outline" size="icon-sm" aria-label={`Move ${material} up`} disabled={index === 0} onClick={() => move(index, index - 1)}>
                  <ArrowUpIcon />
                </Button>
                <Button
                  ref={keep(`down:${key}`)}
                  type="button"
                  variant="outline"
                  size="icon-sm"
                  aria-label={`Move ${material} down`}
                  disabled={index === materials.length - 1}
                  onClick={() => move(index, index + 1)}
                >
                  <ArrowDownIcon />
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`Remove ${material}`}
                  disabled={materials.length <= 1}
                  onClick={() => onChange(materials.filter((_, i) => i !== index))}
                >
                  <XIcon />
                </Button>
              </span>
            </li>
          )
        })}
      </ol>
      {addable.length > 0 && materials.length < MAX_MATERIALS && (
        // Controlled at no value, so it shows its placeholder again after each pick.
        <Select value="" onValueChange={(material) => material && onChange([...materials, material])}>
          <SelectTrigger aria-label="Add a material" className="w-48" size="sm">
            <SelectValue placeholder="Add a material" />
          </SelectTrigger>
          <SelectContent position="popper">
            {addable.map((material) => (
              <SelectItem key={materialKey(material)} value={material}>
                {material}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}
    </div>
  )
}
