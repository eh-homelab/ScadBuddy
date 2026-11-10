import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { beforeEach, describe, expect, it } from 'vitest'
import { api } from '../../api/client'
import { resetMockState } from '../../mocks/handlers'
import { server } from '../../mocks/server'
import {
  mockLoseNextSendAnswer,
  mockMessageChange,
  mockOrderMessage,
  mockSendKeys,
  mockStorefrontMails,
} from '../../mocks/features/orders/messages'
import { ORDER_ACCEPTED, ORDER_DRAFT } from '../../mocks/features/orders/state'
import { renderPage } from '../../test/utils'
import { OrderMessages } from './OrderMessages'

describe('OrderMessages (#5660)', () => {
  beforeEach(() => resetMockState())

  it('shows nothing when the order has no messages', async () => {
    renderPage(<OrderMessages orderId={ORDER_ACCEPTED} />)
    await waitFor(() => expect(screen.queryByRole('list', { name: 'Customer messages' })).not.toBeInTheDocument())
  })

  it('says why a message was skipped: the storefront mails it itself (#5660 §8)', async () => {
    mockOrderMessage(ORDER_ACCEPTED, { template: 'shipped', status: 'skipped', reason: 'storefront_mail' })
    renderPage(<OrderMessages orderId={ORDER_ACCEPTED} />)
    const list = await screen.findByRole('list', { name: 'Customer messages' })
    expect(within(list).getByText(/Skipped \(the storefront mails it itself\)/)).toBeInTheDocument()
  })

  it('asks before it sends what the storefront already emails, and sends it here too on yes', async () => {
    const first = mockOrderMessage(ORDER_ACCEPTED, { template: 'shipped', status: 'failed', reason: 'transport' })
    mockStorefrontMails()
    const { user } = renderPage(<OrderMessages orderId={ORDER_ACCEPTED} />)
    const list = await screen.findByRole('list', { name: 'Customer messages' })
    await user.click(within(list).getByRole('button', { name: 'Send again' }))
    const asked = await screen.findByRole('region', { name: 'Storefront mail' })
    expect(asked).toHaveTextContent('The storefront already emails this update to the customer. Send it here too?')
    expect(screen.queryByRole('region', { name: 'Message to send' })).not.toBeInTheDocument()
    await user.click(within(asked).getByRole('button', { name: 'Yes, send it here too' }))
    const shown = await screen.findByRole('region', { name: 'Message to send' })
    expect(screen.queryByRole('region', { name: 'Storefront mail' })).not.toBeInTheDocument()
    await user.click(within(shown).getByRole('button', { name: 'Send' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Sent.')
    expect(mockSendKeys()).toHaveLength(1)
    const messages = await api.orderMessages(ORDER_ACCEPTED)
    expect(messages[0]).toMatchObject({ status: 'sent', resend_of: first.id })
  })

  it('serves a retry after a lost answer from its first claim, after the storefront began to mail it', async () => {
    mockOrderMessage(ORDER_ACCEPTED, { template: 'shipped', status: 'failed', reason: 'transport' })
    const { user } = renderPage(<OrderMessages orderId={ORDER_ACCEPTED} />)
    const list = await screen.findByRole('list', { name: 'Customer messages' })
    await user.click(within(list).getByRole('button', { name: 'Send again' }))
    const shown = await screen.findByRole('region', { name: 'Message to send' })
    mockLoseNextSendAnswer()
    await user.click(within(shown).getByRole('button', { name: 'Send' }))
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    mockStorefrontMails()
    await user.click(within(screen.getByRole('region', { name: 'Message to send' })).getByRole('button', { name: 'Send' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Sent.')
    expect(screen.queryByRole('region', { name: 'Storefront mail' })).not.toBeInTheDocument()
    expect(mockSendKeys()).toHaveLength(1)
    expect(await api.orderMessages(ORDER_ACCEPTED)).toHaveLength(2)
  })

  it('previews a skipped message with the flag when "Send here too" is pressed', async () => {
    mockOrderMessage(ORDER_ACCEPTED, { template: 'shipped', status: 'skipped', reason: 'storefront_mail' })
    const bodies: unknown[] = []
    server.events.on('request:start', ({ request }) => {
      if (request.url.endsWith('/messages/preview')) void request.clone().json().then((b) => bodies.push(b))
    })
    const { user } = renderPage(<OrderMessages orderId={ORDER_ACCEPTED} />)
    const list = await screen.findByRole('list', { name: 'Customer messages' })
    await user.click(within(list).getByRole('button', { name: 'Send here too' }))
    await screen.findByRole('region', { name: 'Message to send' })
    server.events.removeAllListeners()
    expect(bodies).toEqual([expect.objectContaining({ also_when_storefront_mails: true })])
  })

  it('sends nothing when the person says no to the storefront question', async () => {
    mockOrderMessage(ORDER_ACCEPTED, { template: 'shipped', status: 'failed', reason: 'transport' })
    mockStorefrontMails()
    const { user } = renderPage(<OrderMessages orderId={ORDER_ACCEPTED} />)
    const list = await screen.findByRole('list', { name: 'Customer messages' })
    await user.click(within(list).getByRole('button', { name: 'Send again' }))
    const asked = await screen.findByRole('region', { name: 'Storefront mail' })
    await user.click(within(asked).getByRole('button', { name: 'No' }))
    expect(screen.queryByRole('region', { name: 'Storefront mail' })).not.toBeInTheDocument()
    expect(mockSendKeys()).toEqual([])
  })

  it('offers "Send here too" on a message skipped for the storefront, and sends it with the person’s yes', async () => {
    const skipped = mockOrderMessage(ORDER_ACCEPTED, { template: 'shipped', status: 'skipped', reason: 'storefront_mail' })
    mockStorefrontMails()
    const { user } = renderPage(<OrderMessages orderId={ORDER_ACCEPTED} />)
    const list = await screen.findByRole('list', { name: 'Customer messages' })
    expect(within(list).queryByRole('button', { name: 'Send again' })).not.toBeInTheDocument()
    await user.click(within(list).getByRole('button', { name: 'Send here too' }))
    const shown = await screen.findByRole('region', { name: 'Message to send' })
    await user.click(within(shown).getByRole('button', { name: 'Send' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Sent.')
    const messages = await api.orderMessages(ORDER_ACCEPTED)
    expect(messages[0]).toMatchObject({ status: 'sent', resend_of: skipped.id })
  })

  it('marks an unknown message sent only on a person’s word', async () => {
    mockOrderMessage(ORDER_ACCEPTED, { status: 'unknown', reason: 'no_answer', finished_at: null })
    const { user } = renderPage(<OrderMessages orderId={ORDER_ACCEPTED} />)
    const list = await screen.findByRole('list', { name: 'Customer messages' })
    expect(within(list).getByText(/Unknown: check the inbox \(no answer came\)/)).toBeInTheDocument()
    await user.click(within(list).getByRole('button', { name: 'Mark sent' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Marked as sent.')
    await waitFor(() => expect(within(list).queryByRole('button', { name: 'Mark sent' })).not.toBeInTheDocument())
    const [message] = await api.orderMessages(ORDER_ACCEPTED)
    expect(message?.status).toBe('sent')
  })

  it('shows the message composed now, then sends it again as the person approved it', async () => {
    const first = mockOrderMessage(ORDER_ACCEPTED, { status: 'failed', reason: 'transport' })
    const { user } = renderPage(<OrderMessages orderId={ORDER_ACCEPTED} />)
    const list = await screen.findByRole('list', { name: 'Customer messages' })
    await user.click(within(list).getByRole('button', { name: 'Send again' }))
    const shown = await screen.findByRole('region', { name: 'Message to send' })
    expect(within(shown).getByText(/To ada@example.com through remote:mail/)).toBeInTheDocument()
    // The line breaks of the text are kept.
    expect(within(shown).getByText(/Hello Ada,/).textContent).toContain('\n\n')
    await user.click(within(shown).getByRole('button', { name: 'Send' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Sent.')
    const messages = await api.orderMessages(ORDER_ACCEPTED)
    expect(messages).toHaveLength(2)
    expect(messages[0]).toMatchObject({ status: 'sent', resend_of: first.id })
  })

  it('sends nothing when the message changed since it was shown', async () => {
    mockOrderMessage(ORDER_ACCEPTED, { status: 'unknown', reason: 'no_answer' })
    const { user } = renderPage(<OrderMessages orderId={ORDER_ACCEPTED} />)
    const list = await screen.findByRole('list', { name: 'Customer messages' })
    await user.click(within(list).getByRole('button', { name: 'Send again' }))
    const shown = await screen.findByRole('region', { name: 'Message to send' })
    mockMessageChange()
    await user.click(within(shown).getByRole('button', { name: 'Send' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('The message changed since you saw it')
    expect(screen.queryByRole('region', { name: 'Message to send' })).not.toBeInTheDocument()
    expect(await api.orderMessages(ORDER_ACCEPTED)).toHaveLength(1)
  })

  it('sends once when the first answer is lost and the person clicks Send again', async () => {
    const first = mockOrderMessage(ORDER_ACCEPTED, { status: 'failed', reason: 'transport' })
    const { user } = renderPage(<OrderMessages orderId={ORDER_ACCEPTED} />)
    const list = await screen.findByRole('list', { name: 'Customer messages' })
    await user.click(within(list).getByRole('button', { name: 'Send again' }))
    const shown = await screen.findByRole('region', { name: 'Message to send' })
    mockLoseNextSendAnswer()
    await user.click(within(shown).getByRole('button', { name: 'Send' }))
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    // The preview stays open with its key; the second click is answered from the first claim.
    await user.click(within(screen.getByRole('region', { name: 'Message to send' })).getByRole('button', { name: 'Send' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Sent.')
    expect(mockSendKeys()).toHaveLength(1)
    const messages = await api.orderMessages(ORDER_ACCEPTED)
    expect(messages).toHaveLength(2)
    expect(messages[0]).toMatchObject({ status: 'sent', resend_of: first.id })
  })

  // N1 of #5907's review: the view belongs to one order, even when it is not keyed by it.
  describe('when the order changes under it', () => {
    beforeEach(() => {
      mockOrderMessage(ORDER_ACCEPTED, { status: 'failed', reason: 'transport' })
      mockOrderMessage(ORDER_DRAFT, { status: 'failed', reason: 'transport' })
    })

    it('closes the first order’s preview', async () => {
      const { user, rerender } = renderPage(<OrderMessages orderId={ORDER_ACCEPTED} />)
      const list = await screen.findByRole('list', { name: 'Customer messages' })
      await user.click(within(list).getByRole('button', { name: 'Send again' }))
      expect(await screen.findByRole('region', { name: 'Message to send' })).toHaveTextContent(ORDER_ACCEPTED)
      rerender(<OrderMessages orderId={ORDER_DRAFT} />)
      // The second order's list loads, and no preview of the first comes back with it.
      await screen.findByRole('list', { name: 'Customer messages' })
      expect(screen.queryByRole('region', { name: 'Message to send' })).not.toBeInTheDocument()
    })

    it('drops a preview that answers after the order changed', async () => {
      let release!: () => void
      const held = new Promise<void>((resolve) => (release = resolve))
      let asked = false
      server.use(
        http.post('/api/v1/ai/orders/:id/messages/preview', async ({ params }) => {
          asked = true
          await held
          return HttpResponse.json({
            message: { source: 'remote:mail', tool: 'send_email', to: [{ kind: 'email', value: 'ada@example.com' }], subject: null, text: `late for ${String(params.id)}` },
            digest: '0'.repeat(64),
          })
        }),
      )
      const { user, rerender } = renderPage(<OrderMessages orderId={ORDER_ACCEPTED} />)
      const list = await screen.findByRole('list', { name: 'Customer messages' })
      await user.click(within(list).getByRole('button', { name: 'Send again' }))
      await waitFor(() => expect(asked).toBe(true))
      rerender(<OrderMessages orderId={ORDER_DRAFT} />)
      release()
      await waitFor(() => expect(within(screen.getByRole('list', { name: 'Customer messages' })).getByRole('button', { name: 'Send again' })).toBeEnabled())
      expect(screen.queryByRole('region', { name: 'Message to send' })).not.toBeInTheDocument()
      expect(screen.queryByText(/late for/)).not.toBeInTheDocument()
    })

    // #5918's review N2: a send's answer is the first order's too.
    it('shows nothing of a send that answers after the order changed', async () => {
      let release!: () => void
      const held = new Promise<void>((resolve) => (release = resolve))
      let asked = false
      server.use(
        http.post('/api/v1/ai/orders/:id/messages/send', async () => {
          asked = true
          await held
          return HttpResponse.json({ sent: true, message: { status: 'sent' } })
        }),
      )
      const { user, rerender } = renderPage(<OrderMessages orderId={ORDER_ACCEPTED} />)
      const list = await screen.findByRole('list', { name: 'Customer messages' })
      await user.click(within(list).getByRole('button', { name: 'Send again' }))
      const shown = await screen.findByRole('region', { name: 'Message to send' })
      await user.click(within(shown).getByRole('button', { name: 'Send' }))
      await waitFor(() => expect(asked).toBe(true))
      rerender(<OrderMessages orderId={ORDER_DRAFT} />)
      release()
      await waitFor(() => expect(within(screen.getByRole('list', { name: 'Customer messages' })).getByRole('button', { name: 'Send again' })).toBeEnabled())
      expect(screen.queryByRole('status')).not.toBeInTheDocument()
      expect(screen.queryByText('Sent.')).not.toBeInTheDocument()
    })

    // #5922's review: an error is the first order's too.
    it('shows no error of a send that fails after the order changed', async () => {
      let release!: () => void
      const held = new Promise<void>((resolve) => (release = resolve))
      let asked = false
      server.use(
        http.post('/api/v1/ai/orders/:id/messages/send', async () => {
          asked = true
          await held
          return HttpResponse.json({ detail: 'already_notified: told', type: 'already_notified' }, { status: 409 })
        }),
      )
      const { user, rerender } = renderPage(<OrderMessages orderId={ORDER_ACCEPTED} />)
      const list = await screen.findByRole('list', { name: 'Customer messages' })
      await user.click(within(list).getByRole('button', { name: 'Send again' }))
      const shown = await screen.findByRole('region', { name: 'Message to send' })
      await user.click(within(shown).getByRole('button', { name: 'Send' }))
      await waitFor(() => expect(asked).toBe(true))
      rerender(<OrderMessages orderId={ORDER_DRAFT} />)
      release()
      await waitFor(() => expect(within(screen.getByRole('list', { name: 'Customer messages' })).getByRole('button', { name: 'Send again' })).toBeEnabled())
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    })
  })
})
