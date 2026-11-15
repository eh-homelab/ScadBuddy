import { StorefrontMailPassword } from '../../../components/settings/StorefrontMailPassword'
import { RuntimeRows, SavedSection } from '../formParts'
import type { AreaId } from '../registry'
import { useBackendSettings } from '../useBackendSettings'
import { fieldsOf } from '../values'

export const id = 'storefront-mail'
export const area: AreaId = 'system'
export const title = 'Storefront mail'
export const order = 19

/** #5283 — the SMTP server the storefront's mail goes out through; the password is saved on its own. */
export function Component() {
  const form = useBackendSettings(id, (s) => fieldsOf(id, s))
  return (
    <SavedSection
      form={form}
      id={id}
      title={title}
      description="The storefront's customers get their order link, sign-in codes and order updates by mail through this server. A housekeeping pass retries every few minutes what did not go out."
    >
      <RuntimeRows form={form} section={id} />
      {/* #5283 — sealed, saved on its own, never read back. */}
      <StorefrontMailPassword />
    </SavedSection>
  )
}
