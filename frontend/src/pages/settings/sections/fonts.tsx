import { RuntimeRows, SavedSection } from '../formParts'
import type { AreaId } from '../registry'
import { useBackendSettings } from '../useBackendSettings'
import { fieldsOf } from '../values'

export const id = 'fonts'
export const area: AreaId = 'library'
export const title = 'Fonts'
export const order = 30

/** Where fonts come from: the Google Fonts key and the catalogue's refresh. */
export function Component() {
  const form = useBackendSettings(id, (s) => fieldsOf(id, s))
  return (
    <SavedSection form={form} id={id} title={title}>
      <RuntimeRows form={form} section={id} />
    </SavedSection>
  )
}
