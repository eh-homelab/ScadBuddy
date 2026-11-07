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
  /** Held until a check has read the process's values. */
  disabled?: boolean
  /** The disclosure opened: the dialog then asks the check for the process's values. */
  onOpen?: () => void
  onChange: (next: PartSettings) => void
}

/** A typed number as a setting: whole, within `0..max`; `null` for blank or not a number. */
function parsed(text: string, max: number): number | null {
  if (text.trim() === '') return null
  const read = Math.round(Number(text))
  return Number.isNaN(read) ? null : Math.min(max, Math.max(0, read))
}

/**
 * #5627 — "Settings for this colour": the slicer settings a print sets on the parts of
 * one filament slot (print-flow spec §4 "Settings per part (#5571)"). Each control opens
 * on the process's value, and only a setting changed from it is sent; a setting set back
 * to the process's value, or reset, is unset.
 */
export function PartSettingsDisclosure({ slotId, value, process, disabled = false, onOpen, onChange }: Props) {
  const [open, setOpen] = useState(false)
  /**
   * The text of the number field being typed in, while it has the focus: a controlled
   * field showing the setting would put the process's value back the moment it is
   * cleared, so backspace and then a new number could not be typed. It becomes the
   * setting as soon as it parses, and on blur.
   */
  const [draft, setDraft] = useState<{ key: string; text: string } | null>(null)
  const changed = changedCount(value)
  const id = `part-settings-${slotId}`
  return (
    <Collapsible
      open={open}
      onOpenChange={(next) => {
        setOpen(next)
        if (next) onOpen?.()
      }}
    >
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
                    disabled={disabled}
                    value={draft?.key === key ? draft.text : String(own ?? fromProcess ?? '')}
                    placeholder="Process"
                    onFocus={(event) => setDraft({ key, text: event.target.value })}
                    onChange={(event) => {
                      const text = event.target.value
                      setDraft({ key, text })
                      const read = parsed(text, max)
                      if (read !== null) onChange(withSetting(value, key, read, fromProcess))
                    }}
                    onBlur={(event) => {
                      setDraft(null)
                      onChange(withSetting(value, key, parsed(event.target.value, max), fromProcess))
                    }}
                    className="sb-num w-24 text-right"
                  />
                  {set && (
                    <Button
                      type="button"
                      variant="link"
                      size="sm"
                      className="h-auto px-0 text-xs"
                      disabled={disabled}
                      onClick={() => {
                        setDraft(null)
                        onChange(withSetting(value, key, null, fromProcess))
                      }}
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
                  disabled={disabled}
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
