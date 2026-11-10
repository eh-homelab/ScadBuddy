import { useState } from 'react'
import { USER_ONLY } from '../../agent/dom'
import { api, ApiError } from '../../api/client'
import { refusalText } from '../orders/refusal'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useAsync } from '../../lib/useAsync'
import { When } from '../orders/When'

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
 * provider's secret is, and bound to the host, port and user set when it was saved. It
 * is typed here and never read back: the page shows only whether one is set, and for
 * which connection. Below it, the mail server's last failure, which no mail was charged for.
 */
export function StorefrontMailPassword() {
  const [version, setVersion] = useState(0)
  const listed = useAsync(() => api.storefrontMail(), [version])
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const status = listed.data
  const shown = status ? status.password_set : null

  async function run(action: () => Promise<{ set: boolean }>) {
    setBusy(true)
    setError(null)
    try {
      await action()
      setVersion((v) => v + 1)
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
        {shown === null ? null : shown ? <span>Password set for {status?.password_for}</span> : <span>No password set</span>}
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <Input
          id="storefront-mail-password"
          type="password"
          autoComplete="new-password"
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
      {status?.server_error && (
        <p role="status" className="text-[13px] text-warn">
          The mail server failed: {status.server_error}
          {status.server_error_at && (
            <>
              {' '}
              (<When at={status.server_error_at} />)
            </>
          )}
          . Queued mail waits and is tried again.
        </p>
      )}
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
    </form>
  )
}
