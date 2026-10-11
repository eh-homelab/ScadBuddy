import type { NozzlePlan, PlannedSlot } from '../../api/types'
import type { Side } from '../../lib/usePrintChoices'
import { Swatch } from './Swatch'

const SIDE_WORD: Record<Side, string> = { L: 'left', R: 'right' }
const FLOW_WORD = { standard: 'Standard', high_flow: 'High Flow' } as const

/** "0.4 High Flow", or each side's flow when they differ. */
function flowWords(plan: NozzlePlan): string {
  const sides = new Set(plan.slots?.map((slot) => slot.side))
  if (plan.left_flow === plan.right_flow || sides.size === 1) {
    const flow = sides.has('L') && sides.size === 1 ? plan.left_flow : plan.right_flow
    return `${plan.size} ${FLOW_WORD[flow]}`
  }
  return `${plan.size} · left ${FLOW_WORD[plan.left_flow]}, right ${FLOW_WORD[plan.right_flow]}`
}

/**
 * #2166 — which nozzle each filament prints from, in one line: "Mistletoe Green → left ·
 * Inland Black → right · 0.4 High Flow". ScadBuddy plans it by material and states it in
 * the file; `stale` dims it while the plan for changed choices is read.
 */
export function NozzlePlanLine({ plan, stale = false }: { plan: NozzlePlan | null | undefined; stale?: boolean }) {
  if (!plan || (plan.slots ?? []).length === 0) return null
  return (
    <p
      className={`flex flex-wrap items-center gap-x-1.5 gap-y-1 text-[12.5px] text-ink ${stale ? 'opacity-60' : ''}`}
      data-testid="nozzle-plan"
      aria-busy={stale}
    >
      <span className="text-muted">Nozzles:</span>
      {(plan.slots ?? []).map((slot) => (
        <span key={slot.slot_id} className="inline-flex items-center gap-1">
          <Swatch colour={slot.colour} size="sm" />
          {slot.name} → {SIDE_WORD[slot.side]}
          <span aria-hidden="true" className="text-faint">
            ·
          </span>
        </span>
      ))}
      <span>{flowWords(plan)}</span>
    </p>
  )
}

type StepProps = {
  plan: NozzlePlan | null | undefined
  /** The sides chosen by hand, by slot id. */
  sides: Record<string, Side>
  onSide: (slotId: number, side: Side | null) => void
}

/**
 * #2166, Advanced — the plan per filament, each overridable: Automatic (ScadBuddy's
 * plan, named), Left or Right. With the Track Switch any spool reaches either nozzle.
 */
export function NozzlePlanStep({ plan, sides, onSide }: StepProps) {
  const slots: PlannedSlot[] = plan?.slots ?? []
  if (slots.length === 0) return null
  return (
    <fieldset className="rounded-[6px] border border-line bg-surface-2 px-3 py-2" data-testid="nozzle-plan-step">
      <legend className="px-1 text-[13px] text-ink">Nozzle for each filament</legend>
      <div className="mt-1.5 flex flex-col gap-2">
        {slots.map((slot) => {
          const id = `nozzle-side-${slot.slot_id}`
          const chosen = sides[String(slot.slot_id)] ?? ''
          return (
            <div key={slot.slot_id} className="flex flex-wrap items-center gap-2">
              <label htmlFor={id} className="flex min-w-0 flex-1 items-center gap-1.5 text-[12px] text-ink">
                <Swatch colour={slot.colour} size="sm" />
                <span className="truncate">
                  Slot {slot.slot_id}: {slot.name}
                </span>
              </label>
              <select
                id={id}
                value={chosen}
                onChange={(event) => onSide(slot.slot_id, (event.target.value || null) as Side | null)}
                className="sb-field w-auto py-1 text-[12px]"
              >
                <option value="">Automatic{chosen ? '' : ` (${SIDE_WORD[slot.side]})`}</option>
                <option value="L">Left</option>
                <option value="R">Right</option>
              </select>
            </div>
          )
        })}
      </div>
      {plan?.track_switch && (
        <p className="mt-1.5 text-[12px] text-muted">
          The Filament Track Switch feeds any spool to either nozzle.
        </p>
      )}
    </fieldset>
  )
}
