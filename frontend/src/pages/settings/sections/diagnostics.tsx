import { RuntimeRows, SavedSection } from '../formParts'
import type { AreaId } from '../registry'
import { useBackendSettings } from '../useBackendSettings'
import { fieldsOf } from '../values'

export const id = 'diagnostics'
export const area: AreaId = 'system'
export const title = 'Diagnostics'
export const order = 10

/** The diagnostics settings, and every env-seeded field no other section has a control for (#322). */
export function Component() {
  const form = useBackendSettings(id, (s) => fieldsOf(id, s))
  return (
    <SavedSection form={form} id={id} title={title}>
      <RuntimeRows form={form} section="diagnostics" />
    </SavedSection>
  )
}
