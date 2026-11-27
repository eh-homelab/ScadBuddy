import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { LOST_PRESET, presetsOff } from '../../mocks/features/filament/presets'
import { server } from '../../mocks/server'
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
    await user.selectOptions(within(compare).getByLabelText('From'), '2')
    await user.selectOptions(within(compare).getByLabelText('To'), '1')
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
    await waitFor(() => expect(within(compare).getByLabelText('To')).toHaveValue('2'))
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

  it('says presets are not on when the area is off', async () => {
    presetsOff()
    show(3)
    expect(await screen.findByText('Presets are not on yet on this server.')).toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Versions' })).not.toBeInTheDocument()
  })

  it('says there is no such preset on a 404 with a code', async () => {
    show(999)
    expect(await screen.findByText('There is no such preset.')).toBeInTheDocument()
  })
})
