import { useEffect, useRef, useState } from 'react'
import { api } from '../../api/client'
import type { OrderMail } from '../../api/types'
import { useAsync } from '../../lib/useAsync'
import { When } from './When'

/** How often the list is read again while a mail waits to be sent. */
const WAITING_REFRESH_MS = 15_000

/** How often the settings are read again while mail is queued and the server is not set up. */
const UNSET_REFRESH_MS = 60_000

/** The most rows the API returns (backend `ORDER_MAIL_MAX`): the newest. A test checks it
 * against the route's `maxItems` in the exported spec. */
const MAIL_MAX = 200

const EVENT: Record<OrderMail['event'], string> = {
  sign_in_code: 'Sign-in code',
  order_link: 'Order link',
  revision_ready: 'Revision ready',
  price_ready: 'Price ready',
  payment_requested: 'Payment requested',
  shipped: 'Order shipped',
}

function State({ mail, now }: { mail: OrderMail; now: number }) {
  if (mail.sent_at) {
    return (
      <>
        Sent <When at={mail.sent_at} />
      </>
    )
  }
  if (mail.failed_at) return <span className="text-warn">Failed: {mail.last_error ?? 'no reason was given'}</span>
  // Waiting only while its next try is still ahead; after that the sender owes it, so it reads queued.
  // Either way the refusal stays visible.
  // A row held back by the process-wide server backoff has no attempts of its own, so it reads
  // "Queued", not "Waiting". The server-error alert above the list explains it.
  const refusal = mail.last_error && <span className="text-muted-foreground"> ({mail.last_error})</span>
  if (mail.attempts > 0 && Date.parse(mail.next_attempt_at) > now) {
    return (
      <>
        Waiting until <When at={mail.next_attempt_at} />
        {refusal}
      </>
    )
  }
  return (
    <>
      Queued
      {refusal}
    </>
  )
}

/**
 * #5283 (storefront spec §5.7) — the mail the shop's own server sends an order's customer:
 * each one's event, address and state (queued, sent, failed with the error, or waiting for its
 * next try), newest first. Never a body, a link or a token: the page cannot read them. A note
 * says when the shop's mail server is not set up, a host or a sender address missing (nothing is sent), and shows the server's last
 * error when it has one. Everything shown is text.
 */
export function CustomerMail({ orderId, version = 0 }: { orderId: string; version?: number }) {
  // `version` moves when the page changes the link: its send queues a mail.
  const mail = useAsync(() => api.orderMail(orderId), [orderId, version], [`order:${orderId}`])
  const settings = useAsync(() => api.getSettings(), [])
  const server = useAsync(() => api.storefrontMail(), [])
  // The clock a backed-off mail is judged by, read again whenever the list is.
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => setNow(Date.now()), [mail.data])
  // The sender needs a host and a sender address (`MailConfig.ready`); without both it sends nothing.
  const unset = settings.data !== undefined && (!settings.data.storefront_mail_host || !settings.data.storefront_mail_sender)
  // A row the sender stamps publishes nothing, so read again while a row waits.
  // With no mail server set up, nothing is sent, so there is nothing to wait for.
  const waiting = !unset && (mail.data?.some((m) => m.sent_at == null && m.failed_at == null) ?? false)
  // Queued rows with no server set up: settings changed in another tab clear the note, so
  // read the settings slowly. When they show a server, `waiting` turns on and the poll resumes.
  const queuedUnset = unset && (mail.data?.some((m) => m.sent_at == null && m.failed_at == null) ?? false)
  const refreshMail = mail.refresh
  const refreshSettings = settings.refresh
  const refreshServer = server.refresh
  useEffect(() => {
    if (!waiting) return
    const timer = setInterval(() => {
      refreshMail()
      refreshSettings()
      refreshServer()
    }, WAITING_REFRESH_MS)
    return () => clearInterval(timer)
  }, [waiting, refreshMail, refreshSettings, refreshServer])
  useEffect(() => {
    if (!queuedUnset) return
    const timer = setInterval(refreshSettings, UNSET_REFRESH_MS)
    return () => clearInterval(timer)
  }, [queuedUnset, refreshSettings])
  // The settings and the server's error are read again when the page changes the link.
  const firstVersion = useRef(version)
  useEffect(() => {
    if (version === firstVersion.current) return
    refreshSettings()
    refreshServer()
  }, [version, refreshSettings, refreshServer])

  return (
    <section className="flex flex-col gap-2" aria-label="Customer mail">
      <h2 className="text-sm font-medium">Customer mail</h2>
      {unset && (
        <p className="text-[13px] text-muted-foreground">
          The shop's mail server is not set up, so no mail is sent. Set it in Settings, under Storefront mail.
        </p>
      )}
      {server.data?.server_error && (
        <p role="alert" className="text-[13px] text-warn">
          The mail server's last error: <span className="break-all">{server.data.server_error}</span>
          {server.data.server_error_at && (
            <>
              {' · '}
              <When at={server.data.server_error_at} />
            </>
          )}
        </p>
      )}
      {!mail.data && mail.error && (
        <p role="alert" className="text-[13px] text-destructive">
          The order's mail could not be read: {mail.error.message}
        </p>
      )}
      {mail.data && mail.data.length === 0 && <p className="text-[13px] text-muted-foreground">No mail yet.</p>}
      {mail.data && mail.data.length > 0 && (
        <ul className="flex flex-col gap-1 text-[13px]">
          {mail.data.map((m, i) => (
            // The API gives no row id by design; the index keeps two like rows apart.
            <li key={`${i}-${m.created_at}-${m.event}`} className="flex flex-wrap gap-x-2">
              <span className="font-medium">{EVENT[m.event]}</span>
              <span className="break-all">{m.to}</span>
              <span>
                <State mail={m} now={now} />
              </span>
            </li>
          ))}
        </ul>
      )}
      {mail.data && mail.data.length >= MAIL_MAX && (
        <p className="text-[13px] text-muted-foreground">The list shows at most the newest {MAIL_MAX}.</p>
      )}
    </section>
  )
}
