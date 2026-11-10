/**
 * #5660 (spec 2026-11-08-order-notify-customer-design.md §4.3, §7.2) — the agent's routes for
 * customer messages, served under `/api/v1/ai` by the agent sidecar, not the backend, so the
 * shapes are written out here from `agent/src/routes/messagingBinding.ts` and
 * `agent/src/routes/orderMessages.ts`. Each needs the UI's origin; the test send, the preview
 * and the send need a person signed in. A refusal is an `ApiError` with the agent's `detail`,
 * and the send's slug (`message_changed`, `already_notified`) as its `type`.
 */
import { aiRequest } from './aiPlugins'
import type { MessagingBinding, OrderMessage } from './types'

export type NotifyTemplate = OrderMessage['template']

/** Whether the binding's plugin is there and its tool takes the binding; nothing is saved. */
export type BindingCheck = { ok: true } | { ok: false; detail: string }

/** One real test message's outcome; the agent's audit log keeps it. */
export type TestSend = { ok: boolean; status: 'sent' | 'failed' | 'unknown'; reason?: string }

/** What a send would send now. The send names `digest`; the browser never sends the text. */
export type MessagePreview = {
  message: { source: string; tool: string; to: { kind: string; value: string }[]; subject: string | null; text: string }
  digest: string
}

/** A send's answer: the recorded message, and whether the plugin reached the customer. */
export type MessageSent = { sent: boolean; message: OrderMessage; warning?: string }

const post = (body: unknown, headers: Record<string, string> = {}): RequestInit => ({
  method: 'POST',
  body: JSON.stringify(body),
  headers,
})

/** A 422 is the check's own "no": the plugin, the tool or an argument does not fit. */
export async function checkMessagingBinding(binding: MessagingBinding): Promise<BindingCheck> {
  try {
    return await aiRequest<BindingCheck>('/orders/messaging-binding/check', post({ binding }))
  } catch (err) {
    const status = (err as { status?: number }).status
    if (status === 422) return { ok: false, detail: (err as Error).message }
    throw err
  }
}

export function testMessagingBinding(binding: MessagingBinding, to: string): Promise<TestSend> {
  return aiRequest<TestSend>('/orders/messaging-binding/test', post({ binding, to }))
}

export function previewOrderMessage(orderId: string, template: NotifyTemplate, resendOf: string): Promise<MessagePreview> {
  return aiRequest<MessagePreview>(`/orders/${encodeURIComponent(orderId)}/messages/preview`, post({ template, resend_of: resendOf }))
}

/** `key` is one per open preview, so a retry after a lost answer is answered from its claim, never sent twice. */
export function sendOrderMessage(orderId: string, template: NotifyTemplate, resendOf: string, digest: string, key: string): Promise<MessageSent> {
  return aiRequest<MessageSent>(
    `/orders/${encodeURIComponent(orderId)}/messages/send`,
    post({ template, resend_of: resendOf, digest }, { 'Idempotency-Key': key }),
  )
}
