import { useEffect, useState } from 'react'
import { USER_ONLY } from '../../agent/dom'
import { ApiError, api } from '../../api/client'
import { checkMessagingBinding, testMessagingBinding } from '../../api/orderMessages'
import type { MessagingBinding } from '../../api/types'
import { useAsync } from '../../lib/useAsync'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { refusalText } from '../orders/refusal'

/** The test route's 401: no person is signed in (auth mode off, or signed out). */
export const SIGNED_IN_ONLY = 'A test message needs a person signed in to ScadBuddy, and no one is signed in (sign-in may be off), so nothing was sent.'

type Fixed = { key: string; value: string }
type Form = {
  source: string
  tool: string
  contactKind: string
  to: string
  joined: boolean
  subject: string
  text: string
  key: string
  fixed: Fixed[]
}

const EMPTY: Form = { source: '', tool: '', contactKind: 'email', to: '', joined: false, subject: '', text: '', key: '', fixed: [] }

function formOf(b: MessagingBinding | null | undefined): Form {
  if (!b) return EMPTY
  return {
    source: b.source,
    tool: b.tool,
    contactKind: b.contact_kind,
    to: b.args.to,
    joined: b.args.to_shape === 'string',
    subject: b.args.subject ?? '',
    text: b.args.text,
    key: b.args.idempotency_key ?? '',
    fixed: Object.entries(b.fixed_args ?? {}).map(([key, value]) => ({ key, value: String(value) })),
  }
}

/** The binding the form describes, or what to fix first. The backend's checks run on save. */
function bindingOf(f: Form): MessagingBinding | string {
  if (!f.source.trim()) return 'Enter the plugin, as remote:<name> or package:<package>/<server>.'
  if (!f.tool.trim()) return 'Enter the name of the tool that sends a message.'
  if (!f.contactKind.trim()) return 'Enter the kind of contact it reaches, such as email.'
  if (!f.to.trim() || !f.text.trim()) return 'Enter the tool’s argument names for the recipients and the text.'
  const fixed: Record<string, string> = {}
  for (const [i, row] of f.fixed.entries()) {
    if (!row.key.trim()) return `Fixed argument ${i + 1}: enter its name.`
    fixed[row.key.trim()] = row.value
  }
  return {
    source: f.source.trim(),
    tool: f.tool.trim(),
    contact_kind: f.contactKind.trim(),
    args: {
      to: f.to.trim(),
      to_shape: f.joined ? 'string' : 'list',
      subject: f.subject.trim() || null,
      text: f.text.trim(),
      idempotency_key: f.key.trim() || null,
    },
    ...(f.fixed.length > 0 ? { fixed_args: fixed } : {}),
  }
}

const FIELDS: { id: keyof Form; label: string; hint: string }[] = [
  { id: 'source', label: 'Plugin', hint: 'remote:<name>, or package:<package>/<server>' },
  { id: 'tool', label: 'Tool', hint: 'the tool that sends one message, such as send_email' },
  { id: 'contactKind', label: 'Contact kind', hint: 'the customer contacts it reaches, such as email' },
  { id: 'to', label: 'Recipients argument', hint: 'the tool’s argument for the addresses' },
  { id: 'subject', label: 'Subject argument', hint: 'optional' },
  { id: 'text', label: 'Text argument', hint: 'the tool’s argument for the message body' },
  { id: 'key', label: 'Dedupe key argument', hint: 'optional: an argument the plugin uses to drop a repeated send' },
]

/**
 * #5660 (spec 2026-11-08-order-notify-customer-design.md §4) — Settings → Orders' customer
 * messages: the one messaging plugin tool the assistant sends an order's messages through
 * (`orders_customer_messaging`). Save checks the binding against the plugin's tool first
 * (the agent's check route) and saves only one that fits; "Send a test" sends one real message
 * to an address typed here (a person only; the agent's audit log keeps it). No secret goes
 * here: the plugin's header or OAuth sign-in holds it.
 */
