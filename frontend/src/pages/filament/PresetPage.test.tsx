import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { LOST_PRESET, presetsOff } from '../../mocks/features/filament/presets'
import { server } from '../../mocks/server'
import { choose } from '../../test/select'
import { renderPage } from '../../test/utils'
import { PresetPage } from './PresetPage'

const show = (id: number) => renderPage(<PresetPage />, { route: `/filament/presets/${id}`, path: '/filament/presets/:id' })

const problem = (status: number, code: string, detail: string) =>
  HttpResponse.json({ type: 'about:blank', title: detail, status, detail, code }, { status, headers: { 'Content-Type': 'application/problem+json' } })

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
})
