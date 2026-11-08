import { FilamentsEditor } from '../../../components/orders/FilamentsEditor'
import { MachineRates } from '../../../components/orders/MachineRates'
import { DepositPolicyEditor } from '../../../components/settings/DepositPolicyEditor'
import { ShippingOptionsEditor } from '../../../components/settings/ShippingOptionsEditor'
import { RuntimeRows, SavedSection } from '../formParts'
import type { AreaId } from '../registry'
import { useBackendSettings } from '../useBackendSettings'
import { fieldsOf } from '../values'

export const id = 'orders'
export const area: AreaId = 'printing'
export const title = 'Orders'
export const order = 40

/** #4249, #4250 — what a new order records and how a quote is priced; #5281 — tax, deposits and shipping. */
export function Component() {
  const form = useBackendSettings(id, (s) => fieldsOf(id, s))
  return (
    <SavedSection
      form={form}
      id={id}
      title={title}
      description="What a new order records, how a quote is priced, and the tax, deposits and shipping options an order takes (#4249, #4250, #5281). A recorded quote keeps the rates it was priced at."
    >
      <RuntimeRows form={form} section="orders" />
      {/* #4250 — the price book's filaments, saved on their own: each edit is a row. */}
      <FilamentsEditor />
      {/* #4625 — each printer's machine rate, from its inputs, saved on its own. */}
      <MachineRates />
      {/* #5281 — the deposit policy and the shipping options, each saved on its own. */}
      <DepositPolicyEditor />
      <ShippingOptionsEditor />
    </SavedSection>
  )
}
