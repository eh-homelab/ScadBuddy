import type { ProviderStatus } from '../../api/types'
import { When } from '../orders/When'

/** One fixed sentence per category: the backend keeps no provider text to show (#5684). */
const TEXT: Record<NonNullable<ProviderStatus['last_webhook_error']>, string> = {
  signature: 'its signature did not match. Check the signing secret.',
  provider_unavailable: 'the provider did not answer. The provider sends the event again.',
  handler_error: 'ScadBuddy could not record it. Check the API log.',
}

/** #5684 — why the provider's last webhook failed, until the next one succeeds. */
export function WebhookError({ error, at }: { error: ProviderStatus['last_webhook_error']; at: string | null }) {
  if (!error) return null
  return (
    <p role="status" className="text-[13px] text-destructive">
      The last webhook failed{at && (
        <>
          {' '}
          <When at={at} />
        </>
      )}
      : {TEXT[error]}
    </p>
  )
}
