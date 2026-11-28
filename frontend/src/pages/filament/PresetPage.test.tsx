import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { LOST_PRESET, mirrorShows, presetsOff, writesOff } from '../../mocks/features/filament/presets'
import { server } from '../../mocks/server'
import { choose } from '../../test/select'
import { renderPage } from '../../test/utils'
import { PresetPage } from './PresetPage'

const show = (id: number) => renderPage(<PresetPage />, { route: `/filament/presets/${id}`, path: '/filament/presets/:id' })

const problem = (status: number, code: string, detail: string, extra: Record<string, unknown> = {}) =>
  HttpResponse.json({ type: 'about:blank', title: detail, status, detail, code, ...extra }, { status, headers: { 'Content-Type': 'application/problem+json' } })

const detail = (over: Record<string, unknown>) => ({
  id: 3,
  ref: 'sb-3',
  name: 'Stub',
  source: 'manual',
  version: 1,
  type_id: null,
  printer_model: null,
  nozzle_diameter: null,
  filament_type: null,
  filament_colour: null,
  compatible_printers: [],
  sha256: 'a',
  bb_preset_id: null,
  bb_instance: null,
  mirror: 'sent',
  mirror_error: null,
  mirror_error_at: null,
  used_by_types: 0,
  used_by_spools: 0,
  created_at: '2026-11-26T10:00:00Z',
  updated_at: '2026-11-26T10:00:00Z',
  versions: [{ version: 1, sha256: 'a', made_by: null, made_at: '2026-11-26T10:00:00Z', fetch_run_id: null, bb_updated_at: null }],
  document: {},
  document_error: null,
  ...over,
})

