import { useState } from 'react'
import { USER_ONLY } from '../../agent/dom'
import { api, ApiError } from '../../api/client'
import type { BankKeys, BankStatus } from '../../api/types'
import { refusalText } from '../orders/refusal'
import { openPlaidLink } from '../../lib/plaidLink'
import { useAsync } from '../../lib/useAsync'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'

const KEK_MISSING = 'https://scadbuddy.dev/problems/payments/kek_missing'

function failure(error: unknown): string {
  if (error instanceof ApiError && error.problem.type === KEK_MISSING) {
    return 'The key file is missing, so the Plaid keys cannot be kept safe. Set SCADBUDDY_SECRET_KEY_FILE for the API and restart it.'
  }
  return refusalText(error)
}

/**
 * #4961 — the shop's checking or savings account, linked through Plaid Link. Plaid is not a
 * payment provider: it shows deposits, it takes no payment. The page shows the account's
 * name, type and mask only; the keys are typed here and never read back.
 */
export function BankAccount() {
  const listed = useAsync(() => api.bankLink(), [])
  const [status, setStatus] = useState<BankStatus | null>(null)
  const [editing, setEditing] = useState(false)
  const [keys, setKeys] = useState<BankKeys>({ environment: 'production', client_id: '', secret: '' })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [note, setNote] = useState<string | null>(null)
  const shown = status ?? listed.data

  async function run(action: () => Promise<BankStatus | null | void>) {
    setBusy(true)
    setError(null)
    try {
      const next = await action()
      setStatus(next ?? (await api.bankLink()))
      return true
    } catch (err) {
      setError(failure(err))
      return false
    } finally {
      setBusy(false)
    }
  }

  async function saveKeys() {
    if (await run(() => api.saveBankKeys(keys))) {
      // The secret leaves the page as soon as it is sent.
      setKeys({ ...keys, client_id: '', secret: '' })
      setEditing(false)
    }
  }

  async function unlink() {
    await run(async () => {
      setNote((await api.unlinkBank()).note)
    })
  }

  async function link() {
    await run(async () => {
      const { link_token } = await api.bankLinkToken()
      const chosen = await openPlaidLink(link_token)
      return chosen ? api.linkBank(chosen) : null
    })
  }

  const form = shown && (!shown.configured || editing) && !shown.account
  return (
    <section aria-label="Bank account" className="space-y-2 rounded-md border p-3">
      <h4 className="text-sm font-medium">Bank account (Plaid)</h4>
      <p className="text-[13px] text-muted-foreground">
        Link the shop&apos;s checking or savings account. Matching its deposits to orders comes in a later step of #4961.
      </p>
      <p className="text-[13px] text-muted-foreground">Stripe payouts go to the bank set in your Stripe dashboard.</p>
      {listed.error && !shown && (
        <p role="alert" className="text-sm text-warn">
          {listed.error.message}
        </p>
      )}
      {shown?.account && (
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-[13px]">
            <span className="font-medium">{shown.account.name}</span>
            <span className="text-muted-foreground">
              {' · '}
              {shown.account.type}
              {shown.account.mask ? ` · ending ${shown.account.mask}` : ''}
            </span>
          </p>
          <Button size="sm" variant="outline" disabled={busy} onClick={() => void unlink()} {...USER_ONLY}>
            Unlink
          </Button>
        </div>
      )}
      {shown?.configured && !shown.account && (
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-[13px] text-muted-foreground">Plaid keys saved ({shown.environment})</p>
          <div className="flex gap-2">
            <Button size="sm" disabled={busy} onClick={() => void link()} {...USER_ONLY}>
              Link a bank account
            </Button>
            <Button size="sm" variant="outline" disabled={busy} onClick={() => setEditing(!editing)} {...USER_ONLY}>
              {editing ? 'Keep keys' : 'Replace keys'}
            </Button>
            <Button size="sm" variant="outline" disabled={busy} onClick={() => void run(() => api.forgetBankKeys())} {...USER_ONLY}>
              Forget keys
            </Button>
          </div>
        </div>
      )}
      {form && (
        <form
          className="space-y-2"
          onSubmit={(event) => {
            event.preventDefault()
            void saveKeys()
          }}
        >
          <fieldset>
            <legend className="text-[13px]">Plaid environment</legend>
            <RadioGroup
              name="plaid-environment"
              value={keys.environment}
              onValueChange={(next) => setKeys({ ...keys, environment: next as BankKeys['environment'] })}
              className="mt-1 flex gap-4"
            >
              {(['production', 'sandbox'] as const).map((env) => (
                <div key={env} className="flex items-center gap-2">
                  <RadioGroupItem id={`plaid-env-${env}`} value={env} />
                  <Label htmlFor={`plaid-env-${env}`} className="font-normal">
                    {env === 'production' ? 'Production' : 'Sandbox'}
                  </Label>
                </div>
              ))}
            </RadioGroup>
          </fieldset>
          <div className="flex flex-wrap items-end gap-2">
            <div className="flex flex-col gap-1">
              <Label htmlFor="plaid-client-id">Client ID</Label>
              <Input id="plaid-client-id" autoComplete="off" className="w-56" value={keys.client_id} onChange={(e) => setKeys({ ...keys, client_id: e.target.value })} />
            </div>
            <div className="flex flex-col gap-1">
              <Label htmlFor="plaid-secret">Secret</Label>
              <Input id="plaid-secret" type="password" autoComplete="off" className="w-56" value={keys.secret} onChange={(e) => setKeys({ ...keys, secret: e.target.value })} />
            </div>
            <Button type="submit" size="sm" disabled={busy} {...USER_ONLY}>
              Save keys
            </Button>
          </div>
        </form>
      )}
      {note && (
        <p role="status" className="text-[13px] text-warn">
          {note}
        </p>
      )}
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
    </section>
  )
}
