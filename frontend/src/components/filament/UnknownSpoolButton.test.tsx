import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { seedUnknownTray } from '../../mocks/features/filament/unknownTrays'
import { resetMockState } from '../../mocks/handlers'
import { dismissToast, getToasts } from '../../lib/toast'
import { NeedsYouHarness } from '../../test/needsYou'
import { renderPage } from '../../test/utils'
import { server } from '../../mocks/server'
import { http, HttpResponse } from 'msw'
import { UnknownSpoolButton } from './UnknownSpoolButton'

const clearToasts = () => getToasts().forEach((one) => dismissToast(one.id))

describe('UnknownSpoolButton (#7143)', () => {
  beforeEach(() => {
    resetMockState()
    clearToasts()
  })

  it('opens the shell dialog for its tray, toasts the link and calls onResolved', async () => {
    const tray = seedUnknownTray()
    const onResolved = vi.fn()
    const { user } = renderPage(
      <NeedsYouHarness>
        <UnknownSpoolButton trayId={tray.id} onResolved={onResolved} />
      </NeedsYouHarness>,
    )
    await user.click(await screen.findByRole('button', { name: 'Tell ScadBuddy which spool…' }))
    const dialog = await screen.findByRole('dialog', { name: 'Unknown spool in 3DP-31B-598 AMS-A slot 2' })
    await user.click(within(dialog).getByRole('radio', { name: /S-9\b/ }))
    await user.type(within(dialog).getByLabelText('Net weight (g)'), '980')
    await user.click(within(dialog).getByRole('button', { name: 'Link spool' }))
    await waitFor(() => expect(onResolved).toHaveBeenCalledTimes(1))
    expect(getToasts().map((one) => one.title)).toContain('Linked S-9 to AMS-A slot 2')
  })

  it('says so when the tray is no longer unknown', async () => {
    seedUnknownTray()
    const { user } = renderPage(
      <NeedsYouHarness>
        <UnknownSpoolButton trayId="00000000-0000-4000-8000-0000000000ff" label="Link it" />
      </NeedsYouHarness>,
    )
    await user.click(await screen.findByRole('button', { name: 'Link it' }))
    expect(await screen.findByText('That tray is no longer unknown.')).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('asks the caller to read again when the tray is no longer unknown', async () => {
    seedUnknownTray()
    const onResolved = vi.fn()
    const { user } = renderPage(
      <NeedsYouHarness>
        <UnknownSpoolButton trayId="00000000-0000-4000-8000-0000000000ff" label="Link it" onResolved={onResolved} />
      </NeedsYouHarness>,
    )
    await user.click(await screen.findByRole('button', { name: 'Link it' }))
    await screen.findByText('That tray is no longer unknown.')
    expect(onResolved).toHaveBeenCalledTimes(1)
  })

  describe('with the unknown-spool area off', () => {
    const off = () => server.use(http.get('/api/v1/filament/unknown-trays', () => HttpResponse.json({ status: 404, detail: 'Not Found' }, { status: 404 })))

    it('says how to link from the slot page, and never that the tray is no longer unknown', async () => {
      off()
      const { user } = renderPage(
        <NeedsYouHarness>
          <UnknownSpoolButton trayId="t1" label="Link it" />
        </NeedsYouHarness>,
      )
      await user.click(await screen.findByRole('button', { name: 'Link it' }))
      const note = await screen.findByRole('status')
      expect(note).toHaveTextContent('Filament, then Locations')
      expect(note).toHaveTextContent('New spool from this tray')
      expect(within(note).getByRole('link', { name: 'Open Locations' })).toHaveAttribute('href', '/filament/locations')
      expect(screen.queryByText('That tray is no longer unknown.')).toBeNull()
    })

    it('links to the place when the tray place is known', async () => {
      off()
      const { user } = renderPage(
        <NeedsYouHarness>
          <UnknownSpoolButton trayId="t1" label="Link it" placeId="loc-1" />
        </NeedsYouHarness>,
      )
      await user.click(await screen.findByRole('button', { name: 'Link it' }))
      expect(await screen.findByRole('link', { name: "Open the slot's page" })).toHaveAttribute('href', '/filament/locations/loc-1')
    })
  })
})
