import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import type { RackAlgorithm, RackOption, RackPickView } from '../../api/types'
import { ALGORITHM_LABELS, flowLabel } from './rackLabels'
import { Swatch } from './Swatch'

/**
 * #836 — Simple mode's one line: the rack position ScadBuddy would pick for the right
 * nozzle, the side the rack swaps hotends onto, and why. A preview: the run picks per
 * sliced group, and its result says what was sent. Warnings (an unsafe material) come
 * through the check's verdict and never hold Print.
 */
export function RackNozzleLine({ rack, algorithm }: { rack: RackPickView | null | undefined; algorithm: RackAlgorithm }) {
  if (!rack) return null
  const picked = rack.options?.find((option) => option.position === rack.position)
  let text: string | null = null
  if (picked) {
    text = `Right nozzle from the rack: position ${picked.position} (${picked.nozzle_diameter} ${flowLabel(picked.flow)})`
    if (rack.reason) text += ` — ${rack.reason}`
  } else if (algorithm === 'bambuddy') text = 'Right nozzle from the rack: Bambuddy picks at dispatch'
  if (text === null) return null
  return (
    <div className="text-[13px]">
      <p data-testid="rack-nozzle-line">{text}</p>
      {rack.glow_unchecked && (
        <p className="text-xs text-muted-foreground">A spool is not in the inventory, so Glow could not be checked.</p>
      )}
    </div>
  )
}

/** What a hotend last ran, as a person reads it: "last ran black PLA", or nothing known. */
function lastRan(option: RackOption): string | null {
  const ran = [option.color_word, (option.filament_type ?? '').trim()].filter(Boolean).join(' ')
  return ran ? `last ran ${ran}` : null
}

interface StepProps {
  rack: RackPickView | null | undefined
  algorithm: RackAlgorithm
  position: number | null
  onAlgorithm: (next: RackAlgorithm) => void
  /** The algorithm's save for this printer failed; this print still uses it. */
  algorithmUnsaved?: boolean
  onPosition: (next: number | null) => void
}

/**
 * #836, #2166 — Advanced mode: which hotend the right nozzle takes from the rack, as one
 * list: Automatic, or a position picked by hand, each named by its size, flow and the
 * filament it last ran, with that filament's colour (never a hex). How Automatic ranks
 * them is remembered per printer. The left nozzle has no rack: it prints with the hotend
 * mounted on it.
 */
export function RackNozzleStep({ rack, algorithm, position, onAlgorithm, onPosition, algorithmUnsaved }: StepProps) {
  return (
    <Card size="sm" role="group" aria-labelledby="rack-step" data-testid="rack-step">
      <CardHeader>
        <CardTitle id="rack-step" className="text-sm">
          Right nozzle, from the rack
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <RadioGroup
          aria-label="Rack nozzle position"
          value={position === null ? 'auto' : String(position)}
          onValueChange={(next) => onPosition(next === 'auto' ? null : Number(next))}
          className="gap-1.5"
        >
          <Label className="cursor-pointer font-normal">
            <RadioGroupItem value="auto" data-testid="rack-position-auto" />
            Automatic
          </Label>
          {(rack?.options ?? []).map((option) => {
            const ran = lastRan(option)
            return (
              <Label key={option.position} className="cursor-pointer flex-wrap gap-x-2 gap-y-0.5 font-normal">
                <RadioGroupItem value={String(option.position)} data-testid={`rack-position-${option.position}`} />
                {option.color ? <Swatch colour={option.color} size="sm" /> : null}
                <span>
                  Position {option.position} · {option.nozzle_diameter} {flowLabel(option.flow)}
                  {ran ? ` · ${ran}` : ''}
                </span>
                <span className="text-muted-foreground">
                  {option.prints} {option.prints === 1 ? 'print' : 'prints'}
                  {option.pending ? ` · ${option.pending} queued` : ''}
                </span>
              </Label>
            )
          })}
        </RadioGroup>
        <div className="grid gap-1.5">
          <Label htmlFor="rack-algorithm" className="text-xs font-normal text-muted-foreground">
            Automatic picks by
          </Label>
          <Select value={algorithm} onValueChange={(next) => onAlgorithm(next as RackAlgorithm)}>
            <SelectTrigger id="rack-algorithm" aria-label="Rack algorithm" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {(Object.keys(ALGORITHM_LABELS) as RackAlgorithm[]).map((value) => (
                <SelectItem key={value} value={value}>
                  {ALGORITHM_LABELS[value]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {algorithm === 'bambuddy' && (
            <p data-testid="rack-algorithm-bambuddy" className="text-xs text-muted-foreground">
              Printed over the LAN, the printer picks the hotends itself. A position picked above is sent
              only when the plate prints from one rack nozzle group; with several, the printer picks them all.
            </p>
          )}
          {algorithmUnsaved && (
            <p role="alert" data-testid="rack-algorithm-unsaved" className="text-xs text-destructive">
              Not remembered for this printer; this print still uses it.
            </p>
          )}
        </div>
        <p className="text-xs text-muted-foreground">The left nozzle prints with the hotend mounted on it.</p>
      </CardContent>
    </Card>
  )
}
