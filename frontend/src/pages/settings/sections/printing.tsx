import { Input } from '@/components/ui/input'
import { ChoiceSelect } from '../../../components/settings/form'
import { useSettingsAgentField } from '../agentFields'
import { api } from '../../../api/client'
import { useAsync } from '../../../lib/useAsync'
import { FieldRow } from '../controls'
import { RuntimeRows, SavedSection } from '../formParts'
import type { AreaId } from '../registry'
import { useBackendSettings } from '../useBackendSettings'
import { RETENTION_MAX_DAYS, fieldsOf } from '../values'

export const id = 'printing'
export const area: AreaId = 'printing'
export const title = 'Printing defaults'
export const order = 10

/**
 * The printer a print starts on, and how long finished print runs and operations are kept.
 * #5167 — the printer is a registry printer, a LAN-only one too, saved as `default_printer`.
 */
export function Component() {
  const registry = useAsync(() => api.listPrinters(), [])
  const form = useBackendSettings(id, (s) => fieldsOf(id, s))
  const { value, setField, errors } = form
  const printers = (registry.data ?? []).filter((printer) => printer.kind === 'physical')
  const current = value('default_printer')
  // A default the list does not hold (the list still loading, or a printer gone) stays a
  // choice, so the select never shows a value it does not offer.
  const unlisted = current !== '' && !printers.some((printer) => printer.id === current)

  useSettingsAgentField('default_printer', {
    value: () => value('default_printer'),
    set: (next) => setField('default_printer', next),
    allowed: () => ['', ...printers.map((printer) => printer.id)],
    listed: () => printers.map((printer) => ({ value: printer.id, name: printer.name })),
  })

  return (
    <SavedSection form={form} id={id} title={title}>
      <FieldRow id="printer" label="Printer">
        <ChoiceSelect
          id="printer"
          value={current}
          onValueChange={(next) => setField('default_printer', next)}
          choices={[
            { value: '', label: 'None' },
            ...printers.map((printer) => ({
              value: printer.id,
              label: `${printer.name}${printer.model ? ` (${printer.model})` : ''}`,
            })),
            ...(unlisted ? [{ value: current, label: registry.data ? 'A printer that is gone' : 'Loading…' }] : []),
          ]}
        />
      </FieldRow>
      <FieldRow
        id="print-run-retention"
        label="Keep finished print runs for (days)"
        help="Empty keeps every run, the start of print history. A number deletes runs that finished longer ago."
        error={errors.print_run_retention_seconds}
      >
        <Input
          id="print-run-retention"
          type="number"
          min="1"
          max={RETENTION_MAX_DAYS}
          step="any"
          inputMode="decimal"
          placeholder="Forever"
          value={value('print_run_retention_seconds')}
          onChange={(event) => setField('print_run_retention_seconds', event.target.value)}
        />
      </FieldRow>
      {/* The operations record (#1053). */}
      <FieldRow
        id="operation-retention"
        label="Keep finished Bambuddy operations for (days)"
        help="Sends, reprints, project filing and the like. Empty keeps every record; a number deletes those that finished longer ago. Keep it at least as long as Temporal's namespace retention, or a retried request whose record is gone is told it may have been done instead of its outcome."
        error={errors.operation_retention_seconds}
      >
        <Input
          id="operation-retention"
          type="number"
          min="1"
          max={RETENTION_MAX_DAYS}
          step="any"
          inputMode="decimal"
          placeholder="Forever"
          value={value('operation_retention_seconds')}
          onChange={(event) => setField('operation_retention_seconds', event.target.value)}
        />
      </FieldRow>
      <RuntimeRows form={form} section="printing" />
    </SavedSection>
  )
}
