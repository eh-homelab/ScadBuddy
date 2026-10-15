import type { PrimeTowerChoice } from '../../api/types'

type Props = {
  /** `null` leaves the tower to the template's `print_settings` and the process. */
  value: PrimeTowerChoice | null
  onChange: (next: PrimeTowerChoice | null) => void
  /** The template's `print_settings`, for what Default means for this model. */
  templateSettings?: Record<string, string> | undefined
  /** A library 3MF: its own project settings, unread here, may declare the tower. */
  fromFile?: boolean
  /**
   * How many filaments the chosen plate prints (every plate's under "All plates"): the
   * tower only matters past one. `null` while they load, which says nothing either way.
   */
  colours: number | null
}

type Mode = 'default' | 'on' | 'off'

function modeOf(value: PrimeTowerChoice | null): Mode {
  if (value === null) return 'default'
  return value.enabled ? 'on' : 'off'
}

/** What Default prints with: the template's declaration, else the process preset's. */
function defaultLabel(settings: Record<string, string> | undefined, fromFile: boolean): string {
  if (settings?.enable_prime_tower === '1') return 'Default (template: on)'
  if (settings?.enable_prime_tower === '0') return 'Default (template: off)'
  return fromFile ? 'Default (file or process)' : 'Default (on with more than one filament)'
}

/**
 * #3628 — Bambu Studio's prime (purge) tower for this print, and its "No sparse layers"
 * (`wipe_tower_no_sparse_layers`): the tower printed only on layers that change filament.
 * Default leaves both to the template, else the process, which prints a tower whenever a
 * plate uses more than one filament. Per print, never remembered.
 */
export function PrimeTowerStep({ value, onChange, templateSettings, fromFile = false, colours }: Props) {
  const mode = modeOf(value)
  // On, chosen here or by Default when nothing says off. A library 3MF's own settings are
  // unread here, so under Default its "No sparse layers" is not shown as if known.
  const on =
    mode === 'on' || (mode === 'default' && !fromFile && templateSettings?.enable_prime_tower !== '0')
  const noSparse = value?.no_sparse_layers ?? templateSettings?.wipe_tower_no_sparse_layers === '1'
  return (
    <fieldset className="rounded-[6px] border border-line bg-surface-2 px-3 py-2">
      <legend className="px-1 text-[13px] text-ink">Prime tower</legend>
      <div className="mt-1.5 flex flex-col gap-1">
        <label htmlFor="prime-tower-select" className="text-[12px] text-muted">
          Prime tower
        </label>
        <select
          id="prime-tower-select"
          value={mode}
          onChange={(event) => {
            const next = event.target.value as Mode
            if (next === 'default') onChange(null)
            else if (next === 'off') onChange({ enabled: false })
            // Keeps a "No sparse layers" chosen before, so switching back is not a reset.
            else
              onChange(
                value?.no_sparse_layers == null
                  ? { enabled: true }
                  : { enabled: true, no_sparse_layers: value.no_sparse_layers },
              )
          }}
          className="sb-field"
        >
          <option value="default">{defaultLabel(templateSettings, fromFile)}</option>
          <option value="on">On</option>
          <option value="off">Off</option>
        </select>
        {colours !== null && colours <= 1 && (
          <p className="text-[12px] text-muted">
            This plate prints one filament, so the slicer leaves the tower out either way.
          </p>
        )}
        {mode === 'off' && colours !== null && colours > 1 && (
          <p className="text-[12px] text-warn">
            Without a tower, a filament or nozzle change can leave blobs and gaps on the model.
          </p>
        )}
      </div>
      {on && (
        <label className="mt-2 flex items-start gap-2 text-[12px] text-ink">
          <input
            type="checkbox"
            className="mt-0.5"
            checked={noSparse}
            onChange={(event) => onChange({ enabled: true, no_sparse_layers: event.target.checked })}
          />
          <span>
            No sparse layers
            {/* Unchosen and undeclared, the preset decides: off in every Bambu one (#3648
                review). Not "process", which a query for the Process select would match. */}
            {value?.no_sparse_layers == null && templateSettings?.wipe_tower_no_sparse_layers === undefined && (
              <span className="text-muted"> (slicer default)</span>
            )}
            <span className="block text-muted">
              Print the tower only on layers that change filament, for a print whose colours
              change only in a few layers. Experimental in Bambu Studio: the nozzle travels
              down to the tower, so check nothing taller stands in its way.
            </span>
          </span>
        </label>
      )}
    </fieldset>
  )
}
