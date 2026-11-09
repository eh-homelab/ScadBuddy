import { useState } from 'react'
import { USER_ONLY } from '../../agent/dom'
import { api, ApiError } from '../../api/client'
import { refusalText } from '../orders/refusal'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useAsync } from '../../lib/useAsync'

const KEK_MISSING = 'https://scadbuddy.dev/problems/storefront/kek_missing'

/** What to tell the person; the missing key file has its own sentence, with the fix. */
function failure(error: unknown): string {
  if (error instanceof ApiError && error.problem.type === KEK_MISSING) {
    return 'The key file is missing, so the password cannot be kept safe. Set SCADBUDDY_SECRET_KEY_FILE for the API and restart it.'
  }
  return refusalText(error)
}

/**
 * #5283 — the storefront's SMTP password, sealed under the API's key file as a payment
 * provider's secret is. It is typed here and never read back: the page shows only
 * whether one is set.
 */
export function StorefrontMailPassword() {
  const listed = useAsync(() => api.storefrontMailPassword(), [])
  const [set, setSet] = useState<boolean | null>(null)
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const shown = set ?? listed.data?.set ?? null

  async function run(action: () => Promise<{ set: boolean }>) {
    setBusy(true)
    setError(null)
    try {
      const next = await action()
      setSet(next.set)
      // The password leaves the page as soon as it is sent.
      setPassword('')
    } catch (err) {
      setError(failure(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form
      className="mt-4 space-y-2 rounded-md border p-3"
      onSubmit={(event) => {
        event.preventDefault()
        void run(() => api.setStorefrontMailPassword(password))
      }}
    >
      <Label htmlFor="storefront-mail-password">SMTP password</Label>
      <p className="text-xs text-muted-foreground">
        Stored sealed under SCADBUDDY_SECRET_KEY_FILE and never shown again.{' '}
        {shown === null ? null : shown ? <span>Password set</span> : <span>No password set</span>}
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <Input
          id="storefront-mail-password"
          type="password"
          autoComplete="off"
          className="w-56"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        <Button type="submit" size="sm" disabled={busy || password === ''} {...USER_ONLY}>
          Save password
        </Button>
        {shown && (
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={() => void run(() => api.forgetStorefrontMailPassword())}
            {...USER_ONLY}
          >
            Forget password
          </Button>
        )}
      </div>
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
    </form>
  )
}
