import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { expect, it } from 'vitest'
import { OLD_PHONE, PHONE } from '../mocks/features/devices'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { DevicesPage } from './DevicesPage'

const row = async (name: string) => (await screen.findByRole('heading', { name, level: 2 })).closest('li') as HTMLElement

it('lists each device, online or offline, with what it can do and when it expires', async () => {
  renderPage(<DevicesPage />)
  const phone = await row(PHONE.name)
  expect(within(phone).getByText('Online')).toBeInTheDocument()
  expect(within(phone).getByText('Google Pixel 8, app 1.4.0')).toBeInTheDocument()
  expect(within(within(phone).getByRole('list', { name: `What ${PHONE.name} can do` })).getAllByRole('listitem').map((one) => one.textContent)).toEqual([
    'Read NFC tags',
    'Write NFC tags',
    'Scan codes',
    'Take photos',
  ])
  expect(within(phone).getByText('Expires if not used')).toBeInTheDocument()
  expect(within(phone).getByText('Pairing ends')).toBeInTheDocument()

  const old = await row(OLD_PHONE.name)
  expect(within(old).getByText(/^Offline, last seen /)).toBeInTheDocument()
  expect(within(old).queryByText('Online')).not.toBeInTheDocument()
})

it('shows a pairing code as a QR code and a scadbuddy://pair link on this host', async () => {
  const { user } = renderPage(<DevicesPage />)
  await row(PHONE.name)
  await user.click(screen.getByRole('button', { name: 'Add a device' }))
  const pairing = await screen.findByRole('region', { name: 'Pair a device' })
  expect(within(pairing).getByRole('img', { name: 'Pairing QR code' }).querySelector('svg')).not.toBeNull()
  const link = within(pairing).getByRole('link', { name: 'Open the pairing link' })
  const href = link.getAttribute('href') ?? ''
  expect(href.startsWith('scadbuddy://pair?url=')).toBe(true)
  const target = new URL(decodeURIComponent(href.slice('scadbuddy://pair?url='.length)))
  expect(target.origin).toBe(window.location.origin)
  expect(target.pathname).toMatch(/^\/api\/v1\/devices\/pairings\/mock-pairing-code-\d+\/claim$/)
  await user.click(within(pairing).getByRole('button', { name: 'Cancel' }))
  expect(screen.queryByRole('region', { name: 'Pair a device' })).not.toBeInTheDocument()
})

it('moves a pairing link core made on another host onto the host the page is on', async () => {
  server.use(http.post('*/api/v1/devices/pairings', () => HttpResponse.json({ code_url: 'http://10.0.0.5:8000/api/v1/devices/pairings/abc/claim', expires_at: '2099-01-01T00:00:00Z' })))
  const { user } = renderPage(<DevicesPage />)
  await row(PHONE.name)
  await user.click(screen.getByRole('button', { name: 'Add a device' }))
  const link = await screen.findByRole('link', { name: 'Open the pairing link' })
  expect(link).toHaveAttribute('href', `scadbuddy://pair?url=${encodeURIComponent(`${window.location.origin}/api/v1/devices/pairings/abc/claim`)}`)
})

it('revokes a device after a confirm', async () => {
  const { user } = renderPage(<DevicesPage />)
  await row(OLD_PHONE.name)
  await user.click(screen.getByRole('button', { name: `Revoke ${OLD_PHONE.name}` }))
  const dialog = await screen.findByRole('alertdialog')
  expect(within(dialog).getByText(`Revoke ${OLD_PHONE.name}?`)).toBeInTheDocument()
  await user.click(within(dialog).getByRole('button', { name: 'Revoke device' }))
  await waitFor(() => expect(screen.queryByRole('heading', { name: OLD_PHONE.name })).not.toBeInTheDocument())
  expect(screen.getByRole('heading', { name: PHONE.name })).toBeInTheDocument()
})

it('says so when this ScadBuddy does not serve the devices routes', async () => {
  server.use(http.get('*/api/v1/devices', () => HttpResponse.json({ title: 'Not Found', status: 404, detail: 'Not Found' }, { status: 404 })))
  renderPage(<DevicesPage />)
  expect(await screen.findByText('This ScadBuddy does not have devices yet.')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Add a device' })).not.toBeInTheDocument()
})
