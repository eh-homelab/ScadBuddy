import { useEffect, useRef, useState } from 'react'
import { USER_ONLY } from '../../agent/dom'
import { ApiError, api, newRequestId } from '../../api/client'
import { type MessagePreview, previewOrderMessage, sendOrderMessage } from '../../api/orderMessages'
import type { OrderMessage } from '../../api/types'
import { useAsync } from '../../lib/useAsync'
import { Button } from '@/components/ui/button'
import { refusalText } from './refusal'
import { When } from './When'

const TEMPLATE: Record<OrderMessage['template'], string> = {
  quoted: 'Quote ready',
  accepted: 'Order accepted',
  ready: 'Order ready',
  shipped: 'Order shipped',
}

const STATUS: Record<OrderMessage['status'], string> = {
  sending: 'Sending',
  sent: 'Sent',
  failed: 'Not sent',
  unknown: 'Unknown: check the inbox',
  skipped: 'Skipped',
}

const REASON: Record<string, string> = {
  plugin_error: 'the plugin refused it',
  transport: 'the plugin could not be reached',
  no_answer: 'no answer came',
  unavailable: 'the plugin was not available',
}

/** The send's slug when the agent refused it (`message_changed`), else null. */
function slugOf(error: unknown): string | null {
  return error instanceof ApiError ? (error.problem.type ?? null) : null
}

/**
 * #5660 (spec 2026-11-08-order-notify-customer-design.md §7) — the messages sent, or tried, to
 * the order's customer through the bound messaging plugin. An `unknown` one may have reached the
 * customer and is never sent again by itself: a person checks the inbox, then "Mark sent", or
 * "Send again". "Send again" first shows the message composed now; "Send" sends that message
 * only, by its digest (the agent composes it again and refuses one that changed).
 *
 * The open preview keeps one Idempotency-Key for every click on its "Send", and drops it only when
 * a send is answered (the preview closes). A click after a lost answer (a network error after
 * the plugin already sent) is then served from the first claim, never sent twice. An error status
 * keeps it too, since a proxy's 502 can hide a send that went out: only a new "Send again" makes
 * a new key.
 * The order page keys this by the order. Without the key it still clears the preview, the note
 * and the error when `orderId` changes, and drops a preview that comes back for another order.
 */
export function OrderMessages({ orderId }: { orderId: string }) {
  const listed = useAsync(() => api.orderMessages(orderId), [orderId], [`order:${orderId}`])
  const [preview, setPreview] = useState<{ of: OrderMessage; shown: MessagePreview; key: string } | null>(null)
  // The order this view shows now; null once it is gone. A late preview for another is dropped.
  const showing = useRef<string | null>(orderId)
  useEffect(() => {
    showing.current = orderId
    return () => {
      showing.current = null
    }
  }, [orderId])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [note, setNote] = useState<string | null>(null)
  const [stateFor, setStateFor] = useState(orderId)
  if (stateFor !== orderId) {
    setStateFor(orderId)
    setPreview(null)
    setError(null)
    setNote(null)
  }
  const messages = listed.data ?? []
  if (messages.length === 0 && !error) return null

  /**
   * Runs one click's action. `still()` says whether this view still shows the order the click
   * was for; an answer that comes back after the order changed sets nothing.
   */
  async function act(action: (still: () => boolean) => Promise<void>) {
    const asked = orderId
    const still = () => showing.current === asked
    setBusy(true)
    setError(null)
    setNote(null)
    try {
      await action(still)
    } catch (err) {
      if (!still()) return
      if (slugOf(err) === 'message_changed') {
        setPreview(null)
        setError('The message changed since you saw it (the order, the customer or the binding), so nothing was sent. Choose Send again to see it as it is now.')
      } else {
        setError(refusalText(err))
      }
    } finally {
      listed.refresh()
      setBusy(false)
    }
  }

  return (
    <div className="space-y-1">
      <h3 className="text-[13px] font-medium">Customer messages</h3>
      <ul className="flex flex-col gap-1 text-[13px]" aria-label="Customer messages">
        {messages.map((m) => (
          <li key={m.id} className="flex flex-wrap items-center justify-between gap-2 rounded-md border px-2 py-1">
            <span>
              <span className="font-medium">{TEMPLATE[m.template]}</span>
              <span className="text-muted-foreground">
                {' · '}
                {m.recipients.map((r) => r.value).join(', ')} · <When at={m.created_at} />
                {m.resend_of ? ' · sent again' : ''}
              </span>
              {' · '}
              <span className={m.status === 'sent' ? '' : 'text-warn'}>
                {STATUS[m.status]}
                {m.reason && m.status !== 'sent' ? ` (${REASON[m.reason] ?? m.reason})` : ''}
              </span>
            </span>
            {m.status !== 'sending' && (
              <span className="flex gap-2">
                {m.status === 'unknown' && (
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={busy}
                    onClick={() =>
                      void act(async (still) => {
                        await api.markOrderMessageSent(orderId, m.id)
                        if (still()) setNote('Marked as sent.')
                      })
                    }
                    {...USER_ONLY}
                  >
                    Mark sent
                  </Button>
                )}
                <Button
                  size="sm"
                  variant="outline"
                  disabled={busy}
                  onClick={() =>
                    void act(async (still) => {
                      const shown = await previewOrderMessage(orderId, m.template, m.id)
                      if (still()) setPreview({ of: m, shown, key: newRequestId() })
                    })
                  }
                  {...USER_ONLY}
                >
                  Send again
                </Button>
              </span>
            )}
          </li>
        ))}
      </ul>
      {preview && (
        <section aria-label="Message to send" className="space-y-1 rounded-md border p-2 text-[13px]">
          <p>
            To {preview.shown.message.to.map((r) => r.value).join(', ')} through {preview.shown.message.source}
            {preview.shown.message.subject !== null && <>: “{preview.shown.message.subject}”</>}
          </p>
          <p className="whitespace-pre-wrap break-words rounded-sm bg-muted/40 p-2">{preview.shown.message.text}</p>
          <span className="flex gap-2">
            <Button
              size="sm"
              disabled={busy}
              onClick={() =>
                void act(async (still) => {
                  const { of, shown, key } = preview
                  const answer = await sendOrderMessage(orderId, of.template, of.id, shown.digest, key)
                  if (!still()) return
                  setPreview(null)
                  setNote(answer.warning ?? (answer.sent ? 'Sent.' : `Not sent: ${STATUS[answer.message.status]}.`))
                })
              }
              {...USER_ONLY}
            >
              Send
            </Button>
            <Button size="sm" variant="ghost" disabled={busy} onClick={() => setPreview(null)}>
              Cancel
            </Button>
          </span>
        </section>
      )}
      {note && (
        <p role="status" className="text-[13px] text-muted-foreground">
          {note}
        </p>
      )}
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
    </div>
  )
}
