import { useEffect, useState } from 'react'
import { USER_ONLY } from '../../../agent/dom'
import { api, ApiError } from '../../../api/client'
import type { ProviderStatus } from '../../../api/types'
import { When } from '../../../components/orders/When'
import { WebhookError } from '../../../components/settings/WebhookError'
import { refusalText } from '../../../components/orders/refusal'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useAsync } from '../../../lib/useAsync'
import { useSettingsCache } from '../cache'
import { Section } from '../controls'
import type { AreaId } from '../registry'

export const id = 'payments'
export const area: AreaId = 'system'
export const title = 'Payments'
export const order = 18

const KEK_MISSING = 'https://scadbuddy.dev/problems/payments/kek_missing'

/** What to tell the person; the missing key file has its own sentence, with the fix. */
function failure(error: unknown): string {
  if (error instanceof ApiError && error.problem.type === KEK_MISSING) {
    return 'The key file is missing, so the credentials cannot be kept safe. Set SCADBUDDY_SECRET_KEY_FILE for the API and restart it.'
  }
  return refusalText(error)
}

/** #4957 — the payment providers and the shop's account at each. A secret is typed here and never read back. */
export function Component() {
  const listed = useAsync(() => api.paymentProviders(), [])
  const [rows, setRows] = useState<ProviderStatus[] | null>(null)
  const shown = rows ?? listed.data ?? []
  return (
    <Section
      id={id}
      title={title}
      description="Connect the shop's account at a payment provider to send customers a payment link. The provider tells ScadBuddy when a customer has paid."
    >
      {listed.error && !rows ? (
        <p role="alert" className="text-sm text-warn">
          {listed.error.message}
        </p>
      ) : (
        <div className="space-y-3">
          {shown.map((provider) => (
            <Provider key={provider.id} provider={provider} onChanged={() => void api.paymentProviders().then(setRows)} />
          ))}
        </div>
      )}
      <Instructions />
    </Section>
  )
}

/** The backend's bound (`settings_store.py` `storefront_payment_instructions`). */
const INSTRUCTIONS_MAX = 2000

/**
 * #5281 (storefront spec §7) — how a customer pays by hand, such as a bank transfer
 * (`storefront_payment_instructions`). Plain text: it is shown as text, never as markup.
 * Empty saves none.
 */
function Instructions() {
  const settings = useAsync(() => api.getSettings(), [])
  const [text, setText] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  useEffect(() => {
    if (settings.data && text === null) setText(settings.data.storefront_payment_instructions ?? '')
  }, [settings.data, text])

  return (
    <form
      className="mt-4 space-y-2 rounded-md border p-3"
      onSubmit={(event) => {
        event.preventDefault()
        setBusy(true)
        setError(null)
        api
          .putSettings({ storefront_payment_instructions: text?.trim() ? text : null })
          .then((next) => {
            setText(next.storefront_payment_instructions ?? '')
            setSaved(true)
          })
          .catch((err: unknown) => setError(refusalText(err)))
          .finally(() => setBusy(false))
      }}
    >
      <Label htmlFor="payment-instructions">Payment instructions</Label>
      <p className="text-xs text-muted-foreground">How a customer pays without a provider, such as your bank details. Plain text, shown to the customer as written.</p>
      <Textarea
        id="payment-instructions"
        rows={3}
        maxLength={INSTRUCTIONS_MAX}
        disabled={text === null}
        value={text ?? ''}
        onChange={(e) => {
          setSaved(false)
          setText(e.target.value)
        }}
      />
      <div className="flex items-center gap-2">
        <Button type="submit" size="sm" disabled={busy || text === null} {...USER_ONLY}>
          Save instructions
        </Button>
        {saved && <span className="text-xs text-muted-foreground">Saved.</span>}
      </div>
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
    </form>
  )
}

