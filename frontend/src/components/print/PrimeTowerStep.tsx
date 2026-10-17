import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { StepCard } from './StepCard'
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
  return fromFile ? 'Default (file or process)' : 'Default (process: usually on with more than one filament)'
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
  // Nor under Default for a one-filament plate, which the slicer prints with no tower.
  const on =
    mode === 'on' ||
    (mode === 'default' &&
      !fromFile &&
      templateSettings?.enable_prime_tower !== '0' &&
      (colours === null || colours > 1))
  const noSparse = value?.no_sparse_layers ?? templateSettings?.wipe_tower_no_sparse_layers === '1'
  return (
    <StepCard title="Prime tower">
      <div className="grid gap-1.5">
        <Label htmlFor="prime-tower-select" className="text-xs font-normal text-muted-foreground">
          Prime tower
        </Label>
        <Select
          value={mode}
          onValueChange={(chosen) => {
            const next = chosen as Mode
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
        >
          <SelectTrigger id="prime-tower-select" className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="default">{defaultLabel(templateSettings, fromFile)}</SelectItem>
            <SelectItem value="on">On</SelectItem>
            <SelectItem value="off">Off</SelectItem>
          </SelectContent>
        </Select>
        {/* Only under Default, so it never reads as contradicting an explicit On (#3648 review). */}
        {colours !== null && colours <= 1 && mode === 'default' && (
          <p className="text-xs text-muted-foreground">
            This plate prints one filament, so the slicer leaves the tower out.
          </p>
        )}
        {mode === 'off' && colours !== null && colours > 1 && (
          <p className="text-xs text-destructive">
            Without a tower, a filament or nozzle change can leave blobs and gaps on the model.
          </p>
        )}
      </div>
      {on && (
        <Label className="cursor-pointer items-start pt-1 font-normal leading-snug">
          <Checkbox
            className="mt-0.5"
            checked={noSparse}
            // Intended: ticking it under Default makes the choice On, since No sparse
            // layers only applies with a tower. PrintPicker.test pins it (select reads On).
            onCheckedChange={(checked) => onChange({ enabled: true, no_sparse_layers: checked === true })}
          />
          <span>
            No sparse layers
            {/* Unchosen and undeclared, the preset decides: off in every Bambu one (#3648
                review). Not "process", which a query for the Process select would match. */}
            {value?.no_sparse_layers == null && templateSettings?.wipe_tower_no_sparse_layers === undefined && (
              <span className="text-muted-foreground"> (slicer default)</span>
            )}
            <span className="block text-xs text-muted-foreground">
              Print the tower only on layers that change filament, for a print whose colours
              change only in a few layers. Experimental in Bambu Studio: the nozzle travels
              down to the tower, so check nothing taller stands in its way.
            </span>
          </span>
        </Label>
      )}
    </StepCard>
  )
}
