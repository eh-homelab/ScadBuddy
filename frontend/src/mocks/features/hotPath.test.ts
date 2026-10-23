import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { api } from '../../api/client'
import type { PrintRunRequest } from '../../api/types'
import { resetMockState } from '../handlers'
import {
  HOT_FOLDERS,
  HOT_PATH_KEY,
  HOT_PATH_STALE_KEY,
  HOT_PRINTER,
  NO_BAMBUDDY,
  TAGGED_TRAY,
  UNLINKED_RECORD,
  UNTAGGED_TRAY,
  setHotPath,
} from './hotPath'

const at = (path: string, init?: RequestInit) => fetch(new URL(`/api/v1${path}`, window.location.href), init)

beforeEach(() => resetMockState())
afterEach(() => localStorage.clear())

describe('hot-path mock (#3818, plan 2026-10-18 Task 11)', () => {
  it('is off unless switched on, and then every route answers as before', async () => {
    expect((await api.getSettings()).bambuddy_url).not.toBeNull()
    expect((await api.listPrinters()).some((row) => row.id === HOT_PRINTER.id)).toBe(false)
  })

  it('reads the switch from localStorage on reset, as a page load does', async () => {
    localStorage.setItem(HOT_PATH_KEY, '1')
    resetMockState()
    expect((await api.getSettings()).bambuddy_url).toBeNull()
  })

  it('answers the settings with no Bambuddy and the native backends, and one LAN-linked H2C', async () => {
    setHotPath(true)
    expect(await api.getSettings()).toMatchObject({
      bambuddy_url: null,
      slicer_backend: 'native',
      library_backend: 'native',
      store_backend: 's3',
    })
    expect(await api.listPrinters()).toEqual([
      expect.objectContaining({ model: 'H2C', preferred_path: 'native', bambuddy_printer_id: null, has_access_code: true }),
    ])
  })

  it('answers the filaments as trays: one with a Bambuddy record, one untagged with a record to link', async () => {
    setHotPath(true)
    const options = await api.getFilaments('out-1', { printer: HOT_PRINTER.id })
    expect(options.source).toBe('trays')
    expect(options.overlay_notes).toEqual([])
    const [tagged, untagged, ...rest] = options.spools ?? []
    expect(tagged).toMatchObject({ spool_id: TAGGED_TRAY, label: 'AMS-A slot 1', records: { bambuddy: { brand: 'Bambu Lab' } } })
    expect(untagged).toMatchObject({ spool_id: UNTAGGED_TRAY, records: {} })
    // The record to link is on the shelf (#4506), never a print choice in `spools`.
    expect(rest).toEqual([])
    expect(options.shelf).toEqual([expect.objectContaining({ spool_id: UNLINKED_RECORD, loaded: null })])
    expect(options.trays).toEqual([expect.objectContaining({ ams_id: 0, tray_id: 1, candidates: [UNLINKED_RECORD] })])
  })

  it('answers a stale overlay with no records and one note', async () => {
    localStorage.setItem(HOT_PATH_KEY, '1')
    localStorage.setItem(HOT_PATH_STALE_KEY, '1')
    resetMockState()
    const options = await api.getFilaments('out-1')
    expect(options.overlay_notes).toHaveLength(1)
    expect(options.overlay_notes?.[0]).toMatch(/Bambuddy/)
    expect((options.spools ?? []).every((spool) => Object.keys(spool.records ?? {}).length === 0)).toBe(true)
    expect(options.trays).toEqual([])
    expect(options.shelf).toEqual([])
  })

  it('links the untagged tray, after which it carries the record', async () => {
    setHotPath(true)
    const linked = await api.linkTraySpool(HOT_PRINTER.id, 0, 1, { provider: 'bambuddy', record_id: '27' })
    expect(linked).toMatchObject({ ams_id: 0, tray_id: 1, linked_by: 'manual', records: { bambuddy: { name: 'Pink' } } })
    const options = await api.getFilaments('out-1')
    expect(options.trays).toEqual([])
    expect(options.spools?.find((spool) => spool.spool_id === UNTAGGED_TRAY)?.records?.['bambuddy']).toBeTruthy()
  })

  it('lists native folders, and refuses Bambuddy projects', async () => {
    setHotPath(true)
    expect(await api.getProjects({ native: true })).toMatchObject({ projects: [], folders: HOT_FOLDERS })
    await expect(api.getProjects()).rejects.toMatchObject({ status: 409, detail: NO_BAMBUDDY })
    expect(await api.getDefaultProject({ outputIds: ['out-1'], native: true })).toEqual({ project_id: null, reason: 'none' })
    await expect(api.getDefaultProject({ outputIds: ['out-1'] })).rejects.toMatchObject({ status: 409 })
  })

  it('answers 409 on every Bambuddy-only route', async () => {
    setHotPath(true)
    for (const [path, method] of [
      ['/print/printers/1/camera', 'GET'],
      ['/print/printers/1/camera/stream', 'GET'],
      ['/farm/inventory', 'GET'],
      ['/print/printers/1/trays/0/1/spool', 'POST'],
      ['/print/projects', 'POST'],
    ] as const) {
      const response = await at(path, { method, body: method === 'POST' ? '{}' : undefined })
      expect(response.status, `${method} ${path}`).toBe(409)
      expect(((await response.json()) as { detail: string }).detail).toBe(NO_BAMBUDDY)
    }
  })

  it('slices natively at 20 %, then 60 %, then done', async () => {
    setHotPath(true)
    const started = await api.previewSlice('out-1', { plate_id: 1 } as PrintRunRequest)
    expect(started.slicer).toBe('native')
    const reads = [
      await api.getPreviewSlice('out-1', started.job_id, 'native'),
      await api.getPreviewSlice('out-1', started.job_id, 'native'),
      await api.getPreviewSlice('out-1', started.job_id, 'native'),
    ]
    expect(reads.map((read) => [read.status, read.progress ?? null])).toEqual([
      ['running', 20],
      ['running', 60],
      ['completed', null],
    ])
  })

  it('answers the run 202, then a result with no Bambuddy link, and the printer follows it', async () => {
    setHotPath(true)
    const accepted = await at('/print/outputs/out-1/run', { method: 'POST', body: JSON.stringify({ copies: 1 }) })
    expect(accepted.status).toBe(202)
    const run = (await accepted.json()) as { id: string; status: string }
    expect(run.status).toBe('running')
    const done = (await (await at(`/print/runs/${run.id}`)).json()) as { status: string; result: { bambuddy_url: unknown } }
    expect(done.status).toBe('succeeded')
    expect(done.result.bambuddy_url).toBeNull()

    const cards = []
    for (let read = 0; read < 3; read += 1) cards.push((await api.getPrintersNow()).printers?.[0])
    expect(cards.map((card) => [card?.path, card?.activity, card?.progress])).toEqual([
      ['native', 'printing', 30],
      ['native', 'printing', 80],
      ['native', 'finished', 100],
    ])
  })
})
