import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { api } from '../../../api/client'
import { resetMockState } from '../../../mocks/handlers'
import { mockConnectProvider, mockPaymentEvent, setMockKek } from '../../../mocks/features/payments'
import { ORDER_ACCEPTED } from '../../../mocks/features/orders/state'
import { enter, region, renderSettingsSection } from '../testing'
import { Component as Payments } from './payments'

const route = '/settings/system'

async function fill(user: ReturnType<typeof renderSettingsSection>['user']) {
  await enter(user, await screen.findByLabelText('Account'), 'a')
  await enter(user, screen.getByLabelText('Signing secret'), 's3cret')
}

describe('Settings → Payments (#4957)', () => {
  beforeEach(() => resetMockState())

  it('connects a provider and never shows its secret again', async () => {
    const { user } = renderSettingsSection(<Payments />, { route })
    await fill(user)
    expect(screen.getByLabelText('Signing secret')).toHaveAttribute('type', 'password')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText('Connected as Fake account a')).toBeInTheDocument()
    expect(screen.queryByDisplayValue('s3cret')).not.toBeInTheDocument()
    expect(screen.getByText('No webhook received yet')).toBeInTheDocument()
    expect(JSON.stringify(await api.paymentProviders())).not.toContain('s3cret')
  })

  it('says the key file is missing on kek_missing', async () => {
    setMockKek(false)
    const { user } = renderSettingsSection(<Payments />, { route })
    await fill(user)
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/key file/)
    expect(screen.queryByText(/Connected as/)).not.toBeInTheDocument()
  })

  it('shows when the last webhook came', async () => {
    mockConnectProvider()
    await api.addPayment(ORDER_ACCEPTED, { amount_minor: 100 })
    const opened = await api.requestPayment(ORDER_ACCEPTED, {})
    mockPaymentEvent(ORDER_ACCEPTED, opened.id, 'expired')
    renderSettingsSection(<Payments />, { route })
    expect(await screen.findByText(/Last webhook/)).toBeInTheDocument()
  })

  it('refuses to disconnect while a request is open, with the backend’s reason', async () => {
    mockConnectProvider()
    await api.requestPayment(ORDER_ACCEPTED, {})
    const { user } = renderSettingsSection(<Payments />, { route })
    await user.click(await screen.findByRole('button', { name: 'Disconnect' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('open payment requests')
    expect(screen.getByText('Connected as Fake account a')).toBeInTheDocument()
  })

  it('disconnects a provider with no open request', async () => {
    mockConnectProvider()
    const { user } = renderSettingsSection(<Payments />, { route })
    await user.click(await screen.findByRole('button', { name: 'Disconnect' }))
    await waitFor(() => expect(screen.queryByText(/Connected as/)).not.toBeInTheDocument())
    expect(within(region('Payments')).getByLabelText('Account')).toBeInTheDocument()
  })
})