export function CustomerMessaging() {
  const settings = useAsync(() => api.getSettings(), [])
  const [form, setForm] = useState<Form | null>(null)
  const [testTo, setTestTo] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [note, setNote] = useState<string | null>(null)
  const loaded = settings.data !== undefined
  const bound = settings.data?.orders_customer_messaging ?? null
  useEffect(() => {
    if (loaded && form === null) setForm(formOf(settings.data?.orders_customer_messaging))
  }, [loaded, form, settings.data])
  const f = form ?? EMPTY
  const edit = (patch: Partial<Form>) => {
    setNote(null)
    setForm({ ...f, ...patch })
  }

  async function act(action: (binding: MessagingBinding) => Promise<string | null>) {
    const binding = bindingOf(f)
    if (typeof binding === 'string') {
      setError(binding)
      return
    }
    setBusy(true)
    setError(null)
    setNote(null)
    try {
      const checked = await checkMessagingBinding(binding)
      if (!checked.ok) {
        setError(`The plugin cannot take this binding: ${checked.detail}`)
        return
      }
      setNote(await action(binding))
    } catch (err) {
      setError(refusalText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section aria-label="Customer messages" className="space-y-2 rounded-md border p-3">
      <h4 className="text-sm font-medium">Customer messages</h4>
      <p className="text-xs text-muted-foreground">
        The messaging plugin tool that sends an order&rsquo;s messages to its customer. {bound ? `Bound to ${bound.source} ${bound.tool}.` : 'None is bound: the assistant composes the text for you to send by hand.'}{' '}
        Put no secret here; the plugin&rsquo;s own sign-in holds it.
      </p>
      <form
        className="flex flex-col gap-2"
        onSubmit={(event) => {
          event.preventDefault()
          void act(async (binding) => {
            const next = await api.putSettings({ orders_customer_messaging: binding })
            setForm(formOf(next.orders_customer_messaging))
            settings.refresh()
            return 'Saved.'
          })
        }}
      >
        <div className="flex flex-wrap gap-2">
          {FIELDS.map(({ id, label, hint }) => (
            <div key={id} className="flex flex-col gap-1">
              <Label htmlFor={`messaging-${id}`}>{label}</Label>
              <Input id={`messaging-${id}`} className="w-56" placeholder={hint} value={f[id] as string} onChange={(e) => edit({ [id]: e.target.value })} />
            </div>
          ))}
        </div>
        <div className="flex items-center gap-2">
          <Checkbox id="messaging-joined" checked={f.joined} onCheckedChange={(checked) => edit({ joined: checked === true })} />
          <Label htmlFor="messaging-joined">The tool takes the recipients as one comma-separated text</Label>
        </div>
        {f.fixed.map((row, i) => (
          <div key={i} className="flex flex-wrap items-end gap-2">
            <div className="flex flex-col gap-1">
              <Label htmlFor={`messaging-fixed-${i}-key`}>{`Fixed argument ${i + 1}`}</Label>
              <Input id={`messaging-fixed-${i}-key`} className="w-40" value={row.key} onChange={(e) => edit({ fixed: f.fixed.map((r, j) => (j === i ? { ...r, key: e.target.value } : r)) })} />
            </div>
            <div className="flex flex-col gap-1">
              <Label htmlFor={`messaging-fixed-${i}-value`}>{`Fixed argument ${i + 1} value`}</Label>
              <Input id={`messaging-fixed-${i}-value`} className="w-56" value={row.value} onChange={(e) => edit({ fixed: f.fixed.map((r, j) => (j === i ? { ...r, value: e.target.value } : r)) })} />
            </div>
            <Button type="button" size="xs" variant="ghost" aria-label={`Remove fixed argument ${i + 1}`} onClick={() => edit({ fixed: f.fixed.filter((_, j) => j !== i) })}>
              Remove
            </Button>
          </div>
        ))}
        <div className="flex flex-wrap items-center gap-2">
          <Button type="button" size="sm" variant="outline" disabled={!loaded} onClick={() => edit({ fixed: [...f.fixed, { key: '', value: '' }] })}>
            Add fixed argument
          </Button>
          <Button type="submit" size="sm" disabled={busy || !loaded}>
            Check and save
          </Button>
          {bound && (
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={() => {
                setBusy(true)
                setError(null)
                api
                  .putSettings({ orders_customer_messaging: null })
                  .then(() => {
                    setForm(EMPTY)
                    settings.refresh()
                    setNote('Unbound.')
                  })
                  .catch((err: unknown) => setError(refusalText(err)))
                  .finally(() => setBusy(false))
              }}
            >
              Unbind
            </Button>
          )}
        </div>
      </form>
      <div className="flex flex-wrap items-end gap-2">
        <div className="flex flex-col gap-1">
          <Label htmlFor="messaging-test-to">Test address</Label>
          <Input id="messaging-test-to" className="w-56" value={testTo} onChange={(e) => setTestTo(e.target.value)} />
        </div>
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={busy || !testTo.trim()}
          onClick={() =>
            void act(async (binding) => {
              const sent = await testMessagingBinding(binding, testTo.trim()).catch((err: unknown) => {
                if (err instanceof ApiError && err.status === 401) throw new Error(SIGNED_IN_ONLY)
                throw err
              })
              return sent.ok ? `Test message sent to ${testTo.trim()}.` : `The test message was not sent: ${sent.status}${sent.reason ? ` (${sent.reason})` : ''}.`
            })
          }
          {...USER_ONLY}
        >
          Send a test
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">A test sends one real message, so it needs a person signed in to ScadBuddy. With sign-in off, it is refused.</p>
      {note && (
        <p role="status" className="text-xs text-muted-foreground">
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
