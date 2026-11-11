import { Checkbox } from '@/components/ui/checkbox'
import { SwitchField } from '../../../components/settings/form'
import { useBambuddyResources } from '../bambuddyResources'
import { FieldRow } from '../controls'
import { SavedSection } from '../formParts'
import type { AreaId } from '../registry'
import { useBackendSettings } from '../useBackendSettings'
import { AUTO_IMPORT_SOURCES, type AutoImportSource, fieldsOf, printerIdsValue, sourcesValue } from '../values'

export const id = 'print-watcher'
export const area: AreaId = 'printing'
export const title = 'Print watcher'
export const order = 15

const SOURCE_LABEL: Record<AutoImportSource, string> = {
  archive: "The archive's file",
  archive_source: "The archive's slicer project",
  library: 'A library file with the same hash',
  printer: "The printer's storage",
}

/** The values the form holds for a list field: comma-joined (`values.ts` `baseline`). */
function listOf(value: string): string[] {
  return value === '' ? [] : value.split(',')
}

/**
 * #4699 — the print watcher: whether it imports the prints ScadBuddy did not start, where
 * it may take a print's 3MF from, and which Bambuddy printers it leaves alone. Only a
 * person saves these (the API refuses an agent with 403 `person_only`), because the
 * watcher reads Bambuddy on its own and, with the printer source, the printers too.
 */
export function Component() {
  const { targets } = useBambuddyResources()
  const form = useBackendSettings(id, (s) => fieldsOf(id, s))
  const { value, setField, errors } = form
  const on = value('print_auto_import') === 'true'
  const sources = listOf(value('print_auto_import_sources'))
  const skipped = listOf(value('print_auto_import_skip_printers')).map(Number)
  const printers = targets.data?.printers ?? []
  // A skipped printer Bambuddy no longer lists stays shown, so it can be cleared.
  const listed = [
    ...printers.map((printer) => ({ id: printer.id, name: printer.name })),
    ...skipped.filter((pid) => !printers.some((printer) => printer.id === pid)).map((pid) => ({ id: pid, name: `Printer ${pid}` })),
  ]

  function toggleSource(source: AutoImportSource, checked: boolean) {
    const next = checked ? [...sources, source] : sources.filter((one) => one !== source)
    setField('print_auto_import_sources', sourcesValue(next))
  }

  function toggleSkip(printerId: number, checked: boolean) {
    const next = checked ? [...skipped, printerId] : skipped.filter((one) => one !== printerId)
    setField('print_auto_import_skip_printers', printerIdsValue(next))
  }

  return (
    <SavedSection form={form} id={id} title={title}>
      <FieldRow
        id="print-auto-import"
        label="Watcher"
        help="Every 5 minutes, it reads the archives Bambuddy made since it was switched on. It links a print to a model only when it is sure; every other print goes to Unsorted imports on the Prints page."
        error={errors.print_auto_import}
      >
        <SwitchField
          id="print-auto-import"
          label="Import the prints ScadBuddy did not start"
          checked={on}
          onCheckedChange={(checked) => setField('print_auto_import', checked ? 'true' : 'false')}
        />
      </FieldRow>
      <FieldRow
        id="print-auto-import-sources"
        label="Take a print's 3MF from"
        help="The watcher tries these in this order and takes the first that has the file. With none, every print goes to Unsorted imports."
        error={errors.print_auto_import_sources}
      >
        <fieldset id="print-auto-import-sources" className="flex flex-col gap-1.5">
          {AUTO_IMPORT_SOURCES.map((source) => (
            <label key={source} className="flex items-start gap-2 text-sm">
              <Checkbox
                aria-label={SOURCE_LABEL[source]}
                checked={sources.includes(source)}
                onCheckedChange={(checked) => toggleSource(source, checked === true)}
                className="mt-0.5"
              />
              <span className="flex flex-col">
                <span>{SOURCE_LABEL[source]}</span>
                {source === 'printer' && (
                  <span className="text-xs text-warn">
                    The watcher then reads the printer's storage over FTP by itself, while the print runs, and keeps the
                    file in Bambuddy's library inbox folder.
                  </span>
                )}
              </span>
            </label>
          ))}
        </fieldset>
      </FieldRow>
      <FieldRow
        id="print-auto-import-skip"
        label="Leave these printers alone"
        help="The watcher takes no print from a printer checked here."
        error={errors.print_auto_import_skip_printers}
      >
        <fieldset id="print-auto-import-skip" className="flex flex-col gap-1.5">
          {listed.length === 0 ? (
            <span className="text-xs text-muted-foreground">
              {targets.error ? "Could not read Bambuddy's printers." : 'No Bambuddy printers.'}
            </span>
          ) : (
            listed.map((printer) => (
              <label key={printer.id} className="flex items-center gap-2 text-sm">
                <Checkbox
                  aria-label={`Skip ${printer.name}`}
                  checked={skipped.includes(printer.id)}
                  onCheckedChange={(checked) => toggleSkip(printer.id, checked === true)}
                />
                <span>{printer.name}</span>
              </label>
            ))
          )}
        </fieldset>
      </FieldRow>
    </SavedSection>
  )
}
