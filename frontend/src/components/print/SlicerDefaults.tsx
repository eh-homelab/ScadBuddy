type Props = {
  /** The template's `print_settings`: Bambu Studio process keys and values. */
  settings: Record<string, string> | undefined
  /** Keys this print sets itself in a step above (#2112): listed, but marked as not applying. */
  overridden?: readonly string[]
}

/**
 * Readable names for the process keys a template may set: exactly the backend's
 * `PRINT_SETTING_VALUES` (`backend/scadbuddy/library/catalogue.py`). Any other key shows as
 * itself.
 */
const LABELS: Record<string, string> = {
  enable_prime_tower: 'Prime tower',
  wipe_tower_no_sparse_layers: 'No sparse layers on the prime tower',
  enable_support: 'Supports',
  support_type: 'Support type',
  support_threshold_angle: 'Support threshold angle',
  support_on_build_plate_only: 'Supports on build plate only',
  brim_width: 'Brim width',
  brim_type: 'Brim',
  print_sequence: 'Print sequence',
}

/** Keys whose value is a length in millimetres. */
const MILLIMETRES = new Set(['brim_width'])

/** Keys whose value is Bambu's `0` / `1` switch. */
const SWITCHES = new Set([
  'enable_prime_tower',
  'wipe_tower_no_sparse_layers',
  'enable_support',
  'support_on_build_plate_only',
])

/** Keys whose value is an angle in degrees (#1899). */
const DEGREES = new Set(['support_threshold_angle'])

function valueOf(key: string, value: string): string {
  if (SWITCHES.has(key) && (value === '0' || value === '1')) return value === '1' ? 'On' : 'Off'
  if (MILLIMETRES.has(key)) return `${value} mm`
  if (DEGREES.has(key)) return `${value}°`
  return value
}

/**
 * #1294 — the slicer settings the template applies to every slice and download, shown so
 * the print's prime tower or supports are not a surprise. Read-only: nothing here sets
 * them. Shown only when the template has some.
 */
export function SlicerDefaults({ settings, overridden = [] }: Props) {
  const entries = Object.entries(settings ?? {})
  if (entries.length === 0) return null
  return (
    <section
      aria-labelledby="slicer-defaults-heading"
      className="rounded-[6px] border border-line bg-surface-2 px-3 py-2"
    >
      <h3 id="slicer-defaults-heading" className="text-[13px] text-ink">
        Slicer defaults from this template
      </h3>
      <dl className="mt-1.5 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-[12px]">
        {entries.map(([key, value]) => (
          <div key={key} className="contents">
            <dt className="text-muted">{LABELS[key] ?? key}</dt>
            {overridden.includes(key) ? (
              <dd className="text-muted">
                <del>{valueOf(key, value)}</del> (overridden above)
              </dd>
            ) : (
              <dd className="text-ink">{valueOf(key, value)}</dd>
            )}
          </div>
        ))}
      </dl>
    </section>
  )
}
