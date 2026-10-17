import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import type { NozzlePlan, PlannedSlot } from '../../api/types'
import type { Side } from '../../lib/usePrintChoices'
import { Swatch } from './Swatch'

const SIDE_WORD: Record<Side, string> = { L: 'left', R: 'right' }
const FLOW_WORD = { standard: 'Standard', high_flow: 'High Flow' } as const
/** The side select's value for "plan it": Radix's select takes no empty value. */
const AUTOMATIC = 'auto'

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
export function NozzlePlanLine({
  plan,
  stale = false,
  sliced = false,
}: {
  plan: NozzlePlan | null | undefined
  stale?: boolean
  /** #2703 — the sides are the slice's (`asSliced`), not the plan's. */
  sliced?: boolean
}) {
  if (!plan || (plan.slots ?? []).length === 0) return null
  return (
    <p
      className={`flex flex-wrap items-center gap-x-1.5 gap-y-1 text-[13px] ${stale ? 'opacity-60' : ''}`}
      data-testid="nozzle-plan"
      aria-busy={stale}
    >
      <span className="text-muted-foreground">{sliced ? 'Nozzles, as sliced:' : 'Nozzles:'}</span>
      {(plan.slots ?? []).map((slot) => (
        <span key={slot.slot_id} className="inline-flex items-center gap-1">
          <Swatch colour={slot.colour} size="sm" />
          {slot.name} → {SIDE_WORD[slot.side]}
          <span aria-hidden="true" className="text-muted-foreground">
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
    <Card size="sm" role="group" aria-labelledby="nozzle-plan-step" data-testid="nozzle-plan-step">
      <CardHeader>
        <CardTitle id="nozzle-plan-step" className="text-sm">
          Nozzle for each filament
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        {slots.map((slot) => {
          const id = `nozzle-side-${slot.slot_id}`
          const chosen = sides[String(slot.slot_id)]
          return (
            <div key={slot.slot_id} className="flex flex-wrap items-center gap-2">
              <Label htmlFor={id} className="min-w-0 flex-1 gap-1.5 font-normal">
                <Swatch colour={slot.colour} size="sm" />
                <span className="truncate">
                  Slot {slot.slot_id}: {slot.name}
                </span>
              </Label>
              <Select
                value={chosen ?? AUTOMATIC}
                onValueChange={(next) => onSide(slot.slot_id, next === AUTOMATIC ? null : (next as Side))}
              >
                <SelectTrigger id={id} size="sm">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={AUTOMATIC}>Automatic{chosen ? '' : ` (${SIDE_WORD[slot.side]})`}</SelectItem>
                  <SelectItem value="L">Left</SelectItem>
                  <SelectItem value="R">Right</SelectItem>
                </SelectContent>
              </Select>
            </div>
          )
        })}
        {plan?.track_switch && (
          <p className="text-xs text-muted-foreground">The Filament Track Switch feeds any spool to either nozzle.</p>
        )}
        {/* #2737 review: say it here, before a slice lands to show it; only for a plan on both
            sides, as one held to a single side is never regrouped. #3955: a side chosen by
            hand pins every filament where this plan puts it. */}
        {new Set((plan?.slots ?? []).map((slot) => slot.side)).size > 1 && (
          <p className="text-xs text-muted-foreground" data-testid="nozzle-side-hint">
            {(plan?.slots ?? []).some((slot) => sides[String(slot.slot_id)])
              ? 'With a side chosen here, the slice keeps every filament on the side shown.'
              : 'Left on Automatic, the slicer groups filaments itself to cut purging, so it may put one on the other side: choose a side to keep it there.'}
          </p>
        )}
      </CardContent>
    </Card>
  )
}
