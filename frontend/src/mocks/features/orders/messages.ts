import { HttpResponse, http } from 'msw'
import type { OrderMessage } from '../../../api/types'
import { newId, now } from './state'

/**
 * #5660 — an order's customer messages (backend `api/order_messages.py`) and the agent's routes
 * around them (`agent/src/routes/messagingBinding.ts`, `orderMessages.ts`). A preview's message
 * is fixed text; `mockMessageChange()` makes the next send find a different message, as an edit
 * to the order or the customer would. A binding whose tool is `missing_tool` fails the check.
 * A send is claimed under its `Idempotency-Key`, as the agent's is: a retry with the same key is
 * answered from that claim. `mockLoseNextSendAnswer()` claims and sends the next one, then drops
 * its answer, as a network error after the plugin already sent would.
 */

let messages: OrderMessage[] = []
let version = 0
const tests: { to: string }[] = []
const claims = new Map<string, OrderMessage>()
let loseNextAnswer = false

export function reset(): void {
  messages = []
  version = 0
  tests.length = 0
  claims.clear()
  loseNextAnswer = false
}

/** A message recorded on `orderId`, for a test's setup. */
export function mockOrderMessage(orderId: string, message: Partial<OrderMessage> = {}): OrderMessage {
  const made: OrderMessage = {
    id: newId('msg'),
    order_id: orderId,
    template: 'ready',
    source: 'remote:mail',
    tool: 'send_email',
    recipients: [{ kind: 'email', value: 'ada@example.com' }],
    subject: 'Order ORD-2026-1 is ready',
    text: 'Hello Ada,\n\nOrder ORD-2026-1 is ready.',
    status: 'sent',
    reason: null,
    provider_message_id: null,
    episode: 1,
    resend_of: null,
    by: { kind: 'agent', principal: 'browser', session: null, run_id: null },
    created_at: now(),
    finished_at: now(),
    ...message,
  }
  messages.push(made)
  return made
}

/** The next send finds another message than the one previewed. */
export function mockMessageChange(): void {
  version += 1
}

/** The next send is claimed and sent, but its answer is lost on the way back. */
export function mockLoseNextSendAnswer(): void {
  loseNextAnswer = true
}

/** The Idempotency-Keys the sends were claimed under, for a test to read. */
export function mockSendKeys(): string[] {
  return [...claims.keys()]
}

/** The test messages sent, for a test to read. */
export function mockTestSends(): readonly { to: string }[] {
  return tests
}

const digestOf = (v: number) => v.toString(16).padStart(64, '0')

function composed(orderId: string, template: string) {
  return {
    source: 'remote:mail',
    tool: 'send_email',
    to: [{ kind: 'email', value: 'ada@example.com' }],
    subject: `Order ${orderId} is ${template}`,
    text: `Hello Ada,\n\nOrder ${orderId} is ${template}.`,
  }
}

export const handlers = [
  http.get('/api/v1/orders/:id/messages', ({ params }) =>
    HttpResponse.json(messages.filter((m) => m.order_id === params.id).toReversed()),
  ),
  http.post('/api/v1/orders/:id/messages/:mid/mark-sent', ({ params }) => {
    const found = messages.find((m) => m.order_id === params.id && m.id === params.mid)
    if (!found) return HttpResponse.json({ title: 'Not Found', status: 404, detail: 'no such message' }, { status: 404 })
    if (found.status !== 'unknown') {
      return HttpResponse.json(
        { type: 'https://scadbuddy.dev/problems/orders/message_not_unknown', title: 'Conflict', status: 409, detail: `message ${found.id} is ${found.status}` },
        { status: 409 },
      )
    }
    found.status = 'sent'
    found.finished_at = now()
    return HttpResponse.json(found)
  }),
  http.post('/api/v1/ai/orders/messaging-binding/check', async ({ request }) => {
    const { binding } = (await request.json()) as { binding: { source: string; tool: string } }
    if (binding.tool === 'missing_tool') return HttpResponse.json({ ok: false, detail: `${binding.source} lists no tool missing_tool` }, { status: 422 })
    return HttpResponse.json({ ok: true })
  }),
  http.post('/api/v1/ai/orders/messaging-binding/test', async ({ request }) => {
    const { to } = (await request.json()) as { to: string }
    tests.push({ to })
    return HttpResponse.json({ ok: true, status: 'sent' })
  }),
  http.post('/api/v1/ai/orders/:id/messages/preview', async ({ request, params }) => {
    const { template } = (await request.json()) as { template: string }
    return HttpResponse.json({ message: composed(String(params.id), template), digest: digestOf(version) })
  }),
  http.post('/api/v1/ai/orders/:id/messages/send', async ({ request, params }) => {
    const { template, resend_of, digest } = (await request.json()) as { template: OrderMessage['template']; resend_of: string; digest: string }
    if (digest !== digestOf(version)) {
      return HttpResponse.json(
        { detail: 'message_changed: the order, the customer or the messaging binding changed since the preview, so nothing was sent; preview it again', type: 'message_changed' },
        { status: 409 },
      )
    }
    const key = request.headers.get('idempotency-key') ?? ''
    const claimed = claims.get(key)
    if (claimed) return HttpResponse.json({ sent: claimed.status === 'sent', message: claimed })
    const message = composed(String(params.id), template)
    const sent = mockOrderMessage(String(params.id), {
      template,
      recipients: message.to,
      subject: message.subject,
      text: message.text,
      resend_of,
      episode: 1,
    })
    claims.set(key, sent)
    if (loseNextAnswer) {
      loseNextAnswer = false
      return HttpResponse.error()
    }
    return HttpResponse.json({ sent: true, message: sent })
  }),
]
