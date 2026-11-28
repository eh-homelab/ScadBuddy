import { propertiesOf, requiredOf } from '../../components/entities/definedProperties'
import { useAsync } from '../../lib/useAsync'
import { inventoryApi, type KindSchema } from './api'

/**
 * #6949 K1 (R-K1-33) — the schema an item form shows as fields: the kind's merged schema, with
 * `required` the keys an item of the kind must have (`item_required`: the schema's own, then
 * its special type's, such as a printer's `model_code`). A required key the schema does not
 * define is a text field, so the form can still ask for it.
 */
export function formSchema(kind: KindSchema): Record<string, unknown> {
  const required = kind.item_required ?? requiredOf(kind.schema)
  const properties = { ...propertiesOf(kind.schema) } as Record<string, unknown>
  for (const key of required) properties[key] ??= { type: 'string' }
  return { ...kind.schema, properties, required }
}

/** The form schema of an item of `kindId`, read again when the kind changes; null for no kind. */
export function useItemSchema(kindId: string | null | undefined) {
  return useAsync(() => (kindId ? inventoryApi.kindSchema(kindId).then(formSchema) : Promise.resolve(null)), [kindId])
}
