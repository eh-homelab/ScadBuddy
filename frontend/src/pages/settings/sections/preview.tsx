import { api } from '../../../api/client'
import { ChoiceSelect } from '../../../components/settings/form'
import { useAsync } from '../../../lib/useAsync'
import { plateSize, setDisplayUnit, type DisplayUnit } from '../../../lib/units'
import { useSettingsAgentField } from '../agentFields'
import { FieldRow } from '../controls'
import { FieldBadge, SavedSection } from '../formParts'
import type { AreaId } from '../registry'
import { useBackendSettings } from '../useBackendSettings'
import { fieldsOf } from '../values'

export const id = 'preview'
export const area: AreaId = 'library'
export const title = 'Preview'
export const order = 60

/** The unit dimensions are shown in, and the plate the customizer falls back to (#81). */
export function Component() {
  const form = useBackendSettings(id, (s) => fieldsOf(id, s), {
    onSaved: (next) => setDisplayUnit(next.display_unit),
  })
  const { value, setField, errors } = form
  // #81 — needs no Bambuddy: the plates are ScadBuddy's own table.
  const platesState = useAsync(() => api.listPlates(), [])
  const plateNames = (platesState.data?.plates ?? []).map((plate) => plate.name)
  const plate = value('default_plate')
  const unit = value('display_unit') as DisplayUnit

  useSettingsAgentField('default_plate', {
    value: () => value('default_plate'),
    set: (next) => setField('default_plate', next),
    allowed: () => ['', ...plateNames, ...(plate && !plateNames.includes(plate) ? [plate] : [])],
    listed: () => plateNames,
  })
  useSettingsAgentField('display_unit', {
    value: () => value('display_unit'),
    set: (next) => setField('display_unit', next),
    allowed: () => ['mm', 'in'],
    listed: () => ['mm', 'in'],
  })

  return (
    <SavedSection form={form} id={id} title={title}>
      <FieldRow
        id="display-unit"
        label="Show dimensions in"
        help="For every model: the bounding box, plate sizes and fit warnings. Models, parameters and the files sent to Bambuddy stay in millimetres."
      >
        <ChoiceSelect
          id="display-unit"
          value={value('display_unit')}
          onValueChange={(next) => setField('display_unit', next)}
          choices={[
            { value: 'mm', label: 'Millimetres (mm)' },
            { value: 'in', label: 'Inches (in)' },
          ]}
        />
      </FieldRow>

      <FieldRow
        id="default-plate"
        label="Default plate"
        badge={<FieldBadge form={form} name="default_plate" />}
        error={errors.default_plate}
        help="The plate the customizer draws and checks the model against until a printer is chosen in the print picker."
      >
        <ChoiceSelect
          id="default-plate"
          value={plate}
          onValueChange={(next) => setField('default_plate', next)}
          choices={[
            { value: '', label: plateSize([256, 256], unit) },
            // A value set through SCADBUDDY_DEFAULT_PLATE may be a code ("A1M").
            ...(plate && !plateNames.includes(plate) ? [{ value: plate, label: plate }] : []),
            ...(platesState.data?.plates ?? []).map((entry) => ({
              value: entry.name,
              label: `${entry.name} (${plateSize(entry.size, unit)})`,
            })),
          ]}
        />
      </FieldRow>
    </SavedSection>
  )
}
