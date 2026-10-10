type Props = {
  /** The template's `print_settings`: Bambu Studio process keys and values. */
  settings: Record<string, string> | undefined
}

/** Readable names for the process keys templates set; any other key shows as itself. */
const LABELS: Record<string, string> = {
  enable_prime_tower: 'Prime tower',
  wipe_tower_no_sparse_layers: 'No sparse layers on the prime tower',
  enable_support: 'Supports',
  support_type: 'Support type',
  brim_type: 'Brim',
  print_sequence: 'Print sequence',
  layer_height: 'Layer height',
  sparse_infill_density: 'Infill density',
  wall_loops: 'Walls',
  top_shell_layers: 'Top shell layers',
  bottom_shell_layers: 'Bottom shell layers',
}

/** Keys whose value is Bambu's `0` / `1` switch. */
const SWITCHES = new Set(['enable_prime_tower', 'wipe_tower_no_sparse_layers', 'enable_support'])

function valueOf(key: string, value: string): string {
  if (SWITCHES.has(key) && (value === '0' || value === '1')) return value === '1' ? 'On' : 'Off'
  return value
}

/**
 * #1294 — the slicer settings the template applies to every slice and download, shown so
 * the print's prime tower or supports are not a surprise. Read-only: nothing here sets
 * them. Shown only when the template has some.
 */
export function SlicerDefaults({ settings }: Props) {
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
            <dd className="text-ink">{valueOf(key, value)}</dd>
          </div>
        ))}
      </dl>
    </section>
  )
}
