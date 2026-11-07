import { useState } from 'react'
import type { PartSettings } from '../../api/types'
import { CHOICE_SETTINGS, changedCount, NUMBER_SETTINGS, withSetting } from '../../lib/partSettings'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

/** The select's value for "the process's": no setting of this print's own. */
const PROCESS = '__process__'

type Props = {
  slotId: number
  /** This print's settings for the slot's parts; each unset one is the process's. */
  value: PartSettings | undefined
  /** The process's own values (the check's `process_part_settings`); `null` while unknown. */
  process: PartSettings | null
  onChange: (next: PartSettings) => void
}

/**
 * #5627 — "Settings for this colour": the slicer settings a print sets on the parts of
 * one filament slot (print-flow spec §4 "Settings per part (#5571)"). Each control opens
 * on the process's value, and only a setting changed from it is sent; a setting set back
 * to the process's value, or reset, is unset.
 */
export function PartSettingsDisclosure({ slotId, value, process, onChange }: Props) {
  const [open, setOpen] = useState(false)
  const changed = changedCount(value)
  const id = `part-settings-${slotId}`
  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger asChild>
        <Button
          type="button"
          variant="link"
          size="sm"
          className="h-auto w-fit px-0 text-xs"
          data-testid={`part-settings-toggle-${slotId}`}
        >
          Settings for this colour
          {changed > 0 && <span className="text-muted-foreground">{` (${changed} changed)`}</span>}
        </Button>
      </CollapsibleTrigger>
      <CollapsibleContent>
        <div className="mt-2 grid gap-2 sm:grid-cols-2" data-testid={`part-settings-${slotId}`}>
          {NUMBER_SETTINGS.map(({ key, label, max, unit }) => {
            const own = value?.[key]
            const fromProcess = process?.[key] ?? null
            const set = own !== null && own !== undefined
            return (
              <div key={key} className="grid gap-1">
                <Label htmlFor={`${id}-${key}`} className="text-xs font-normal text-muted-foreground">
                  {label}
                  {unit ? ` (${unit})` : ''}
                  {set && <span className="text-foreground"> · changed</span>}
                </Label>
                <div className="flex items-center gap-2">
                  <Input
                    id={`${id}-${key}`}
                    type="number"
                    min={0}
                    max={max}
                    step={1}
                    value={own ?? fromProcess ?? ''}
                    placeholder="Process"
                    onChange={(event) => {
                      const text = event.target.value
                      const read = text === '' ? null : Math.round(Number(text))
                      const bounded = read === null || Number.isNaN(read) ? null : Math.min(max, Math.max(0, read))
                      onChange(withSetting(value, key, bounded, fromProcess))
                    }}
                    className="sb-num w-24 text-right"
                  />
                  {set && (
                    <Button
                      type="button"
                      variant="link"
                      size="sm"
                      className="h-auto px-0 text-xs"
                      onClick={() => onChange(withSetting(value, key, null, fromProcess))}
                      data-testid={`${id}-${key}-reset`}
                    >
                      Reset
                    </Button>
                  )}
                </div>
              </div>
            )
          })}
          {CHOICE_SETTINGS.map(({ key, label, values }) => {
            const own = value?.[key] ?? null
            const fromProcess = process?.[key] ?? null
            return (
              <div key={key} className="grid gap-1">
                <Label htmlFor={`${id}-${key}`} className="text-xs font-normal text-muted-foreground">
                  {label}
                  {own !== null && <span className="text-foreground"> · changed</span>}
                </Label>
                <Select
                  value={own ?? PROCESS}
                  onValueChange={(next) =>
                    onChange(
                      withSetting(
                        value,
                        key,
                        next === PROCESS ? null : (next as NonNullable<PartSettings[typeof key]>),
                        fromProcess,
                      ),
                    )
                  }
                >
                  <SelectTrigger id={`${id}-${key}`} className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={PROCESS}>
                      {fromProcess === null ? 'Process' : `Process (${fromProcess})`}
                    </SelectItem>
                    {values.map((option) => (
                      <SelectItem key={option} value={option}>
                        {option}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            )
          })}
        </div>
      </CollapsibleContent>
    </Collapsible>
  )
}
