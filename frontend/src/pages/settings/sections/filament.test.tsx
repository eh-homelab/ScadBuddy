import { act, screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { api } from '../../../api/client'
import { getSyncStatus } from '../../../api/filament'
import { describe, expect, it, vi } from 'vitest'
import { spoolId } from '../../../mocks/features/filament/seed'
import { linkOtherBambuddy } from '../../../mocks/features/filament/sync'
import { mockSettings } from '../../../mocks/handlers'
import { server } from '../../../mocks/server'
import { choose } from '../../../test/select'
import { enter, region, renderSettingsSection } from '../testing'
import { SECTION_REGISTRY } from '../registry'
import { Component as Filament } from './filament'

async function seeded() {
  await screen.findByLabelText('Low stock below, percent')
}

describe('Filament (#6262)', () => {
  it('shows the sync choice, the interval, the thresholds and the currency', async () => {
    renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const filament = region('Filament')
    expect(within(filament).getByRole('combobox', { name: 'Spools and types' })).toHaveTextContent('Bambuddy → ScadBuddy')
    expect(within(filament).getByLabelText('Pull from Bambuddy every')).toHaveValue(300)
    expect(within(filament).getByLabelText('Low stock below, percent')).toHaveValue(20)
    expect(within(filament).getByLabelText('Default cost per kg')).toHaveValue('')
    expect(within(filament).getByText('USD')).toBeInTheDocument()
    expect(within(filament).getByRole('link', { name: 'Change it in Orders' })).toHaveAttribute(
      'href',
      expect.stringContaining('/settings/printing#orders'),
    )
    expect(within(filament).getByRole('link', { name: 'Filament' })).toHaveAttribute('href', expect.stringContaining('/filament'))
  })

  it('saves the burn window, the lead time and the safety margin with Save Filament (#6269)', async () => {
    const { user } = renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const filament = region('Filament')
    expect(within(filament).getByLabelText('Burn window, days')).toHaveValue(30)
    expect(within(filament).getByLabelText('Lead time, days')).toHaveValue(7)
    expect(within(filament).getByLabelText('Safety margin, days')).toHaveValue(3)
    await user.clear(within(filament).getByLabelText('Burn window, days'))
    await enter(user, within(filament).getByLabelText('Burn window, days'), '14')
    await user.clear(within(filament).getByLabelText('Lead time, days'))
    await enter(user, within(filament).getByLabelText('Lead time, days'), '21')
    await user.clear(within(filament).getByLabelText('Safety margin, days'))
    await enter(user, within(filament).getByLabelText('Safety margin, days'), '10')
    await user.click(within(filament).getByRole('button', { name: 'Save Filament' }))
    await waitFor(() => expect(mockSettings()).toMatchObject({ filament_burn_window_days: 14, filament_lead_time_days: 21, filament_safety_margin_days: 10 }))
  })

  const DEFAULT_SYNC = { spools: 'bb_to_sb', weights: 'bb_to_sb', tags: 'bb_to_sb', presets: 'bb_to_sb', suppliers: 'off', locations: 'off' } as const

  it('offers the four directions for each of the six data types', async () => {
    const { user } = renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const filament = region('Filament')
    for (const [label, shown] of [
      ['Spools and types', 'Bambuddy → ScadBuddy'],
      ['Weights', 'Bambuddy → ScadBuddy'],
      ['Tags', 'Bambuddy → ScadBuddy'],
      ['Suppliers and prices', 'Off'],
      ['Locations', 'Off'],
      ['Presets and K values', 'Bambuddy → ScadBuddy'],
    ] as const) {
      expect(within(filament).getByRole('combobox', { name: label })).toHaveTextContent(shown)
    }
    await user.click(within(filament).getByRole('combobox', { name: 'Locations' }))
    expect((await screen.findAllByRole('option')).map((option) => option.textContent)).toEqual([
      'Off',
      'ScadBuddy → Bambuddy',
      'Bambuddy → ScadBuddy',
      'Both ways (newer change wins)',
    ])
  })

  it('saves every direction in one filament_sync value', async () => {
    let sent: Record<string, unknown> | undefined
    server.events.on('request:start', async ({ request }) => {
      if (request.method === 'PUT' && new URL(request.url).pathname === '/api/v1/settings') sent = (await request.clone().json()) as Record<string, unknown>
    })
    const { user } = renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const filament = region('Filament')
    await choose(user, within(filament).getByRole('combobox', { name: 'Weights' }), 'Both ways (newer change wins)')
    expect(mockSettings().filament_sync).toEqual(DEFAULT_SYNC)
    await user.click(within(filament).getByRole('button', { name: 'Save Filament' }))
    await waitFor(() => expect(sent?.filament_sync).toEqual({ ...DEFAULT_SYNC, weights: 'two_way' }))
    await waitFor(() => expect(mockSettings().filament_sync).toEqual({ ...DEFAULT_SYNC, weights: 'two_way' }))
    server.events.removeAllListeners()
  })

  it('keeps each choice beside the others until the save', async () => {
    const { user } = renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const filament = region('Filament')
    await choose(user, within(filament).getByRole('combobox', { name: 'Tags' }), 'ScadBuddy → Bambuddy')
    await choose(user, within(filament).getByRole('combobox', { name: 'Spools and types' }), 'Off')
    await choose(user, within(filament).getByRole('combobox', { name: 'Suppliers and prices' }), 'Bambuddy → ScadBuddy')
    expect(within(filament).getByRole('combobox', { name: 'Tags' })).toHaveTextContent('ScadBuddy → Bambuddy')
    await user.click(within(filament).getByRole('button', { name: 'Save Filament' }))
    await waitFor(() =>
      expect(mockSettings().filament_sync).toEqual({ ...DEFAULT_SYNC, spools: 'off', tags: 'sb_to_bb', suppliers: 'bb_to_sb' }),
    )
  })

  it('the mock accepts two_way on every key', async () => {
    const everyWay = { spools: 'two_way', weights: 'two_way', tags: 'two_way', presets: 'two_way', suppliers: 'two_way', locations: 'two_way' } as const
    await api.putSettings({ filament_sync: everyWay })
    expect(mockSettings().filament_sync).toEqual(everyWay)
    await expect(api.putSettings({ filament_sync: { ...everyWay, tags: 'sideways' as 'off' } })).rejects.toMatchObject({ status: 422 })
  })

  it('shows a failed spool with its error', async () => {
    renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const failed = await within(region('Filament')).findByRole('list', { name: 'Items not synced' })
    const [spool, supplier] = within(failed).getAllByRole('listitem') as [HTMLElement, HTMLElement]
    expect(within(spool).getByRole('link', { name: 'Spool #3' })).toHaveAttribute('href', expect.stringContaining(`/filament/spools/${spoolId(3)}`))
    expect(within(supplier).getByRole('link', { name: 'Supplier Micro Center' })).toBeInTheDocument()
    expect(spool).toHaveTextContent('Bambuddy refused it (HTTP 422).')
    expect(spool).toHaveTextContent('2 attempts')
    expect(supplier).toHaveTextContent('Not sent: another Bambuddy row has this name.')
    expect(within(region('Filament')).getByText(/No pass runs: no data type but tags is set to send to Bambuddy/)).toBeInTheDocument()
  })

  it('names an item that is gone by its id', async () => {
    const gone = '0badc0de-0000-4000-8000-000000000001'
    server.use(
      http.get('*/api/v1/filament/sync/status', () =>
        HttpResponse.json({
          instance: null,
          last_pass_at: null,
          last_pass_started_at: null,
          last_ok_at: null,
          last_error: null,
          counts: {},
          errors: [{ kind: 'location', sb_id: gone, label: null, error: 'internal', attempts: 1, at: '2026-11-15T07:00:00Z' }],
          directions: DEFAULT_SYNC,
          skipped: null,
        }),
      ),
    )
    renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const failed = await within(region('Filament')).findByRole('list', { name: 'Items not synced' })
    expect(within(failed).getByRole('link', { name: 'Location 0badc0de' })).toBeInTheDocument()
  })

  /** The sync status reads made so far, counted from msw's request events. */
  function countStatusReads() {
    const count = { reads: 0 }
    server.events.on('request:start', ({ request }) => {
      if (new URL(request.url).pathname === '/api/v1/filament/sync/status') count.reads += 1
    })
    return count
  }

  async function startSync() {
    await api.putSettings({ filament_sync: { ...DEFAULT_SYNC, weights: 'two_way' } })
    const rendered = renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const filament = region('Filament')
    const failed = await within(filament).findByRole('list', { name: 'Items not synced' })
    expect(within(failed).getAllByRole('listitem')[0]).toHaveTextContent('2 attempts')
    await rendered.user.click(within(filament).getByRole('button', { name: 'Sync now' }))
    // Announced politely: the note is its own live region.
    expect(await within(filament).findByText(/Sync started/)).toHaveAttribute('aria-live', 'polite')
    return { ...rendered, filament, failed }
  }

  it('sync now re-reads the status every 3 s until the pass lands', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const count = countStatusReads()
    try {
      const { filament, failed } = await startSync()
      // The mock's pass lands on the second read after the run, as a background pass would.
      await act(() => vi.advanceTimersByTimeAsync(3_000))
      expect(within(failed).getAllByRole('listitem')[0]).toHaveTextContent('2 attempts')
      const before = count.reads
      await act(() => vi.advanceTimersByTimeAsync(3_000))
      await waitFor(() => expect(within(failed).getAllByRole('listitem')[0]).toHaveTextContent('3 attempts'))
      expect(within(filament).getByText('Sync finished.')).toBeInTheDocument()
      expect(count.reads).toBe(before + 1)
      // Landed: no more reads.
      await act(() => vi.advanceTimersByTimeAsync(9_000))
      expect(count.reads).toBe(before + 1)
    } finally {
      server.events.removeAllListeners()
      vi.useRealTimers()
    }
  })

  it('keeps reading the status when a pass from before the request lands first', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      const { filament } = await startSync()
      // A timer pass (or an earlier nudge's) that started before the run lands first.
      let early = true
      server.use(
        http.get('*/api/v1/filament/sync/status', () => {
          if (!early) return
          early = false
          return HttpResponse.json({
            instance: null,
            last_pass_at: '2026-11-15T07:00:30Z',
            last_pass_started_at: '2026-11-15T07:00:00Z',
            last_ok_at: '2026-11-15T07:00:30Z',
            last_error: null,
            counts: {},
            errors: [],
            directions: { ...DEFAULT_SYNC, weights: 'two_way' },
            skipped: null,
          })
        }),
      )
      await act(() => vi.advanceTimersByTimeAsync(3_000))
      await waitFor(() => expect(within(filament).queryByRole('list', { name: 'Items not synced' })).toBeNull())
      expect(within(filament).queryByText('Sync finished.')).toBeNull()
      expect(within(filament).getByText(/Sync started/)).toBeInTheDocument()
      // The pass the run asked for lands on the mock's second read.
      await act(() => vi.advanceTimersByTimeAsync(6_000))
      expect(await within(filament).findByText('Sync finished.')).toBeInTheDocument()
      const failed = within(filament).getByRole('list', { name: 'Items not synced' })
      expect(within(failed).getAllByRole('listitem')[0]).toHaveTextContent('3 attempts')
    } finally {
      vi.useRealTimers()
    }
  })

  it('ends the read on a pass that started after the request even when the browser clock is ahead', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      server.use(http.post('*/api/v1/filament/sync/run', () => HttpResponse.json({ started: true, requested_at: '2026-11-15T08:00:00Z' }, { status: 202 })))
      const { filament } = await startSync()
      // The browser's clock is the real now, far past the server's 2026-11-15.
      server.use(
        http.get('*/api/v1/filament/sync/status', () =>
          HttpResponse.json({
            instance: null,
            last_pass_at: '2026-11-15T08:00:07Z',
            last_pass_started_at: '2026-11-15T08:00:05Z',
            last_ok_at: '2026-11-15T08:00:07Z',
            last_error: null,
            counts: {},
            errors: [],
            directions: { ...DEFAULT_SYNC, weights: 'two_way' },
            skipped: null,
          }),
        ),
      )
      await act(() => vi.advanceTimersByTimeAsync(3_000))
      expect(await within(filament).findByText('Sync finished.')).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('says the pass is still running after 60 s with no new pass', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    server.use(http.post('*/api/v1/filament/sync/run', () => HttpResponse.json({ started: true, requested_at: '2026-11-15T08:00:00Z' }, { status: 202 })))
    const count = countStatusReads()
    try {
      const { filament } = await startSync()
      const before = count.reads
      await act(() => vi.advanceTimersByTimeAsync(60_000))
      expect(await within(filament).findByText('The pass is still running. Check back later.')).toBeInTheDocument()
      expect(count.reads - before).toBe(20)
      await act(() => vi.advanceTimersByTimeAsync(9_000))
      expect(count.reads - before).toBe(20)
    } finally {
      server.events.removeAllListeners()
      vi.useRealTimers()
    }
  })

  it('stops reading the status when the page goes away', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const count = countStatusReads()
    try {
      const { unmount } = await startSync()
      const before = count.reads
      unmount()
      // Five intervals: a poll left running would read the status again (twice, until the mock pass lands).
      await act(() => vi.advanceTimersByTimeAsync(15_000))
      expect(count.reads).toBe(before)
    } finally {
      server.events.removeAllListeners()
      vi.useRealTimers()
    }
  })

  it('links to Connection when no Bambuddy is set up', async () => {
    server.use(
      http.post('*/api/v1/filament/sync/run', () =>
        HttpResponse.json({ type: 'about:blank', title: 'Conflict', status: 409, detail: 'no Bambuddy is configured', code: 'no_bambuddy' }, { status: 409 }),
      ),
    )
    const { user } = renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const filament = region('Filament')
    await user.click(await within(filament).findByRole('button', { name: 'Sync now' }))
    const alert = await within(filament).findByRole('alert')
    expect(within(alert).getByRole('link', { name: 'Connection' })).toHaveAttribute('href', expect.stringContaining('/settings/general#connection'))
  })

  it('names the sync status block for its label', async () => {
    renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    expect(document.getElementById('filament-sync-status')).not.toBeNull()
    expect(within(region('Filament')).getByText('Sync status').closest('label')).toHaveAttribute('for', 'filament-sync-status')
  })

  it('says what to change when nothing is set to go to Bambuddy', async () => {
    const { user } = renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const filament = region('Filament')
    await user.click(await within(filament).findByRole('button', { name: 'Sync now' }))
    expect(await within(filament).findByRole('alert')).toHaveTextContent(/Nothing is set to go to Bambuddy\. Set a data type to "ScadBuddy → Bambuddy" or "Both ways"/)
  })

  it('says to try again later when the sync could not be started', async () => {
    server.use(
      http.post('*/api/v1/filament/sync/run', () =>
        HttpResponse.json({ type: 'about:blank', title: 'Service Unavailable', status: 503, detail: 'the sync could not be started: internal' }, { status: 503 }),
      ),
    )
    const { user } = renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const filament = region('Filament')
    await user.click(await within(filament).findByRole('button', { name: 'Sync now' }))
    expect(await within(filament).findByRole('alert')).toHaveTextContent('The sync could not be started. Try again later.')
  })

  describe('Reset Bambuddy links (#6773)', () => {
    async function withSync() {
      await api.putSettings({ filament_sync: { ...DEFAULT_SYNC, spools: 'two_way' } })
      const rendered = renderSettingsSection(<Filament />, { route: '/settings/printing' })
      await seeded()
      return { ...rendered, filament: region('Filament') }
    }

    it('is not offered while the links belong to the configured Bambuddy', async () => {
      const { filament } = await withSync()
      await within(filament).findByRole('button', { name: 'Sync now' })
      expect(within(filament).queryByRole('button', { name: 'Reset Bambuddy links' })).toBeNull()
    })

    it('names what it deletes, says no ScadBuddy spool goes, resets the instance it read and waits for the pass', async () => {
      linkOtherBambuddy('0ld0ld0ld0ld')
      let sent: unknown
      server.events.on('request:start', ({ request }) => {
        if (request.method === 'POST' && request.url.endsWith('/filament/sync/reset-links')) void request.clone().json().then((body) => (sent = body))
      })
      try {
        const { user, filament } = await withSync()
        expect(await within(filament).findByText(/The links belong to another Bambuddy/)).toBeInTheDocument()
        await user.click(within(filament).getByRole('button', { name: 'Reset Bambuddy links' }))
        const dialog = await screen.findByRole('alertdialog', { name: 'Reset the Bambuddy links?' })
        expect(dialog).toHaveTextContent('No ScadBuddy spool, type, supplier or location is deleted')
        const items = within(within(dialog).getByRole('list', { name: 'What the reset deletes' })).getAllByRole('listitem')
        expect(items.map((item) => item.textContent)).toEqual([
          '12 spool links',
          '4 spool type links',
          '1 insert type link',
          '2 supplier links',
          '3 location links',
          '1 unfinished create',
          '2 listed sync errors',
        ])
        await user.click(within(dialog).getByRole('button', { name: 'Reset links' }))
        await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
        expect(sent).toEqual({ confirm_instance: '0ld0ld0ld0ld' })
        expect(await within(filament).findByText('22 links reset. A full pass now links the new Bambuddy.')).toBeInTheDocument()
        expect(within(filament).queryByRole('button', { name: 'Reset Bambuddy links' })).toBeNull()
        expect(within(filament).queryByRole('list', { name: 'Items not synced' })).toBeNull()
      } finally {
        server.events.removeAllListeners()
      }
    })

    it('warns that an untagged spool can end up twice when the Bambuddy is another one', async () => {
      linkOtherBambuddy()
      const { user, filament } = await withSync()
      await user.click(await within(filament).findByRole('button', { name: 'Reset Bambuddy links' }))
      const dialog = await screen.findByRole('alertdialog', { name: 'Reset the Bambuddy links?' })
      expect(dialog).toHaveTextContent('a spool with no tag can end up twice')
    })

    it('keeps the links when the Bambuddy set up now is the same one at a new address (#6845)', async () => {
      linkOtherBambuddy('0ld0ld0ld0ld', { same: true })
      const { user, filament } = await withSync()
      await user.click(await within(filament).findByRole('button', { name: 'Reset Bambuddy links' }))
      const dialog = await screen.findByRole('alertdialog', { name: 'Keep the Bambuddy links?' })
      expect(dialog).toHaveTextContent('Nothing is deleted')
      expect(dialog).not.toHaveTextContent('end up twice')
      expect(within(dialog).queryByRole('list', { name: 'What the reset deletes' })).toBeNull()
      await user.click(within(dialog).getByRole('button', { name: 'Keep links' }))
      await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
      expect(await within(filament).findByText('Links kept. A full pass now syncs with the Bambuddy at its new address.')).toBeInTheDocument()
      expect(within(filament).queryByRole('button', { name: 'Reset Bambuddy links' })).toBeNull()
    })

    it('keeps everything when the person cancels', async () => {
      linkOtherBambuddy()
      const { user, filament } = await withSync()
      await user.click(await within(filament).findByRole('button', { name: 'Reset Bambuddy links' }))
      const dialog = await screen.findByRole('alertdialog')
      await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
      await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
      expect(within(filament).getByRole('button', { name: 'Reset Bambuddy links' })).toBeInTheDocument()
      expect((await getSyncStatus()).instance_changed).toBe(true)
    })

    it('says nothing was deleted when the links changed since the dialog opened', async () => {
      linkOtherBambuddy()
      server.use(
        http.post('*/api/v1/filament/sync/reset-links', () =>
          HttpResponse.json(
            { type: 'about:blank', title: 'Conflict', status: 409, detail: 'confirm_instance is not the instance the links belong to', code: 'confirm_mismatch' },
            { status: 409 },
          ),
        ),
      )
      const { user, filament } = await withSync()
      await user.click(await within(filament).findByRole('button', { name: 'Reset Bambuddy links' }))
      const dialog = await screen.findByRole('alertdialog')
      await user.click(within(dialog).getByRole('button', { name: 'Reset links' }))
      expect(await within(dialog).findByRole('alert')).toHaveTextContent('The links changed since this dialog opened. Nothing was deleted.')
    })

    it('does not show the dialog\'s error under the button once the dialog is closed', async () => {
      linkOtherBambuddy()
      server.use(
        http.post('*/api/v1/filament/sync/reset-links', () =>
          HttpResponse.json(
            { type: 'about:blank', title: 'Conflict', status: 409, detail: 'confirm_instance is not the instance the links belong to', code: 'confirm_mismatch' },
            { status: 409 },
          ),
        ),
      )
      const { user, filament } = await withSync()
      await user.click(await within(filament).findByRole('button', { name: 'Reset Bambuddy links' }))
      const dialog = await screen.findByRole('alertdialog')
      await user.click(within(dialog).getByRole('button', { name: 'Reset links' }))
      await within(dialog).findByRole('alert')
      await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
      await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
      expect(within(filament).queryByText(/Nothing was deleted/)).toBeNull()
    })
  })

  it('switches the RFID override mode on and says Bambuddy no longer adds unknown spools', async () => {
    let sent: unknown
    server.use(
      http.put('*/api/v1/filament/rfid/override-mode', async ({ request }) => {
        sent = await request.json()
        return HttpResponse.json({ override_mode: true, state: 'applied', devices: [] })
      }),
    )
    const { user } = renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const filament = region('Filament')
    await user.click(await within(filament).findByRole('switch', { name: 'RFID override mode' }))
    await waitFor(() => expect(sent).toEqual({ enabled: true }))
    expect(await within(filament).findByText('Bambuddy no longer adds unknown RFID spools')).toBeInTheDocument()
    expect(within(filament).getByRole('switch', { name: 'RFID override mode' })).toBeChecked()
  })

  it('shows why a blocked override mode is blocked and leaves the switch on', async () => {
    server.use(
      http.get('*/api/v1/filament/rfid/status', () =>
        HttpResponse.json({ override_mode: true, state: 'blocked', detail: "Bambuddy's authentication is on, so ScadBuddy cannot change its settings", devices: [] }),
      ),
    )
    renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const filament = region('Filament')
    expect(await within(filament).findByText(/Bambuddy's authentication is on/)).toBeInTheDocument()
    expect(within(filament).getByRole('switch', { name: 'RFID override mode' })).toBeChecked()
  })

  it('says a busy update applies on the next refresh', async () => {
    server.use(
      http.get('*/api/v1/filament/rfid/status', () => HttpResponse.json({ override_mode: true, state: 'pending', detail: 'busy', devices: [] })),
    )
    renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    expect(await within(region('Filament')).findByText('Another update is running; it applies on the next refresh')).toBeInTheDocument()
  })

  it('applies now and says what the reconcile did', async () => {
    server.use(
      http.post('*/api/v1/filament/rfid/reconcile', () =>
        HttpResponse.json({
          mode: { override_mode: true, state: 'applied', devices: [] },
          tags: { pushed: 2, moved: 0, cleared: 0, unchanged: 1, failed: 0 },
          slots: { configured: 1, unchanged: 0, no_preset: 0, no_temps: 3, no_nozzle: 0, failed: 0 },
        }),
      ),
    )
    const { user } = renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const filament = region('Filament')
    await user.click(await within(filament).findByRole('button', { name: 'Apply now' }))
    expect(await within(filament).findByText(/2 tags sent/)).toBeInTheDocument()
    expect(within(filament).getByText(/3 spools have no nozzle temperatures/)).toBeInTheDocument()
  })

  it('says which tags and slots the reconcile could not send', async () => {
    server.use(
      http.post('*/api/v1/filament/rfid/reconcile', () =>
        HttpResponse.json({
          mode: { override_mode: true, state: 'applied', devices: [] },
          tags: { pushed: 0, moved: 0, cleared: 0, unchanged: 0, failed: 2 },
          slots: { configured: 0, unchanged: 0, no_preset: 0, no_temps: 0, no_nozzle: 0, failed: 1 },
        }),
      ),
    )
    const { user } = renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const filament = region('Filament')
    await user.click(await within(filament).findByRole('button', { name: 'Apply now' }))
    expect(await within(filament).findByText(/2 tags were not sent/)).toBeInTheDocument()
    expect(within(filament).getByText(/1 slot was not set/)).toBeInTheDocument()
  })

  it('says which slots were skipped for no preset or no nozzle', async () => {
    server.use(
      http.post('*/api/v1/filament/rfid/reconcile', () =>
        HttpResponse.json({
          mode: { override_mode: true, state: 'applied', devices: [] },
          tags: { pushed: 1, moved: 0, cleared: 0, unchanged: 0, failed: 0 },
          slots: { configured: 0, unchanged: 0, no_preset: 2, no_temps: 0, no_nozzle: 1, failed: 0 },
        }),
      ),
    )
    const { user } = renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const filament = region('Filament')
    await user.click(await within(filament).findByRole('button', { name: 'Apply now' }))
    expect(await within(filament).findByText(/2 spools have no preset for the nozzle/)).toBeInTheDocument()
    expect(within(filament).getByText(/1 slot: the printer does not report the nozzle for its tray, so it is not set/)).toBeInTheDocument()
  })

  it('saves the thresholds and shows the server refusing 101 percent', async () => {
    const { user } = renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const percent = () => within(region('Filament')).getByLabelText('Low stock below, percent')
    await user.clear(percent())
    await enter(user, percent(), '101')
    await user.click(within(region('Filament')).getByRole('button', { name: 'Save Filament' }))
    await within(region('Filament')).findByRole('alert')
    expect(mockSettings().filament_low_stock_pct).toBe(20)

    await user.clear(percent())
    await enter(user, percent(), '35')
    await enter(user, within(region('Filament')).getByLabelText('Default cost per kg'), '24.50')
    await user.click(within(region('Filament')).getByRole('button', { name: 'Save Filament' }))
    await waitFor(() => expect(mockSettings().filament_low_stock_pct).toBe(35))
    expect(mockSettings().filament_default_cost_per_kg).toBe('24.50')
  })

  it('saves the 3DFP fetch nozzles with Save Filament, and shows them again on the next visit (#6625)', async () => {
    const first = renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const nozzles = () => within(region('Filament')).getByRole('group', { name: 'Nozzles to fetch from 3DFP' })
    for (const size of ['0.2', '0.4', '0.6', '0.8']) expect(within(nozzles()).getByRole('checkbox', { name: `${size} mm` })).toBeChecked()
    await first.user.click(within(nozzles()).getByRole('checkbox', { name: '0.2 mm' }))
    await first.user.click(within(nozzles()).getByRole('checkbox', { name: '0.8 mm' }))
    // Nothing moves until the section is saved.
    expect(mockSettings().profile_fetch_nozzles).toEqual(['0.2', '0.4', '0.6', '0.8'])
    await first.user.click(within(region('Filament')).getByRole('button', { name: 'Save Filament' }))
    await waitFor(() => expect(mockSettings().profile_fetch_nozzles).toEqual(['0.4', '0.6']))
    first.unmount()

    renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    expect(within(nozzles()).getByRole('checkbox', { name: '0.2 mm' })).not.toBeChecked()
    expect(within(nozzles()).getByRole('checkbox', { name: '0.4 mm' })).toBeChecked()
    expect(within(nozzles()).getByRole('checkbox', { name: '0.6 mm' })).toBeChecked()
    expect(within(nozzles()).getByRole('checkbox', { name: '0.8 mm' })).not.toBeChecked()
  })

  it('does not save the 3DFP fetch nozzles with none ticked', async () => {
    const { user } = renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const nozzles = within(region('Filament')).getByRole('group', { name: 'Nozzles to fetch from 3DFP' })
    for (const size of ['0.2', '0.4', '0.6', '0.8']) await user.click(within(nozzles).getByRole('checkbox', { name: `${size} mm` }))
    await user.click(within(region('Filament')).getByRole('button', { name: 'Save Filament' }))
    expect(await within(region('Filament')).findByText('Choose at least one nozzle.')).toBeInTheDocument()
    expect(mockSettings().profile_fetch_nozzles).toEqual(['0.2', '0.4', '0.6', '0.8'])
  })

  it('has no section for the spool inventory or ScadBuddy\'s spools', () => {
    const titles = SECTION_REGISTRY.map((section) => section.title)
    expect(titles).not.toContain('Spool inventory')
    expect(titles).not.toContain("ScadBuddy's spools")
    expect(SECTION_REGISTRY.find((section) => section.id === 'filament')?.area).toBe('printing')
  })
})
