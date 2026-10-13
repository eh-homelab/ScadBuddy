import type { SupportsChoice } from '../../api/types'

type Props = {
  /** `null` leaves supports to the template's `print_settings` and the process. */
  value: SupportsChoice | null
  onChange: (next: SupportsChoice | null) => void
  /** The template's `print_settings`, for what Default means for this model. */
  templateSettings?: Record<string, string> | undefined
}

type Mode = 'default' | 'off' | 'tree' | 'normal'

function modeOf(value: SupportsChoice | null): Mode {
  if (value === null) return 'default'
  if (!value.enabled) return 'off'
  return value.type ?? 'tree'
}

/** What Default prints with: the template's declaration, else the process preset's. */
function defaultLabel(settings: Record<string, string> | undefined): string {
  if (settings?.enable_support === '1') {
    const type = settings.support_type?.startsWith('normal') ? 'normal' : 'tree'
    const angle = settings.support_threshold_angle
    return `Default (template: ${type} supports${angle ? `, ${angle}°` : ''})`
  }
  if (settings?.enable_support === '0') return 'Default (template: no supports)'
  return 'Default (process)'
}

/**
 * #1899 — support structures for this print: none, or tree or normal supports, over what
 * the template declares. Tree is Bambu Studio's default and the one an H2C prints best.
 * Per print, never remembered. A setting the server cannot write in the preset's shape is
 * left out with a warning on the result, never a refusal.
 */
export function SupportsStep({ value, onChange, templateSettings }: Props) {
  const mode = modeOf(value)
  const on = value?.enabled === true ? value : null
  return (
    <fieldset className="rounded-[6px] border border-line bg-surface-2 px-3 py-2">
      <legend className="px-1 text-[13px] text-ink">Supports</legend>
      <div className="mt-1.5 flex flex-col gap-1">
        <label htmlFor="supports-select" className="text-[12px] text-muted">
          Supports
        </label>
        <select
          id="supports-select"
          value={mode}
          onChange={(event) => {
            const next = event.target.value as Mode
            if (next === 'default') onChange(null)
            else if (next === 'off') onChange({ enabled: false, type: 'tree', on_build_plate_only: false })
            else
              onChange({
                enabled: true,
                type: next,
                on_build_plate_only: on?.on_build_plate_only ?? false,
                ...(on?.threshold_angle == null ? {} : { threshold_angle: on.threshold_angle }),
              })
          }}
          className="sb-field"
        >
          <option value="default">{defaultLabel(templateSettings)}</option>
          <option value="off">No supports</option>
          <option value="tree">Tree supports</option>
          <option value="normal">Normal supports</option>
        </select>
      </div>
      {on && (
        <div className="mt-2 flex flex-col gap-2">
          <div className="flex flex-col gap-1">
            <label htmlFor="supports-angle" className="text-[12px] text-muted">
              Threshold angle (°)
            </label>
            <input
              id="supports-angle"
              type="number"
              min={0}
              max={90}
              step={1}
              inputMode="numeric"
              placeholder={templateSettings?.support_threshold_angle ?? 'Preset'}
              value={on.threshold_angle ?? ''}
              onChange={(event) => {
                const raw = event.target.value
                const angle = Number(raw)
                const { threshold_angle: _dropped, ...rest } = on
                if (raw === '' || !Number.isInteger(angle) || angle < 0 || angle > 90) onChange(rest)
                else onChange({ ...rest, threshold_angle: angle })
              }}
              className="sb-field w-24"
            />
            <p className="text-[12px] text-muted">
              Overhangs steeper than this, from vertical, get supports. Empty uses the
              template&apos;s angle, else the preset&apos;s.
            </p>
          </div>
          <label className="flex items-center gap-2 text-[12px] text-ink">
            <input
              type="checkbox"
              checked={on.on_build_plate_only ?? false}
              onChange={(event) => onChange({ ...on, on_build_plate_only: event.target.checked })}
            />
            On build plate only
          </label>
        </div>
      )}
    </fieldset>
  )
}
