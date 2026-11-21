import { CheckIcon, TriangleAlertIcon } from 'lucide-react'
import type { TowerCheck, TowerConflict } from '../../api/types'
import { Button } from '@/components/ui/button'

type Props = {
  /** The check's `prime_tower`; `null` or absent when there is no tower to judge. */
  check: TowerCheck | null | undefined
  /** The slicer settings still hold the tower place this dialog's Move wrote, and a tower is printed. */
  moved: boolean
  /** Write the check's fix into the process overrides. */
  onMove: (fix: Record<string, string[]>) => void
  /** Take the values the move wrote out of the process overrides again. */
  onUndo: () => void
  /** Run Arrange again with room for the chosen tower; only an arranged output has it. */
  onRearrange?: (() => void) | undefined
  /** Arrange runs now. */
  arranging?: boolean
}

/** A tower place as the slicer reads it: millimetres, no trailing zeros. */
function mm(value: number): string {
  return String(Math.round(value * 10) / 10)
}

function where(conflict: TowerConflict): string | null {
  if (conflict.move_x == null || conflict.move_y == null) return null
  return `x ${mm(conflict.move_x)}, y ${mm(conflict.move_y)}`
}

/**
 * #6758 — the prime tower judged before the slice (`PrintCheck.prime_tower`): a tower the
 * slicer would refuse with "Object conflicts were detected", and the one-click fix. "Move
 * the prime tower" writes `wipe_tower_x`/`_y` into the print's slicer settings (the
 * generic process overrides, #2998), so the check, the background slice and the run all
 * take the new place. An arranged output can also be arranged again with room for the
 * tower chosen here.
 */
export function TowerConflictNotice({ check, moved, onMove, onUndo, onRearrange, arranging = false }: Props) {
  const conflicts = check?.conflicts ?? []
  if (conflicts.length === 0) {
    if (!moved) return null
    return (
      <p data-testid="tower-moved" className="mt-2 flex items-center gap-1.5 text-xs text-muted-foreground">
        <CheckIcon aria-hidden="true" className="size-3.5 text-primary" />
        <span>The prime tower moves clear of the objects.</span>
        <Button type="button" variant="link" onClick={onUndo} className="h-auto p-0 text-xs">
          Undo
        </Button>
      </p>
    )
  }
  const fix = check?.fix ?? null
  return (
    <div
      role="group"
      aria-labelledby="tower-conflict-title"
      data-testid="tower-conflict"
      className="mt-2 rounded-lg border border-primary/50 bg-primary/10 px-3 py-2 text-[12.5px] text-foreground"
    >
      <p id="tower-conflict-title" className="flex items-center gap-2 font-medium">
        <TriangleAlertIcon aria-hidden="true" className="size-4 shrink-0 text-primary" />
        The slicer will refuse this prime tower
      </p>
      <ul className="mt-1 space-y-1 pl-6">
        {conflicts.map((conflict) => {
          const spot = where(conflict)
          return (
            <li key={conflict.plate_id}>
              {conflict.message}
              {spot && <span className="block text-xs text-muted-foreground">Free spot: {spot}.</span>}
            </li>
          )
        })}
      </ul>
      {(fix || onRearrange) && (
        <div className="mt-2 flex flex-wrap gap-2 pl-6">
          {fix && (
            <Button type="button" size="sm" onClick={() => onMove(fix)}>
              Move the prime tower
            </Button>
          )}
          {onRearrange && (
            <Button type="button" size="sm" variant="outline" disabled={arranging} onClick={onRearrange}>
              {arranging ? 'Arranging…' : 'Re-arrange with room for the tower'}
            </Button>
          )}
        </div>
      )}
    </div>
  )
}
