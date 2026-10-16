import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import type { InstalledNozzle, NozzleChoice } from '../../api/types'

type Props = {
  sizes: string[]
  installed: InstalledNozzle[]
  value: NozzleChoice[]
  onChange: (next: NozzleChoice[]) => void
}

const SIDES = ['Left', 'Right'] as const

/**
 * An Advanced step (#768): Simple mode does not show it, and sends the size the dialog
 * opened on (this model's last, else 0.4 mm) with Standard flow on both sides. The dialog
 * opens in Advanced instead when the flow it opens on is High Flow, remembered or the
 * printer's default (#1895).
 *
 * Spec 2026-09-27 §4.1 — Bambuddy rejects mixed nozzle sizes on the left and right
 * extruder (422 "different sizes"), so size is ONE radiogroup setting both sides. There
 * is no per-side size selector at all. Flow (Standard / High Flow) is per-side, and the
 * print states it for each side in the sliced file, as Bambu Studio does (#484).
 *
 * Only installed sizes are offered (#2166); they and the not-installed note read the whole hotend rack, not what is
 * mounted: the printer swaps the sliced size on itself, and nothing here is checked
 * against the mounted pair (#768).
 */
export function NozzleStep({ sizes, installed, value, onChange }: Props) {
  const has = (size: string) => installed.some((n) => n.size === size)
  const setBothSizes = (size: string) =>
    onChange(value.map((n) => ({ ...n, size: size as NozzleChoice['size'] })))
  const setFlow = (side: number, flow: NozzleChoice['flow']) =>
    onChange(value.map((n, i) => (i === side ? { ...n, flow } : n)))
  const missing = [...new Set(value.map((n) => n.size))].filter(
    (size) => installed.length > 0 && !has(size),
  )
  // #2166 — only the sizes the printer has (mounted or in the rack), and the one chosen.
  const shown = installed.length > 0 ? sizes.filter((size) => has(size) || value[0]?.size === size) : sizes

  return (
    <Card size="sm" role="group" aria-labelledby="print-nozzles">
      <CardHeader>
        <CardTitle id="print-nozzles" className="text-sm">
          Nozzles
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <RadioGroup
          aria-label="Nozzle size"
          value={value[0]?.size ?? ''}
          onValueChange={setBothSizes}
          className="flex flex-wrap gap-4"
        >
          {shown.map((size) => (
            <Label key={size} className="cursor-pointer font-normal">
              <RadioGroupItem value={size} />
              {size} mm
            </Label>
          ))}
        </RadioGroup>

        <div className="flex flex-wrap gap-x-6 gap-y-2">
          {SIDES.map((side, index) => (
            <div key={side} className="flex items-center gap-3">
              <span className="text-xs text-muted-foreground">{side}</span>
              <RadioGroup
                aria-label={`${side} nozzle flow`}
                value={value[index]?.flow ?? ''}
                onValueChange={(flow) => setFlow(index, flow as NozzleChoice['flow'])}
                className="flex gap-3"
              >
                {(['standard', 'high_flow'] as const).map((flow) => {
                  const word = flow === 'standard' ? 'Standard' : 'High Flow'
                  return (
                    <Label key={flow} className="cursor-pointer font-normal">
                      <RadioGroupItem value={flow} aria-label={`${side} ${word}`} />
                      {word}
                    </Label>
                  )
                })}
              </RadioGroup>
            </div>
          ))}
        </div>

        {missing.map((size) => (
          <p key={size} role="status" className="text-xs text-destructive">
            No {size} mm nozzle is installed. Install one before this prints.
          </p>
        ))}
      </CardContent>
    </Card>
  )
}
