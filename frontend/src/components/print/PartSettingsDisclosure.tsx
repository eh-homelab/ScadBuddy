import { useState } from 'react'
import type { PartSettings } from '../../api/types'
import { CHOICE_SETTINGS, changedCount, NUMBER_SETTINGS, withSetting } from '../../lib/partSettings'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

/** The select's value for the slot's default: no setting of this print's own. */
const DEFAULT = '__default__'

type Props = {
  slotId: number
  /** This print's settings for the slot's parts; each unset one is the slot's default. */
  value: PartSettings | undefined
  /**
   * What the slot prints with when this print sets nothing: the process's values with the
   * template's `part_print_settings` (#5626) for the slot laid over them (the check's
   * `slot_part_settings`, else `process_part_settings`); `null` while unknown.
   */
  baseline: PartSettings | null
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

/** Whether `text` is a number outside `0..max`, which `parsed` keeps within the bounds. */
function outOfRange(text: string, max: number): boolean {
  if (text.trim() === '') return false
  const read = Number(text)
  return !Number.isNaN(read) && (read < 0 || read > max)
}

/**
 * #5627 — "Settings for this colour": the slicer settings a print sets on the parts of
 * one filament slot (print-flow spec §4 "Settings per part (#5571)"). Each control opens
 * on the slot's default (`baseline`: the process's value, or the template's for the slot),
 * and only a setting changed from it is sent; a setting set back to the default, or reset,
 * is unset. A number outside the setting's bounds is kept within them, and says so.
 */
export function PartSettingsDisclosure({ slotId, value, baseline, disabled = false, onOpen, onChange }: Props) {
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
            const fromBaseline = baseline?.[key] ?? null
            const set = own !== null && own !== undefined
            const clamped = draft?.key === key && outOfRange(draft.text, max)
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
                    value={draft?.key === key ? draft.text : String(own ?? fromBaseline ?? '')}
                    placeholder="Default"
                    aria-invalid={clamped || undefined}
                    aria-describedby={clamped ? `${id}-${key}-clamped` : undefined}
                    onFocus={(event) => setDraft({ key, text: event.target.value })}
                    onChange={(event) => {
                      const text = event.target.value
                      setDraft({ key, text })
                      const read = parsed(text, max)
                      if (read !== null) onChange(withSetting(value, key, read, fromBaseline))
                    }}
                    onBlur={(event) => {
                      setDraft(null)
                      onChange(withSetting(value, key, parsed(event.target.value, max), fromBaseline))
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
                        onChange(withSetting(value, key, null, fromBaseline))
                      }}
                      data-testid={`${id}-${key}-reset`}
                    >
                      Reset
                    </Button>
                  )}
                </div>
                {clamped && (
                  <p id={`${id}-${key}-clamped`} className="text-xs text-destructive" role="status">
                    {`Kept within 0–${max}.`}
                  </p>
                )}
              </div>
            )
          })}
          {CHOICE_SETTINGS.map(({ key, label, values }) => {
            const own = value?.[key] ?? null
            const fromBaseline = baseline?.[key] ?? null
            return (
              <div key={key} className="grid gap-1">
                <Label htmlFor={`${id}-${key}`} className="text-xs font-normal text-muted-foreground">
                  {label}
                  {own !== null && <span className="text-foreground"> · changed</span>}
                </Label>
                <Select
                  disabled={disabled}
                  value={own ?? DEFAULT}
                  onValueChange={(next) =>
                    onChange(
                      withSetting(
                        value,
                        key,
                        next === DEFAULT ? null : (next as NonNullable<PartSettings[typeof key]>),
                        fromBaseline,
                      ),
                    )
                  }
                >
                  <SelectTrigger id={`${id}-${key}`} className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={DEFAULT}>
                      {fromBaseline === null ? 'Default' : `Default (${fromBaseline})`}
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
