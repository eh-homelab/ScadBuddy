import { AppLink } from '../../../components/AppLink'
import { FieldRow } from '../controls'
import { RuntimeRows, SavedSection } from '../formParts'
import type { AreaId } from '../registry'
import { useBackendSettings } from '../useBackendSettings'
import { fieldsOf } from '../values'

export const id = 'filament'
export const area: AreaId = 'printing'
export const title = 'Filament'
export const order = 30

const LINK = 'underline underline-offset-2 hover:text-foreground'

/**
 * #6262 — the configuration the Filament page leans on: how Bambuddy's spools reach
 * ScadBuddy's own, and the numbers a spool falls back on. The spools themselves, their
 * types and tags are not here; they are on the Filament page.
 */
export function Component() {
  const form = useBackendSettings(id, (s) => fieldsOf(id, s))
  return (
    <SavedSection
      form={form}
      id={id}
      title={title}
      description={
        <>
          Spools, types and tags are in{' '}
          <AppLink href="/filament" className={LINK}>
            Filament
          </AppLink>
          .
        </>
      }
    >
      <RuntimeRows form={form} section="filament" />
      <FieldRow
        id="filament-currency"
        label="Currency"
        help="Costs are in the shop's currency, which an order copies when it is made."
      >
        <p id="filament-currency" className="text-sm">
          <span className="sb-num font-medium">{form.settings.orders_currency}</span>
          {' · '}
          <AppLink href="/settings/printing#orders" className={LINK}>
            Change it in Orders
          </AppLink>
        </p>
      </FieldRow>
    </SavedSection>
  )
}
