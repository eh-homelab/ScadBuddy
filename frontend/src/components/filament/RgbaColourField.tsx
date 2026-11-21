import { useId, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Slider } from '@/components/ui/slider'
import { alphaOf, CLEAR_ALPHA, CLEAR_MAX_ALPHA, parseRgba, TRANSLUCENT_ALPHA, withAlpha } from '../../lib/rgba'
import { RgbaSwatch } from './RgbaSwatch'

/** spec §5.1's presets: the alpha each sets, and whether a colour is in it. */
const PRESETS = [
  { label: 'Opaque', alpha: 0xff, holds: (alpha: number) => alpha === 0xff },
  { label: 'Translucent', alpha: TRANSLUCENT_ALPHA, holds: (alpha: number) => alpha === TRANSLUCENT_ALPHA },
  { label: 'Clear', alpha: CLEAR_ALPHA, holds: (alpha: number) => alpha <= CLEAR_MAX_ALPHA },
] as const

/** What the presets, the picker and the slider change while nothing is set or inherited. */
const BLANK = '#FFFFFFFF'

const percentOf = (alpha: number) => Math.round((alpha / 0xff) * 100)

export interface RgbaColourFieldProps {
  /** `#RRGGBBAA`, or `null` while unset (a spool then takes its type's colour). */
  value: string | null
  onChange: (rgba: string) => void
  /** The type's colour, shown as the placeholder and the swatch while `value` is `null`. */
  inherited?: string | null
}

/**
 * #6262 — an `#RRGGBBAA` colour: a hex field (six digits read as opaque), the browser's
 * colour picker for the RGB, an opacity slider and spec §5.1's presets (opaque `FF`,
 * translucent `80`, clear `10`), beside the spool chip it makes. The picker has no alpha
 * channel, so a new tint keeps the alpha it had.
 */
export function RgbaColourField({ value, onChange, inherited = null }: RgbaColourFieldProps) {
  const id = useId()
  const [draft, setDraft] = useState(value ?? '')
  const [seen, setSeen] = useState(value)
  if (value !== seen) {
    // A new value from outside: show it, unless the draft already reads as it.
    setSeen(value)
    if (parseRgba(draft.trim()) !== value) setDraft(value ?? '')
  }

  const base = value ?? (inherited ? parseRgba(inherited) : null) ?? BLANK
  const alpha = alphaOf(base)
  // Said once the field is left, not at every keystroke on the way to six digits, and
  // dropped once the draft reads again: the next mistake waits for the next leave.
  const [left, setLeft] = useState(false)
  const invalid = left && draft.trim() !== '' && parseRgba(draft.trim()) === null
  const errorId = `${id}-error`

  return (
    <div className="flex items-start gap-3">
      <RgbaSwatch rgba={base} size="lg" className="mt-0.5" />
      <div className="flex min-w-0 flex-1 flex-col gap-2">
        <div className="flex items-center gap-2">
          <Input
            aria-label="Colour (hex)"
            aria-invalid={invalid}
            aria-describedby={invalid ? errorId : undefined}
            spellCheck={false}
            autoComplete="off"
            placeholder={value === null && inherited ? (parseRgba(inherited) ?? inherited) : '#RRGGBBAA'}
            value={draft}
            onBlur={() => setLeft(true)}
            onChange={(event) => {
              setDraft(event.target.value)
              // Spaces around a pasted colour are the field's to drop, not the parser's.
              const parsed = parseRgba(event.target.value.trim())
              if (parsed === null) return
              setLeft(false)
              if (parsed !== value) onChange(parsed)
            }}
            className="w-32 tabular-nums"
          />
          <input
            type="color"
            aria-label="Pick a colour"
            value={base.slice(0, 7).toLowerCase()}
            onChange={(event) => {
              const parsed = parseRgba(event.target.value)
              if (parsed !== null) onChange(withAlpha(parsed, alpha))
            }}
            className="h-8 w-10 shrink-0 cursor-pointer rounded-md border border-input bg-transparent p-0.5"
          />
        </div>
        {invalid && (
          <p id={errorId} role="alert" className="text-[12px] text-destructive">
            Use six or eight hex digits: #RRGGBB or #RRGGBBAA.
          </p>
        )}
        <div className="flex items-center gap-3">
          <span className="text-[13px] text-muted-foreground">Opacity</span>
          <Slider
            min={0}
            max={100}
            step={1}
            value={[percentOf(alpha)]}
            onValueChange={([percent]) => {
              if (percent !== undefined) onChange(withAlpha(base, (percent / 100) * 0xff))
            }}
            thumbLabel="Opacity"
            className="max-w-48"
          />
          <span className="w-10 text-right text-[13px] tabular-nums">{percentOf(alpha)} %</span>
        </div>
        <div className="flex flex-wrap gap-1.5">
          {PRESETS.map((preset) => (
            <Button
              key={preset.label}
              type="button"
              size="xs"
              variant="outline"
              aria-pressed={preset.holds(alpha)}
              onClick={() => onChange(withAlpha(base, preset.alpha))}
              className="aria-pressed:bg-secondary aria-pressed:text-secondary-foreground"
            >
              {preset.label}
            </Button>
          ))}
        </div>
      </div>
    </div>
  )
}