describe('the preset page', () => {
  it('shows the versions newest first and the keys that changed from the previous version', async () => {
    show(3)
    expect(await screen.findByRole('heading', { level: 1, name: 'My PLA, slower' })).toBeInTheDocument()
    const versions = Array.from((await screen.findByRole('list', { name: 'Versions' })).children) as HTMLElement[]
    expect(versions).toHaveLength(2)
    expect(within(versions[0]!).getByText('v2')).toBeInTheDocument()
    expect(within(versions[1]!).getByText('v1')).toBeInTheDocument()
    expect(await within(versions[0]!).findByText('nozzle_temperature')).toBeInTheDocument()
    expect(within(versions[0]!).getByText('filament_max_volumetric_speed')).toBeInTheDocument()
    expect(within(versions[1]!).getByText('First version.')).toBeInTheDocument()
  })

  it('compares any two versions the person picks', async () => {
    const { user } = show(3)
    const compare = await screen.findByRole('region', { name: 'Compare versions' })
    await choose(user, within(compare).getByRole('combobox', { name: 'From' }), 'v2')
    await choose(user, within(compare).getByRole('combobox', { name: 'To' }), 'v1')
    const table = await within(compare).findByRole('table', { name: 'Changed keys, v2 to v1' })
    const row = within(table).getByText('nozzle_temperature').closest('tr')!
    expect(within(row).getByText('220')).toBeInTheDocument()
    expect(within(row).getByText('215')).toBeInTheDocument()
  })

  it("shows the current document's keys with a filter box", async () => {
    const { user } = show(3)
    const doc = await screen.findByRole('region', { name: 'Document' })
    expect(await within(doc).findByText('nozzle_temperature')).toBeInTheDocument()
    expect(within(doc).getByText('filament_max_volumetric_speed')).toBeInTheDocument()
    const box = within(doc).getByRole('searchbox', { name: 'Filter keys' })
    await user.type(box, 'volumetric')
    expect(within(doc).queryByText('nozzle_temperature')).not.toBeInTheDocument()
    expect(within(doc).getByText('filament_max_volumetric_speed')).toBeInTheDocument()
    await user.clear(box)
    await user.type(box, 'zzz')
    expect(within(doc).getByText('No key matches “zzz”.')).toBeInTheDocument()
  })

  it('shows a list value as one line for each item', async () => {
    server.use(http.get('/api/v1/filament/presets/3', () => HttpResponse.json(detail({ document: { compatible_printers: ['H2C 0.4', 'H2C 0.6'] } }))))
    show(3)
    const doc = await screen.findByRole('region', { name: 'Document' })
    expect(await within(doc).findByText('H2C 0.4')).toBeInTheDocument()
    expect(within(doc).getByText('H2C 0.6')).toBeInTheDocument()
  })

  it("shows a message when a version's document is missing", async () => {
    show(LOST_PRESET)
    const compare = await screen.findByRole('region', { name: 'Compare versions' })
    await waitFor(() => expect(within(compare).getByRole('combobox', { name: 'To' })).toHaveTextContent('v2'))
    expect(await within(compare).findByText('The document of one of these versions is not in the store.')).toBeInTheDocument()
    expect(screen.getByRole('list', { name: 'Versions' })).toBeInTheDocument()
  })

  it('says why the document is not shown when the store is not set up', async () => {
    server.use(http.get('/api/v1/filament/presets/3', () => HttpResponse.json(detail({ document: null, document_error: 'store_not_configured' }))))
    show(3)
    expect(await screen.findByText('The blob store is not set up, so the document cannot be read.')).toBeInTheDocument()
    expect(screen.getByRole('list', { name: 'Versions' })).toBeInTheDocument()
  })

  it('shows a message in the compare area when a diff answers 503', async () => {
    server.use(http.get('/api/v1/filament/presets/3/diff', () => problem(503, 'store_unavailable', 'down')))
    show(3)
    const compare = await screen.findByRole('region', { name: 'Compare versions' })
    expect(await within(compare).findByText('The blob store did not answer. Try again.')).toBeInTheDocument()
  })

  it('asks again when the same pair is picked after a failure', async () => {
    let calls = 0
    server.use(
      http.get('/api/v1/filament/presets/3/diff', () => {
        calls += 1
        return calls === 1 ? problem(503, 'store_unavailable', 'down') : HttpResponse.json({ preset_id: 3, from_version: 1, to_version: 2, changes: [] })
      }),
    )
    const { user } = show(3)
    const compare = await screen.findByRole('region', { name: 'Compare versions' })
    expect(await within(compare).findByText('The blob store did not answer. Try again.')).toBeInTheDocument()
    expect(calls).toBe(1)
    await choose(user, within(compare).getByRole('combobox', { name: 'From' }), 'v2')
    await choose(user, within(compare).getByRole('combobox', { name: 'From' }), 'v1')
    await waitFor(() => expect(calls).toBe(2))
    expect(await within(compare).findByText('No key changed.')).toBeInTheDocument()
  })

  it('asks again when Try again is clicked after a failure', async () => {
    let calls = 0
    server.use(
      http.get('/api/v1/filament/presets/3/diff', () => {
        calls += 1
        return calls === 1 ? problem(503, 'store_unavailable', 'down') : HttpResponse.json({ preset_id: 3, from_version: 1, to_version: 2, changes: [{ key: 'nozzle_temperature', before: '200', after: '210' }] })
      }),
    )
    const { user } = show(3)
    const compare = await screen.findByRole('region', { name: 'Compare versions' })
    expect(await within(compare).findByText('The blob store did not answer. Try again.')).toBeInTheDocument()
    expect(calls).toBe(1)
    await user.click(within(compare).getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(calls).toBe(2))
    expect(await within(compare).findByRole('table', { name: 'Changed keys, v1 to v2' })).toBeInTheDocument()
    expect(within(compare).queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument()
  })

  it('says presets are not on when the area is off', async () => {
    presetsOff()
    show(3)
    expect(await screen.findByText('Presets are not on yet on this server.')).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 1, name: 'Presets' })).toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Versions' })).not.toBeInTheDocument()
  })

  it('says there is no such preset on a 404 with a code', async () => {
    show(999)
    expect(await screen.findByText('There is no such preset.')).toBeInTheDocument()
  })

  const versionsOf = (numbers: number[]) => numbers.map((version) => ({ version, sha256: `a${version}`, made_by: null, made_at: '2026-11-26T10:00:00Z', fetch_run_id: null, bb_updated_at: null }))

  it('asks for the changed keys of the newest 10 versions until Show more is clicked', async () => {
    const numbers = Array.from({ length: 12 }, (_, at) => 12 - at)
    const asked: string[] = []
    server.use(
      http.get('/api/v1/filament/presets/3', () => HttpResponse.json(detail({ version: 12, versions: versionsOf(numbers), document: {} }))),
      http.get('/api/v1/filament/presets/3/diff', ({ request }) => {
        const url = new URL(request.url)
        asked.push(`${url.searchParams.get('from_version')}:${url.searchParams.get('to_version')}`)
        return HttpResponse.json({ preset_id: 3, from_version: 0, to_version: 0, changes: [] })
      }),
    )
    const { user } = show(3)
    const list = await screen.findByRole('list', { name: 'Versions' })
    await waitFor(() => expect(within(list).getAllByText('No key changed.').length).toBeGreaterThan(0))
    // 10 versions shown: 10 diffs, and the Compare pair (11:12) is one of them.
    expect(list.children).toHaveLength(10)
    expect(asked.length).toBeLessThanOrEqual(10)
    expect(asked).not.toContain('1:2')
    await user.click(screen.getByRole('button', { name: 'Show more' }))
    await waitFor(() => expect(list.children).toHaveLength(12))
    await waitFor(() => expect(asked).toContain('1:2'))
    expect(asked.length).toBeLessThanOrEqual(12)
    expect(screen.queryByRole('button', { name: 'Show more' })).not.toBeInTheDocument()
  })

  it('asks for the pair of the newest two versions once', async () => {
    const asked: string[] = []
    server.use(
      http.get('/api/v1/filament/presets/3/diff', ({ request }) => {
        const url = new URL(request.url)
        asked.push(`${url.searchParams.get('from_version')}:${url.searchParams.get('to_version')}`)
        return HttpResponse.json({ preset_id: 3, from_version: 1, to_version: 2, changes: [] })
      }),
    )
    show(3)
    const compare = await screen.findByRole('region', { name: 'Compare versions' })
    await within(compare).findByText('No key changed.')
    expect(asked.filter((pair) => pair === '1:2')).toHaveLength(1)
  })

  it('starts the compare from the newest version before the current one when numbers have a gap', async () => {
    const asked: string[] = []
    server.use(
      http.get('/api/v1/filament/presets/3', () => HttpResponse.json(detail({ version: 3, versions: versionsOf([3, 1]), document: {} }))),
      http.get('/api/v1/filament/presets/3/diff', ({ request }) => {
        const url = new URL(request.url)
        asked.push(`${url.searchParams.get('from_version')}:${url.searchParams.get('to_version')}`)
        return HttpResponse.json({ preset_id: 3, from_version: 1, to_version: 3, changes: [] })
      }),
    )
    show(3)
    const compare = await screen.findByRole('region', { name: 'Compare versions' })
    expect(within(compare).getByRole('combobox', { name: 'From' })).toHaveTextContent('v1')
    await waitFor(() => expect(asked).toEqual(['1:3']))
  })

  it('uses the singular for one type and one spool', async () => {
    server.use(http.get('/api/v1/filament/presets/3', () => HttpResponse.json(detail({ used_by_types: 1, used_by_spools: 1 }))))
    show(3)
    expect(await screen.findByText('Used by 1 type, 1 spool')).toBeInTheDocument()
  })

  describe('writes', () => {
    it('sends a failed preset again and shows the new state', async () => {
      const { user } = show(3)
      expect(await screen.findByText('Send to Bambuddy failed')).toBeInTheDocument()
      await user.click(screen.getByRole('button', { name: 'Send again' }))
      expect(await screen.findByText('Sent to Bambuddy.')).toBeInTheDocument()
      expect(screen.getByText('Sent to Bambuddy', { selector: 'span' })).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Send again' })).not.toBeInTheDocument()
    })

    it('says writes are not on yet, with no retry, when the server has no writes', async () => {
      writesOff()
      const { user } = show(3)
      await user.click(await screen.findByRole('button', { name: 'Send again' }))
      expect(await screen.findByText('Writes are not on yet on this server.')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Send again' })).toBeDisabled()
    })

    it('makes a manual copy and opens it', async () => {
      const { user } = show(1)
      await user.click(await screen.findByRole('button', { name: 'Make a copy' }))
      const name = screen.getByRole('textbox', { name: 'Name of the copy' })
      expect(name).toHaveValue('Bambu PLA Basic @H2C 0.4 copy')
      await user.clear(name)
      await user.type(name, 'My own PLA')
      await user.click(screen.getByRole('button', { name: 'Create copy' }))
      expect(await screen.findByRole('heading', { level: 1, name: 'My own PLA' })).toBeInTheDocument()
      expect(screen.getByText('Manual')).toBeInTheDocument()
    })

    it('offers edit only on a manual preset', async () => {
      show(1)
      expect(await screen.findByText('Only a manual preset can be edited. Make a copy first.')).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument()
    })

    it('shows the edit button on a manual preset', async () => {
      show(3)
      expect(await screen.findByRole('button', { name: 'Edit' })).toBeInTheDocument()
      expect(screen.queryByText('Only a manual preset can be edited. Make a copy first.')).not.toBeInTheDocument()
    })

    const patchBody = (answer: () => Response | Promise<Response>) => {
      const seen: { body: unknown; key: string | null }[] = []
      server.use(
        http.patch('/api/v1/filament/presets/3', async ({ request }) => {
          seen.push({ body: await request.json(), key: request.headers.get('Idempotency-Key') })
          return answer()
        }),
      )
      return seen
    }

    const openEdit = async (user: ReturnType<typeof show>['user']) => {
      await user.click(await screen.findByRole('button', { name: 'Edit' }))
      return screen.getByRole('form', { name: 'Edit preset' })
    }

    it('saves an edit as a new version and shows the mirror state', async () => {
      const { user } = show(3)
      const form = await openEdit(user)
      const temp = within(form).getByRole('textbox', { name: 'Value of nozzle_temperature' })
      await user.clear(temp)
      await user.type(temp, '225')
      await user.click(within(form).getByRole('checkbox', { name: 'Remove filament_max_volumetric_speed' }))
      await user.click(within(form).getByRole('button', { name: 'Save edit' }))
      expect(await screen.findByText('Saved as v3. Sent to Bambuddy.')).toBeInTheDocument()
      expect(screen.queryByRole('form', { name: 'Edit preset' })).not.toBeInTheDocument()
      const doc = screen.getByRole('region', { name: 'Document' })
      expect(within(doc).getByText('225')).toBeInTheDocument()
      expect(within(doc).queryByText('filament_max_volumetric_speed')).not.toBeInTheDocument()
    })

    it('sends expected_version, set and unset', async () => {
      const seen = patchBody(() => HttpResponse.json({ changed: true, preset: detail({ id: 3, version: 3 }) }))
      const { user } = show(3)
      const form = await openEdit(user)
      const temp = within(form).getByRole('textbox', { name: 'Value of nozzle_temperature' })
      await user.clear(temp)
      await user.type(temp, '225')
      await user.click(within(form).getByRole('checkbox', { name: 'Remove filament_max_volumetric_speed' }))
      await user.type(within(form).getByRole('textbox', { name: 'New key' }), 'cool_plate_temp')
      await user.type(within(form).getByRole('textbox', { name: 'New value' }), '35')
      await user.click(within(form).getByRole('button', { name: 'Save edit' }))
      await waitFor(() => expect(seen).toHaveLength(1))
      expect(seen[0]!.body).toEqual({ expected_version: 2, set: { nozzle_temperature: ['225'], cool_plate_temp: '35' }, unset: ['filament_max_volumetric_speed'] })
      expect(seen[0]!.key).toBeTruthy()
    })

    it('says nothing changed when the server made no version', async () => {
      patchBody(() => HttpResponse.json({ changed: false, preset: detail({ id: 3, version: 2 }) }))
      const { user } = show(3)
      const form = await openEdit(user)
      await user.clear(within(form).getByRole('textbox', { name: 'Name' }))
      await user.type(within(form).getByRole('textbox', { name: 'Name' }), 'Renamed')
      await user.click(within(form).getByRole('button', { name: 'Save edit' }))
      expect(await screen.findByText('Nothing changed. No new version was made.')).toBeInTheDocument()
    })

    it('says another edit came first and reloads', async () => {
      let reads = 0
      server.use(
        http.get('/api/v1/filament/presets/3', () => {
          reads += 1
          return HttpResponse.json(detail({ name: reads > 1 ? 'Edited elsewhere' : 'My PLA, slower', version: reads > 1 ? 3 : 2, versions: versionsOf(reads > 1 ? [3, 2, 1] : [2, 1]) }))
        }),
      )
      patchBody(() => problem(409, 'version_moved', 'another edit came first'))
      const { user } = show(3)
      const form = await openEdit(user)
      await user.type(within(form).getByRole('textbox', { name: 'New key' }), 'k')
      await user.type(within(form).getByRole('textbox', { name: 'New value' }), 'v')
      await user.click(within(form).getByRole('button', { name: 'Save edit' }))
      expect(await screen.findByText('Another edit came first. The preset is loaded again. Make your change again.')).toBeInTheDocument()
      expect(await screen.findByRole('heading', { level: 1, name: 'Edited elsewhere' })).toBeInTheDocument()
      expect(reads).toBe(2)
      expect(screen.queryByRole('form', { name: 'Edit preset' })).not.toBeInTheDocument()
    })

    it('sends the same key again after a 503 preset_write_pending', async () => {
      let calls = 0
      const seen = patchBody(() => {
        calls += 1
        return calls === 1 ? problem(503, 'preset_write_pending', 'not done yet') : HttpResponse.json({ changed: true, preset: detail({ id: 3, version: 3 }) })
      })
      const { user } = show(3)
      const form = await openEdit(user)
      await user.type(within(form).getByRole('textbox', { name: 'New key' }), 'k')
      await user.type(within(form).getByRole('textbox', { name: 'New value' }), 'v')
      await user.click(within(form).getByRole('button', { name: 'Save edit' }))
      expect(await within(form).findByText('The write has not finished yet. Try again. It will not be done twice.')).toBeInTheDocument()
      await user.click(within(form).getByRole('button', { name: 'Save edit' }))
      await waitFor(() => expect(seen).toHaveLength(2))
      expect(seen[1]!.key).toBe(seen[0]!.key)
      expect(seen[0]!.key).toBeTruthy()
      expect(await screen.findByText(/^Saved as v3/)).toBeInTheDocument()
    })

    it('sends a new key when the form is opened again', async () => {
      const seen = patchBody(() => problem(422, 'key_refused', 'the key bad cannot be set or unset', { key: 'bad' }))
      const { user } = show(3)
      for (let round = 0; round < 2; round += 1) {
        const form = await openEdit(user)
        await user.type(within(form).getByRole('textbox', { name: 'New key' }), 'bad')
        await user.type(within(form).getByRole('textbox', { name: 'New value' }), 'v')
        await user.click(within(form).getByRole('button', { name: 'Save edit' }))
        expect(await within(form).findByText('The key bad is not allowed in this edit. Change it or take it out.')).toBeInTheDocument()
        await user.click(within(form).getByRole('button', { name: 'Cancel' }))
      }
      expect(seen[0]!.key).not.toBe(seen[1]!.key)
    })

    it('says in its own words that the server did not accept the edit', async () => {
      patchBody(() => problem(422, 'invalid_input', 'an edit needs a name, a key to set or a key to unset'))
      const { user } = show(3)
      const form = await openEdit(user)
      await user.type(within(form).getByRole('textbox', { name: 'New key' }), 'k')
      await user.type(within(form).getByRole('textbox', { name: 'New value' }), 'v')
      await user.click(within(form).getByRole('button', { name: 'Save edit' }))
      expect(await within(form).findByText('The change is not valid: an edit needs a name, a key to set or a key to unset.')).toBeInTheDocument()
    })

    it('says why Edit is off when the preset has no document', async () => {
      server.use(http.get('/api/v1/filament/presets/3', () => HttpResponse.json(detail({ version: 2, document: null, document_error: 'document_missing', versions: versionsOf([2, 1]) }))))
      show(3)
      expect(await screen.findByRole('button', { name: 'Edit' })).toBeDisabled()
      expect(screen.getByText('The document is not in the store, so this preset cannot be edited.')).toBeInTheDocument()
    })

    it('refuses an empty list item in the form, and sends nothing', async () => {
      const seen = patchBody(() => HttpResponse.json({ changed: true, preset: detail({ id: 3, version: 3 }) }))
      const { user } = show(3)
      const form = await openEdit(user)
      const temp = within(form).getByRole('textbox', { name: 'Value of nozzle_temperature' })
      await user.clear(temp)
      await user.type(temp, '225{Enter}{Enter}230')
      expect(within(form).getByText('nozzle_temperature: a list item is empty. Fill it in or delete the line.')).toBeInTheDocument()
      expect(within(form).getByRole('button', { name: 'Save edit' })).toBeDisabled()
      await user.clear(temp)
      await user.type(temp, '225{Enter}230')
      expect(within(form).queryByText(/a list item is empty/)).not.toBeInTheDocument()
      await user.click(within(form).getByRole('button', { name: 'Save edit' }))
      await waitFor(() => expect(seen).toHaveLength(1))
      expect(seen[0]!.body).toEqual({ expected_version: 2, set: { nozzle_temperature: ['225', '230'] } })
    })

    it('sends an empty list when the whole list text is cleared', async () => {
      const seen = patchBody(() => HttpResponse.json({ changed: true, preset: detail({ id: 3, version: 3 }) }))
      const { user } = show(3)
      const form = await openEdit(user)
      await user.clear(within(form).getByRole('textbox', { name: 'Value of nozzle_temperature' }))
      expect(within(form).queryByText(/a list item is empty/)).not.toBeInTheDocument()
      await user.click(within(form).getByRole('button', { name: 'Save edit' }))
      await waitFor(() => expect(seen).toHaveLength(1))
      expect(seen[0]!.body).toEqual({ expected_version: 2, set: { nozzle_temperature: [] } })
    })

    it('refuses an empty line in a new list value too', async () => {
      const { user } = show(3)
      const form = await openEdit(user)
      await user.type(within(form).getByRole('textbox', { name: 'New key' }), 'k')
      await user.type(within(form).getByRole('textbox', { name: 'New value' }), 'a{Enter}')
      expect(within(form).getByText('k: a list item is empty. Fill it in or delete the line.')).toBeInTheDocument()
      expect(within(form).getByRole('button', { name: 'Save edit' })).toBeDisabled()
    })

    it.each([
      ['mirror_off', 'The send to Bambuddy is off in Settings.'],
      ['not_configured', 'Bambuddy is not set up in Settings.'],
      ['instance_changed', 'Bambuddy changed. Reset the Bambuddy links in Settings first.'],
    ] as const)('says %s in its own words on a send again', async (code, text) => {
      mirrorShows(code)
      const { user } = show(3)
      await user.click(await screen.findByRole('button', { name: 'Send again' }))
      expect(await screen.findByText(text)).toBeInTheDocument()
    })

    it.each([
      ['busy', 'A send is running now. Wait a moment, then send again.'],
      ['pending', 'The send is queued. Bambuddy has not got this version yet.'],
      ['failed', 'The send failed. See the reason with the preset.'],
    ] as const)('says the outcome %s of a send again', async (outcome, text) => {
      mirrorShows(outcome)
      const { user } = show(3)
      await user.click(await screen.findByRole('button', { name: 'Send again' }))
      expect(await screen.findByText(text)).toBeInTheDocument()
    })
  })
})
