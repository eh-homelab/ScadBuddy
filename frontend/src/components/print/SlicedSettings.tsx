import type { SlicedProfile } from '../../api/types'

/**
 * #2671 — a sliced file's slicer choices, read-only: they are baked into its G-code, so
 * the print queues it as it is. Stands in for the nozzle, quality, plate type and print
 * sequence steps.
 */
export function SlicedSettings({ profile }: { profile: SlicedProfile }) {
  const nozzles = profile.nozzle_diameters ?? []
  const rows: [string, string | null | undefined][] = [
    ['Printer', profile.printer_model],
    ['Nozzle', nozzles.length > 0 ? `${nozzles.join(' / ')} mm` : null],
    ['Process', profile.process],
    ['Plate type', profile.bed_type],
  ]
  return (
    <section
      aria-label="Sliced settings"
      data-testid="sliced-settings"
      className="rounded-[6px] border border-line bg-surface-2 px-3 py-2 text-[13px]"
    >
      <p className="text-ink">Sliced already: Bambuddy queues it as it is, with these settings.</p>
      <dl className="mt-1.5 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-0.5 text-[12.5px]">
        {rows.map(([name, value]) => (
          <div key={name} className="contents">
            <dt className="text-muted">{name}</dt>
            <dd className="min-w-0 truncate text-ink">{value || 'not stated in the file'}</dd>
          </div>
        ))}
      </dl>
    </section>
  )
}
