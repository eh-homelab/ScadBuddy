import { AnswerersPicker } from '../../../components/flows/AnswerersPicker'
import { FieldRow } from '../controls'
import { docOf } from '../fields'
import { SavedSection } from '../formParts'
import type { AreaId } from '../registry'
import { useBackendSettings } from '../useBackendSettings'
import { answerersValue, fieldsOf } from '../values'

export const id = 'flow-answerers'
export const area: AreaId = 'assistant'
export const title = "Flows' questions"
export const order = 120

/**
 * #7471 — who besides a person may answer a flow's question (`AnswerersPicker`). Empty is
 * people only. Only a person saves it (the API refuses an agent with 403 `person_only`).
 */
export function Component() {
  const form = useBackendSettings(id, (s) => fieldsOf(id, s))
  const { value, setField, errors } = form
  const raw = value('flow_answerers')
  const rules = raw === '' ? [] : raw.split(',')

  return (
    <SavedSection form={form} id={id} title={title}>
      <FieldRow id="flow-answerers-who" {...docOf(form.settings, 'flow_answerers')} error={errors.flow_answerers}>
        <AnswerersPicker
          id="flow-answerers-who"
          rules={rules}
          onChange={(next) => setField('flow_answerers', answerersValue(next))}
        />
      </FieldRow>
    </SavedSection>
  )
}
