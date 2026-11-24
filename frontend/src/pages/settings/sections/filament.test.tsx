import { act, screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { api } from '../../../api/client'
import { getResetPreview, getSyncStatus, listSyncPasses, resetSyncLinks } from '../../../api/filament'
import { describe, expect, it, vi } from 'vitest'
import { spoolId } from '../../../mocks/features/filament/seed'
import { leaveOnlyColourLinks, linkOtherBambuddy, loseSameBambuddy } from '../../../mocks/features/filament/sync'
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

  it('saves the tag format for new tags', async () => {
    const { user } = renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const filament = region('Filament')
    const format = within(filament).getByRole('combobox', { name: 'Tag format for new tags' })
    expect(format).toHaveTextContent('OpenSpool')
    await choose(user, format, 'OpenTag3D')
    await user.click(within(filament).getByRole('button', { name: 'Save Filament' }))
    await waitFor(() => expect(mockSettings().filament_tag_format).toBe('opentag3d'))
  })

  it('refuses a tag format that is neither OpenSpool nor OpenTag3D, as the API does', async () => {
    await expect(api.putSettings({ filament_tag_format: 'tigertag' as 'openspool' })).rejects.toMatchObject({ status: 422 })
    expect(mockSettings().filament_tag_format).toBe('openspool')
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

  it('names an item that is gone by its id, as deleted and with no link (#6869)', async () => {
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
    expect(within(failed).queryByRole('link')).toBeNull()
    expect(within(failed).getByRole('listitem')).toHaveTextContent('Location 0badc0de (deleted)')
  })

  it('links a failed place to the spools in it (#6869)', async () => {
    server.use(
      http.get('*/api/v1/filament/sync/status', () =>
        HttpResponse.json({
          instance: null,
          last_pass_at: null,
          last_pass_started_at: null,
          last_ok_at: null,
          last_error: null,
          counts: {},
          errors: [{ kind: 'location', sb_id: '0badc0de-0000-4000-8000-000000000002', label: 'Dry box', error: 'internal', attempts: 1, at: '2026-11-15T07:00:00Z' }],
          directions: DEFAULT_SYNC,
          skipped: null,
        }),
      ),
    )
    renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const failed = await within(region('Filament')).findByRole('list', { name: 'Items not synced' })
    expect(within(failed).getByRole('link', { name: 'Location Dry box' })).toHaveAttribute('href', expect.stringContaining('/filament/locations/0badc0de-0000-4000-8000-000000000002'))
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
          '5 colour links',
          '1 unfinished create',
          '2 listed sync errors',
        ])
        await user.click(within(dialog).getByRole('button', { name: 'Reset links' }))
        await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
        expect(sent).toEqual({ confirm_instance: '0ld0ld0ld0ld', expect_kept: false })
        expect(await within(filament).findByText('27 links reset. A full pass now links the new Bambuddy.')).toBeInTheDocument()
        expect(within(filament).queryByRole('button', { name: 'Reset Bambuddy links' })).toBeNull()
        expect(within(filament).queryByRole('list', { name: 'Items not synced' })).toBeNull()
      } finally {
        server.events.removeAllListeners()
      }
    })

    it('names the colour links when they are the only links left (#6907)', async () => {
      linkOtherBambuddy()
      leaveOnlyColourLinks()
      const { user, filament } = await withSync()
      await user.click(await within(filament).findByRole('button', { name: 'Reset Bambuddy links' }))
      const dialog = await screen.findByRole('alertdialog', { name: 'Reset the Bambuddy links?' })
      const items = within(within(dialog).getByRole('list', { name: 'What the reset deletes' })).getAllByRole('listitem')
      expect(items.map((item) => item.textContent)).toEqual(['5 colour links'])
      await user.click(within(dialog).getByRole('button', { name: 'Reset links' }))
      expect(await within(filament).findByText('5 links reset. A full pass now links the new Bambuddy.')).toBeInTheDocument()
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

    it('reads the dry run again when the Bambuddy changed before "Keep links" (#6845 R-6845-6)', async () => {
      linkOtherBambuddy('0ld0ld0ld0ld', { same: true })
      const { user, filament } = await withSync()
      await user.click(await within(filament).findByRole('button', { name: 'Reset Bambuddy links' }))
      const dialog = await screen.findByRole('alertdialog', { name: 'Keep the Bambuddy links?' })
      loseSameBambuddy()
      await user.click(within(dialog).getByRole('button', { name: 'Keep links' }))
      expect(await within(dialog).findByRole('alert')).toHaveTextContent(
        'The Bambuddy set up now is no longer proven to be the same one. Nothing was deleted.',
      )
      expect(await screen.findByRole('alertdialog', { name: 'Reset the Bambuddy links?' })).toBeInTheDocument()
      expect(within(dialog).getByRole('list', { name: 'What the reset deletes' })).toBeInTheDocument()
      expect((await getSyncStatus()).instance_changed).toBe(true)
    })

    it('offers the delete when the Bambuddy set up now cannot be read (#6845 R-6845-6)', async () => {
      linkOtherBambuddy('0ld0ld0ld0ld', { unreadable: true })
      const { user, filament } = await withSync()
      await user.click(await within(filament).findByRole('button', { name: 'Reset Bambuddy links' }))
      const dialog = await screen.findByRole('alertdialog', { name: 'Reset the Bambuddy links?' })
      expect(dialog).toHaveTextContent('The Bambuddy set up now could not be read')
      await user.click(within(dialog).getByRole('button', { name: 'Reset links' }))
      await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
      expect((await getSyncStatus()).instance_changed).toBe(false)
    })

    it('says why there is no keep when every comparable linked spool changed since the last pull (#6845)', async () => {
      linkOtherBambuddy('0ld0ld0ld0ld', { changed: true })
      const { user, filament } = await withSync()
      await user.click(await within(filament).findByRole('button', { name: 'Reset Bambuddy links' }))
      const dialog = await screen.findByRole('alertdialog', { name: 'Reset the Bambuddy links?' })
      expect(dialog).toHaveTextContent(
        'Every linked spool ScadBuddy could compare (a time on both sides) changed in Bambuddy since the last pull, so ScadBuddy cannot prove this is the same Bambuddy.',
      )
      expect(dialog).not.toHaveTextContent('could not be read')
      expect(within(dialog).queryByRole('button', { name: 'Keep links' })).toBeNull()
      expect(within(dialog).getByRole('button', { name: 'Reset links' })).toBeInTheDocument()
    })

    it('does not give that reason for a Bambuddy that proves nothing else (#6845)', async () => {
      linkOtherBambuddy('0ld0ld0ld0ld')
      const { user, filament } = await withSync()
      await user.click(await within(filament).findByRole('button', { name: 'Reset Bambuddy links' }))
      const dialog = await screen.findByRole('alertdialog', { name: 'Reset the Bambuddy links?' })
      expect(dialog).not.toHaveTextContent('changed in Bambuddy since the last pull')
    })

    it('answers a kept reset with the instance the links belong to now (#6845)', async () => {
      linkOtherBambuddy('0ld0ld0ld0ld', { same: true })
      const preview = await getResetPreview()
      expect(preview.changed_since_pull).toBe(false)
      const done = await resetSyncLinks('0ld0ld0ld0ld', true)
      expect(done.kept).toBe(true)
      expect(done.configured_instance).toBe(preview.configured_instance)
      expect(done.configured_instance).not.toBeNull()
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

  it('saves the label address and refuses a path (#6955)', async () => {
    const { user } = renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const address = () => within(region('Filament')).getByLabelText('Label address')
    await enter(user, address(), 'https://sb.example.com/x')
    await user.click(within(region('Filament')).getByRole('button', { name: 'Save Filament' }))
    await within(region('Filament')).findByRole('alert')
    expect(mockSettings().filament_label_base_url).toBeNull()

    await user.clear(address())
    await enter(user, address(), 'https://sb.example.com')
    await user.click(within(region('Filament')).getByRole('button', { name: 'Save Filament' }))
    await waitFor(() => expect(mockSettings().filament_label_base_url).toBe('https://sb.example.com'))
  })

  it('saves an empty label address as unset (#6955)', async () => {
    await api.putSettings({ filament_label_base_url: 'https://sb.example.com' })
    const { user } = renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const address = within(region('Filament')).getByLabelText('Label address')
    await waitFor(() => expect(address).toHaveValue('https://sb.example.com'))
    await user.clear(address)
    await user.click(within(region('Filament')).getByRole('button', { name: 'Save Filament' }))
    await waitFor(() => expect(mockSettings().filament_label_base_url).toBeNull())
  })

  it('switches off adding empty spools to the shopping list, with Save Filament (#6868)', async () => {
    const { user } = renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const filament = region('Filament')
    const toggle = within(filament).getByRole('switch', { name: /Add a spool's type when it runs out/ })
    expect(toggle).toBeChecked()
    expect(within(filament).getByText('Add empty spools to the shopping list')).toBeInTheDocument()
    await user.click(toggle)
    await user.click(within(filament).getByRole('button', { name: 'Save Filament' }))
    await waitFor(() => expect(mockSettings().filament_shopping_auto_add_empty).toBe(false))
    expect(within(filament).getByRole('switch', { name: /Add a spool's type when it runs out/ })).not.toBeChecked()
  })

  it('switches off spools for unknown RFID tags with Save Filament, and says the setting holds Bambuddy off (#7156)', async () => {
    const { user } = renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const filament = region('Filament')
    const toggle = within(filament).getByRole('switch', { name: /Let Bambuddy add spools for unknown tags/ })
    expect(toggle).toBeChecked()
    expect(within(filament).getByText('Add spools for unknown RFID tags')).toBeInTheDocument()
    // The status line changes only once the save's own read of the RFID status has answered.
    let statusReads = 0
    const onAnswer = ({ request }: { request: Request }) => {
      if (request.method === 'GET' && new URL(request.url).pathname === '/api/v1/filament/rfid/status') statusReads += 1
    }
    server.events.on('response:mocked', onAnswer)
    try {
      await user.click(toggle)
      const before = statusReads
      await user.click(within(filament).getByRole('button', { name: 'Save Filament' }))
      await waitFor(() => expect(mockSettings().filament_auto_add_unknown_rfid).toBe(false))
      expect(within(filament).getByRole('switch', { name: /Let Bambuddy add spools for unknown tags/ })).not.toBeChecked()
      await waitFor(() => expect(statusReads).toBeGreaterThan(before))
    } finally {
      server.events.removeListener('response:mocked', onAnswer)
    }
    // The override mode stays off; the status says what holds Bambuddy's auto-add.
    expect(within(filament).getByRole('switch', { name: 'RFID override mode' })).not.toBeChecked()
    expect(
      await within(filament).findByText('Bambuddy no longer adds unknown RFID spools, because "Add spools for unknown RFID tags" is off'),
    ).toBeInTheDocument()
  })

  it('saves the drying defaults, refuses a temperature out of range, and empty offers none (#7156)', async () => {
    const { user } = renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const temp = () => within(region('Filament')).getByLabelText('Default drying temperature, °C')
    const hours = () => within(region('Filament')).getByLabelText('Default drying time, hours')
    expect(temp()).toHaveValue('')
    expect(hours()).toHaveValue('')
    await enter(user, temp(), '200')
    await user.click(within(region('Filament')).getByRole('button', { name: 'Save Filament' }))
    expect(await within(region('Filament')).findByText('Enter 1 to 150 °C, or leave it empty to offer none.')).toBeInTheDocument()
    expect(mockSettings().filament_drying_temp_c).toBeNull()

    await user.clear(temp())
    await enter(user, temp(), '55')
    await enter(user, hours(), '8.5')
    await user.click(within(region('Filament')).getByRole('button', { name: 'Save Filament' }))
    await waitFor(() => expect(mockSettings().filament_drying_temp_c).toBe(55))
    expect(mockSettings().filament_drying_hours).toBe('8.5')
    // Cleared before the save's answer reaches the form, the fields equal the old stored values
    // (empty), so the next Save would send nothing.
    expect(await within(region('Filament')).findByText(/^Saved at /)).toBeInTheDocument()

    await user.clear(temp())
    await user.clear(hours())
    await user.click(within(region('Filament')).getByRole('button', { name: 'Save Filament' }))
    await waitFor(() => expect(mockSettings().filament_drying_temp_c).toBeNull())
    expect(mockSettings().filament_drying_hours).toBeNull()
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

describe('the sync pass history (#6905)', () => {
  const passes = () => within(region('Filament')).findByRole('list', { name: 'Recent sync passes' })

  it('lists recent passes newest first, a failed one with its error, and pages older ones', async () => {
    const { user } = renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const list = await passes()
    const rows = within(list).getAllByRole('listitem')
    expect(rows).toHaveLength(10)
    expect(rows[0]).toHaveTextContent(/full pass, took 4 s/)
    expect(rows[0]).toHaveTextContent('4 written, 2 failed.')
    expect(rows[0]).toHaveAttribute('data-outcome', 'ok')
    expect(rows[2]).toHaveAttribute('data-outcome', 'failed')
    expect(rows[2]).toHaveTextContent('after a change')
    expect(rows[2]).toHaveTextContent("Failed: ScadBuddy's own API did not answer in time.")
    expect(rows[2]).toHaveTextContent('Nothing written.')
    expect(rows[3]).toHaveAttribute('data-outcome', 'idle')
    expect(rows[3]).toHaveTextContent('Skipped: nothing was set to go to Bambuddy.')
    expect(within(region('Filament')).getByText('Every pass, failed ones too, kept for 30 days.')).toBeInTheDocument()

    await user.click(within(region('Filament')).getByRole('button', { name: 'Show older passes' }))
    await waitFor(() => expect(within(list).getAllByRole('listitem')).toHaveLength(14))
    expect(within(region('Filament')).queryByRole('button', { name: 'Show older passes' })).not.toBeInTheDocument()
  })

  it('says when no pass is recorded', async () => {
    server.use(http.get('/api/v1/filament/sync/passes', () => HttpResponse.json({ passes: [], next_cursor: null })))
    renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    expect(await within(region('Filament')).findByText('No pass recorded yet.')).toBeInTheDocument()
  })

  it('says the history could not be read', async () => {
    server.use(http.get('/api/v1/filament/sync/passes', () => HttpResponse.json({ detail: 'down' }, { status: 503 })))
    renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    expect(await within(region('Filament')).findByText(/The recent passes could not be read/)).toBeInTheDocument()
  })

  it('reads the history again when Sync now lands a pass', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      await api.putSettings({ filament_sync: { spools: 'bb_to_sb', weights: 'two_way', tags: 'bb_to_sb', presets: 'bb_to_sb', suppliers: 'off', locations: 'off' } })
      const { user } = renderSettingsSection(<Filament />, { route: '/settings/printing' })
      await seeded()
      expect(within(await passes()).getAllByRole('listitem')[0]).toHaveTextContent('4 written, 2 failed.')
      await user.click(within(region('Filament')).getByRole('button', { name: 'Sync now' }))
      // The mock's pass lands on the second status read after the run.
      await act(() => vi.advanceTimersByTimeAsync(6_000))
      await waitFor(() => expect(within(region('Filament')).getByText('Sync finished.')).toBeInTheDocument())
      await waitFor(async () => expect(within(await passes()).getAllByRole('listitem')[0]).toHaveTextContent('4 written, 1 failed.'))
    } finally {
      vi.useRealTimers()
    }
  })

  it('drops an older page asked for before a pass landed, so no row is skipped', async () => {
    // The real cursor is a key, not an index: the page after row 10 stays rows 11 to 14 once a pass lands.
    const stale = await listSyncPasses({ limit: 10, cursor: '10' })
    let release = () => {}
    const held = new Promise<void>((resolve) => {
      release = resolve
    })
    let holding = true
    server.use(
      http.get('/api/v1/filament/sync/passes', async ({ request }) => {
        if (!holding || !new URL(request.url).searchParams.has('cursor')) return undefined
        holding = false
        await held
        return HttpResponse.json(stale)
      }),
    )
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      await api.putSettings({ filament_sync: { spools: 'bb_to_sb', weights: 'two_way', tags: 'bb_to_sb', presets: 'bb_to_sb', suppliers: 'off', locations: 'off' } })
      const { user } = renderSettingsSection(<Filament />, { route: '/settings/printing' })
      await seeded()
      await passes()
      const showOlder = () => within(region('Filament')).getByRole('button', { name: 'Show older passes' })
      await user.click(showOlder())
      expect(showOlder()).toBeDisabled()
      await user.click(within(region('Filament')).getByRole('button', { name: 'Sync now' }))
      await act(() => vi.advanceTimersByTimeAsync(6_000))
      await waitFor(async () => expect(within(await passes()).getAllByRole('listitem')[0]).toHaveTextContent('4 written, 1 failed.'))
      release()
      await waitFor(() => expect(showOlder()).toBeEnabled())
      expect(within(await passes()).getAllByRole('listitem')).toHaveLength(10)
      await user.click(showOlder())
      await waitFor(async () => expect(within(await passes()).getAllByRole('listitem')).toHaveLength(15))
    } finally {
      release()
      vi.useRealTimers()
    }
  })

  it('says the stored days while the field holds a value save would refuse', async () => {
    await api.putSettings({ filament_sync_pass_ttl_days: 7 })
    const { user } = renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const field = within(region('Filament')).getByLabelText('Keep sync history for (days)')
    await user.clear(field)
    await enter(user, field, '0')
    expect(within(region('Filament')).getByText('Every pass, failed ones too, kept for 7 days.')).toBeInTheDocument()
    await user.clear(field)
    expect(within(region('Filament')).getByText('Every pass, failed ones too, kept for 30 days.')).toBeInTheDocument()
  })

  it('saves how long passes are kept, refuses 0, and empty puts back the default', async () => {
    const { user } = renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const field = () => within(region('Filament')).getByLabelText('Keep sync history for (days)')
    expect(field()).toHaveValue(null)
    expect(field()).toHaveAttribute('placeholder', '30')
    await enter(user, field(), '0')
    await user.click(within(region('Filament')).getByRole('button', { name: 'Save Filament' }))
    expect(await within(region('Filament')).findByText('Enter 1 to 3650 days, or leave it empty for 30.')).toBeInTheDocument()
    await user.clear(field())
    await enter(user, field(), '7')
    await user.click(within(region('Filament')).getByRole('button', { name: 'Save Filament' }))
    await waitFor(() => expect(mockSettings().filament_sync_pass_ttl_days).toBe(7))
    expect(within(region('Filament')).getByText('Every pass, failed ones too, kept for 7 days.')).toBeInTheDocument()
    // Cleared before the save's answer reaches the form, the field equals the old stored value
    // (empty), so the next Save would send nothing.
    expect(await within(region('Filament')).findByText(/^Saved at /)).toBeInTheDocument()
    await user.clear(field())
    await user.click(within(region('Filament')).getByRole('button', { name: 'Save Filament' }))
    await waitFor(() => expect(mockSettings().filament_sync_pass_ttl_days).toBeNull())
  })
})
