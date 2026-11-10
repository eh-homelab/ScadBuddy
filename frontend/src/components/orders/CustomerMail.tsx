import { useEffect, useState } from 'react'
import { api } from '../../api/client'
import type { OrderMail } from '../../api/types'
import { useAsync } from '../../lib/useAsync'
import { When } from './When'

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
  if (mail.attempts > 0 && Date.parse(mail.next_attempt_at) > now) {
    return (
      <>
        Waiting until <When at={mail.next_attempt_at} />
        {mail.last_error && <span className="text-muted-foreground"> ({mail.last_error})</span>}
      </>
    )
  }
  return <>Queued</>
}

/**
 * #5283 (storefront spec §5.7) — the mail the shop's own server sends an order's customer:
 * each one's event, address and state (queued, sent, failed with the error, or waiting for its
 * next try), newest first. Never a body, a link or a token: the page cannot read them. A note
 * says when the shop's mail server is not set up (nothing is sent), and shows the server's last
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
  const unset = settings.data !== undefined && !settings.data.storefront_mail_host

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
    </section>
  )
}