function Provider({ provider, onChanged }: { provider: ProviderStatus; onChanged: () => void }) {
  const [editing, setEditing] = useState(false)
  const [values, setValues] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const form = !provider.connected || editing

  async function run(action: () => Promise<unknown>) {
    setBusy(true)
    setError(null)
    try {
      await action()
      onChanged()
      return true
    } catch (err) {
      setError(failure(err))
      return false
    } finally {
      setBusy(false)
    }
  }

  async function save() {
    const settings: Record<string, string> = {}
    const secrets: Record<string, string> = {}
    for (const field of provider.fields) (field.secret ? secrets : settings)[field.name] = values[field.name] ?? field.default ?? ''
    const saved = await run(() => api.connectPaymentProvider(provider.id, { settings, secrets }))
    // The secret leaves the page as soon as it is sent.
    if (saved) {
      setValues({})
      setEditing(false)
    }
  }

  return (
    <section aria-label={provider.label} className="space-y-2 rounded-md border p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 className="flex items-center gap-2 text-sm font-medium">
          {provider.label}
          {provider.is_default && <Badge variant="secondary">Default</Badge>}
        </h4>
        {provider.connected && (
          <div className="flex gap-2">
            {!provider.is_default && (
              <Button size="sm" variant="outline" disabled={busy} onClick={() => void run(() => api.setDefaultPaymentProvider(provider.id))} {...USER_ONLY}>
                Make default
              </Button>
            )}
            <Button size="sm" variant="outline" disabled={busy} onClick={() => setEditing(!editing)} {...USER_ONLY}>
              {editing ? 'Keep credentials' : 'Replace credentials'}
            </Button>
            <Button size="sm" variant="outline" disabled={busy} onClick={() => void run(() => api.disconnectPaymentProvider(provider.id))} {...USER_ONLY}>
              Disconnect
            </Button>
          </div>
        )}
      </div>
      {provider.connected && (
        <p className="text-[13px]">
          <span>Connected as {provider.account_label}</span>
          <span className="text-muted-foreground">
            {' · '}
            {provider.last_webhook_at ? (
              <span>
                Last webhook <When at={provider.last_webhook_at} />
              </span>
            ) : (
              <span>No webhook received yet</span>
            )}
          </span>
        </p>
      )}
      {provider.connected && <WebhookError error={provider.last_webhook_error} at={provider.last_webhook_error_at} />}
      {provider.connected && <CurrencyWarning currency={provider.currency ?? null} reported={provider.reports_currency ?? false} />}
      {form && (
        <form
          className="flex flex-wrap items-end gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            void save()
          }}
        >
          {provider.fields.map((field) => (
            <div key={field.name} className="flex flex-col gap-1">
              <Label htmlFor={`${provider.id}-${field.name}`}>{field.label}</Label>
              {/* #4960 — a field with choices takes one of them, its default preselected; a
                  choice the server refuses for now is shown disabled, with why. */}
              {field.choices ? (
                <Select value={values[field.name] ?? field.default ?? ''} onValueChange={(v) => setValues({ ...values, [field.name]: v })}>
                  <SelectTrigger id={`${provider.id}-${field.name}`} aria-label={field.label} className="w-56">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent position="popper">
                    {field.choices.map((choice) => (
                      <SelectItem key={choice} value={choice} disabled={field.unavailable?.[choice] !== undefined}>
                        {field.unavailable?.[choice] !== undefined ? `${choice} (unavailable)` : choice}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              ) : (
              <Input
                id={`${provider.id}-${field.name}`}
                type={field.secret ? 'password' : 'text'}
                autoComplete="off"
                className="w-56"
                value={values[field.name] ?? ''}
                onChange={(e) => setValues({ ...values, [field.name]: e.target.value })}
              />
              )}
              {Object.entries(field.unavailable ?? {}).map(([choice, why]) => (
                <p key={choice} className="max-w-56 text-xs text-muted-foreground">
                  {choice}: {why}
                </p>
              ))}
            </div>
          ))}
          <Button type="submit" size="sm" disabled={busy} {...USER_ONLY}>
            Save
          </Button>
        </form>
      )}
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
    </section>
  )
}

/**
 * #5666 — an account that takes one currency (a Square location's) cannot take a link in
 * another: when `orders_currency` moved away from it after the connect, say so here. Such
 * an account connected before its currency was kept has none: ask for a reconnect.
 */
function CurrencyWarning({ currency, reported }: { currency: string | null; reported: boolean }) {
  const orders = useSettingsCache().settings?.orders_currency
  if (reported && !currency) {
    return (
      <p role="status" className="text-[13px] text-warn">
        Currency not checked; reconnect to check it.
      </p>
    )
  }
  if (!currency || !orders || currency === orders) return null
  return (
    <p role="status" className="text-[13px] text-warn">
      Connected in {currency}; orders are in {orders}. Payment links for orders in {orders} will fail; orders in {currency} still work.
    </p>
  )
}
