import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { beforeEach, describe, expect, it } from 'vitest'
import { mockSettings, resetMockState } from '../../mocks/handlers'
import { mockTestSends } from '../../mocks/features/orders/messages'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { CustomerMessaging, SIGNED_IN_ONLY } from './CustomerMessaging'

async function fill(user: ReturnType<typeof renderPage>['user'], region: HTMLElement, tool = 'send_email') {
  await waitFor(() => expect(within(region).getByRole('button', { name: 'Check and save' })).toBeEnabled())
  await user.type(within(region).getByLabelText('Plugin'), 'remote:mail')
  await user.type(within(region).getByLabelText('Tool'), tool)
  await user.type(within(region).getByLabelText('Recipients argument'), 'to')
  await user.type(within(region).getByLabelText('Subject argument'), 'subject')
  await user.type(within(region).getByLabelText('Text argument'), 'body')
}

describe('CustomerMessaging (#5660)', () => {
  beforeEach(() => resetMockState())

  it('saves a binding the plugin’s tool takes, and unbinds it', async () => {
    const { user } = renderPage(<CustomerMessaging />)
    const region = screen.getByRole('region', { name: 'Customer messages' })
    await fill(user, region)
    await user.click(within(region).getByRole('button', { name: 'Add fixed argument' }))
    await user.type(within(region).getByLabelText('Fixed argument 1'), 'from')
    await user.type(within(region).getByLabelText('Fixed argument 1 value'), 'shop@example.com')
    await user.click(within(region).getByRole('button', { name: 'Check and save' }))
    await waitFor(() =>
      expect(mockSettings().orders_customer_messaging).toEqual({
        source: 'remote:mail',
        tool: 'send_email',
        contact_kind: 'email',
        args: { to: 'to', to_shape: 'list', subject: 'subject', text: 'body', idempotency_key: null },
        fixed_args: { from: 'shop@example.com' },
      }),
    )
    expect(await within(region).findByRole('status')).toHaveTextContent('Saved.')

    await user.click(await within(region).findByRole('button', { name: 'Unbind' }))
    await waitFor(() => expect(mockSettings().orders_customer_messaging).toBeNull())
  })

  it('saves nothing when the plugin cannot take the binding', async () => {
    const { user } = renderPage(<CustomerMessaging />)
    const region = screen.getByRole('region', { name: 'Customer messages' })
    await fill(user, region, 'missing_tool')
    await user.click(within(region).getByRole('button', { name: 'Check and save' }))
    expect(await within(region).findByRole('alert')).toHaveTextContent('The plugin cannot take this binding: remote:mail lists no tool missing_tool')
    expect(mockSettings().orders_customer_messaging).toBeNull()
  })

  it('sends one test message to the address typed', async () => {
    const { user } = renderPage(<CustomerMessaging />)
    const region = screen.getByRole('region', { name: 'Customer messages' })
    await fill(user, region)
    await user.type(within(region).getByLabelText('Test address'), 'me@example.com')
    await user.click(within(region).getByRole('button', { name: 'Send a test' }))
    expect(await within(region).findByRole('status')).toHaveTextContent('Test message sent to me@example.com.')
    expect(mockTestSends()).toEqual([{ to: 'me@example.com' }])
    expect(mockSettings().orders_customer_messaging).toBeNull()
  })

  it('says a test needs a person signed in when the agent refuses it so', async () => {
    server.use(http.post('/api/v1/ai/orders/messaging-binding/test', () => HttpResponse.json({ detail: 'a test message needs a person signed in' }, { status: 401 })))
    const { user } = renderPage(<CustomerMessaging />)
    const region = screen.getByRole('region', { name: 'Customer messages' })
    expect(within(region).getByText(/needs a person signed in to ScadBuddy/)).toBeInTheDocument()
    await fill(user, region)
    await user.type(within(region).getByLabelText('Test address'), 'me@example.com')
    await user.click(within(region).getByRole('button', { name: 'Send a test' }))
    expect(await within(region).findByRole('alert')).toHaveTextContent(SIGNED_IN_ONLY)
    expect(mockTestSends()).toEqual([])
  })
})
