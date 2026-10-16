import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { StepCard } from './StepCard'
import { useState } from 'react'
import type { SupportsChoice } from '../../api/types'

type Props = {
  /** `null` leaves supports to the template's `print_settings` and the process. */
  value: SupportsChoice | null
  onChange: (next: SupportsChoice | null) => void
  /** The template's `print_settings`, for what Default means for this model. */
  templateSettings?: Record<string, string> | undefined
  /** A library 3MF: its own project settings, unread here, may declare supports. */
  fromFile?: boolean
}

type Mode = 'default' | 'off' | 'tree' | 'normal'

function modeOf(value: SupportsChoice | null): Mode {
  if (value === null) return 'default'
  if (!value.enabled) return 'off'
  return value.type ?? 'tree'
}

/** What Default prints with: the template's declaration, else the process preset's. */
function defaultLabel(settings: Record<string, string> | undefined, fromFile: boolean): string {
  if (settings?.enable_support === '1') {
    const type = settings.support_type?.startsWith('normal') ? 'normal' : 'tree'
    const angle = settings.support_threshold_angle
    return `Default (template: ${type} supports${angle ? `, ${angle}°` : ''})`
  }
  if (settings?.enable_support === '0') return 'Default (template: no supports)'
  return fromFile ? 'Default (file or process)' : 'Default (process)'
}

/**
 * #1899 — support structures for this print: none, or tree or normal supports, over what
 * the template declares. Tree is Bambu Studio's default and the one an H2C prints best.
 * Per print, never remembered. A setting the server cannot write in the preset's shape is
 * left out with a warning on the result, never a refusal.
 */
export function SupportsStep({ value, onChange, templateSettings, fromFile = false }: Props) {
  const mode = modeOf(value)
  const on = value?.enabled === true ? value : null
  // What was typed, kept even when it is not a whole 0–90, so the field explains itself
  // rather than blanking (#2837 review); the choice carries only a valid angle.
  const [angleText, setAngleText] = useState(on?.threshold_angle?.toString() ?? '')
  const angleInvalid = angleText !== '' && on?.threshold_angle == null
  // Default, No supports or a reset from outside starts the next choice's field empty.
  if (on === null && angleText !== '') setAngleText('')
  return (
    <StepCard title="Supports">
      <div className="grid gap-1.5">
        <Label htmlFor="supports-select" className="text-xs font-normal text-muted-foreground">
          Support type
        </Label>
        <Select
          value={mode}
          onValueChange={(chosen) => {
            const next = chosen as Mode
            if (next === 'default') onChange(null)
            // Only `enabled`: the backend applies nothing else with supports off (#2837 review).
            else if (next === 'off') onChange({ enabled: false })
            else
              onChange({
                enabled: true,
                type: next,
                // Starts at the template's, so switching type alone keeps it (#2835 review).
                on_build_plate_only:
                  on?.on_build_plate_only ?? templateSettings?.support_on_build_plate_only === '1',
                ...(on?.threshold_angle == null ? {} : { threshold_angle: on.threshold_angle }),
              })
          }}
        >
          <SelectTrigger id="supports-select" className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="default">{defaultLabel(templateSettings, fromFile)}</SelectItem>
            <SelectItem value="off">No supports</SelectItem>
            <SelectItem value="tree">Tree supports</SelectItem>
            <SelectItem value="normal">Normal supports</SelectItem>
          </SelectContent>
        </Select>
      </div>
      {on && (
        <div className="space-y-3 pt-1">
          <div className="grid gap-1.5">
            <Label htmlFor="supports-angle" className="text-xs font-normal text-muted-foreground">
              Threshold angle (°)
            </Label>
            <Input
              id="supports-angle"
              type="number"
              min={0}
              max={90}
              step={1}
              inputMode="numeric"
              placeholder={templateSettings?.support_threshold_angle ?? (fromFile ? 'Default' : 'Preset')}
              value={angleText}
              aria-invalid={angleInvalid}
              aria-describedby={angleInvalid ? 'supports-angle-invalid' : undefined}
              onChange={(event) => {
                const raw = event.target.value
                setAngleText(raw)
                // Digits only: a number field lets `1e1` through, which is no whole number
                // of degrees as written (#3180).
                const angle = Number(raw)
                const { threshold_angle: _dropped, ...rest } = on
                if (!/^\d+$/.test(raw) || angle > 90) onChange(rest)
                else onChange({ ...rest, threshold_angle: angle })
              }}
              className="w-24"
            />
            {angleInvalid && (
              <p id="supports-angle-invalid" className="text-xs text-destructive">
                A whole number of degrees from 0 to 90. Until then, the template&apos;s angle
                is used, else the preset&apos;s.
              </p>
            )}
            <p className="text-xs text-muted-foreground">
              Overhangs steeper than this, from vertical, get supports. Empty uses the
              template&apos;s angle, else the preset&apos;s.
            </p>
          </div>
          <Label className="cursor-pointer font-normal">
            <Checkbox
              checked={on.on_build_plate_only ?? false}
              onCheckedChange={(checked) => onChange({ ...on, on_build_plate_only: checked === true })}
            />
            On build plate only
          </Label>
        </div>
      )}
    </StepCard>
  )
}
