import { FilamentsEditor } from '../../../components/orders/FilamentsEditor'
import { RuntimeRows, SavedSection } from '../formParts'
import type { AreaId } from '../registry'
import { useBackendSettings } from '../useBackendSettings'
import { fieldsOf } from '../values'

export const id = 'orders'
export const area: AreaId = 'printing'
export const title = 'Orders'
export const order = 40

/** #4249, #4250 — what a new order records and how a quote is priced. */
export function Component() {
  const form = useBackendSettings(id, (s) => fieldsOf(id, s))
  return (
    <SavedSection
      form={form}
      id={id}
      title={title}
      description="What a new order records and how a quote is priced (#4249, #4250). A recorded quote keeps the rates it was priced at."
    >
      <RuntimeRows form={form} section="orders" />
      {/* #4250 — the price book's filaments, saved on their own: each edit is a row. */}
      <FilamentsEditor />
    </SavedSection>
  )
}
