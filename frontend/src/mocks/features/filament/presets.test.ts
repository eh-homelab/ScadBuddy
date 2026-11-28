import { readFileSync } from 'node:fs'
import { beforeEach, describe, expect, it } from 'vitest'
import { ApiError } from '../../../api/client'
import { copyPreset, diffPreset, editPreset, getPreset, listPresets, mirrorPreset, routeMissing } from '../../../api/filamentPresets'
import { resetMockState } from '../../handlers'
import { LOST_PRESET, presetsOff, writesOff } from './presets'

// #7204 — the preset mocks answer as `tools/go/core/internal/filamentpresets` does.

const wire = JSON.parse(readFileSync(`${import.meta.dirname}/../../../../../test-fixtures/filament/preset-wire.json`, 'utf8')) as {
  row: object
  detail: { versions: object[] }
  diff: { changes: object[] }
}
const keys = (value: object) => Object.keys(value).sort()

const failure = (call: Promise<unknown>) =>
  call.then(
    () => null,
    (cause: unknown) => cause,
  )

describe('the preset mocks (#7204)', () => {
  beforeEach(() => resetMockState())

  it('write a row, a detail and a diff with the keys of the wire fixture', async () => {
    const { rows } = await listPresets()
    expect(rows.length).toBeGreaterThan(0)
    for (const row of rows) expect(keys(row)).toEqual(keys(wire.row))
    const detail = await getPreset(rows[0]!.id)
    expect(keys(detail)).toEqual(keys(wire.detail))
    for (const version of detail.versions) expect(keys(version)).toEqual(keys(wire.detail.versions[0]!))
    const manual = rows.find((row) => row.source === 'manual' && row.mirror === 'failed')!
    const diff = await diffPreset(manual.id)
    expect(keys(diff)).toEqual(keys(wire.diff))
    expect(diff.changes.length).toBeGreaterThan(0)
    for (const change of diff.changes) expect(keys(change)).toEqual(keys(wire.diff.changes[0]!))
  })

  it('seed a 3dfp, a bambuddy and a failed manual preset', async () => {
    const { rows, served } = await listPresets()
    expect(served).toBe(true)
    expect(rows.map((row) => [row.source, row.mirror])).toEqual(expect.arrayContaining([['3dfp', 'sent'], ['bambuddy', 'bambuddy_owned'], ['manual', 'failed']]))
  })

  it('filter the list by source', async () => {
    const { rows } = await listPresets({ source: 'bambuddy' })
    expect(rows.map((row) => row.source)).toEqual(['bambuddy'])
  })

  it('answer 409 document_missing for the diff of the lost version', async () => {
    const cause = await failure(diffPreset(LOST_PRESET))
    expect(cause).toBeInstanceOf(ApiError)
    expect(cause).toMatchObject({ status: 409, problem: { code: 'document_missing' } })
    expect(routeMissing(cause)).toBe(false)
  })

  it('answer 404 preset_not_found for an unknown preset', async () => {
    expect(await failure(getPreset(9999))).toMatchObject({ status: 404, problem: { code: 'preset_not_found' } })
  })

  it('answer a bare 404 with no code on every path while presetsOff() is on', async () => {
    presetsOff()
    const cause = await failure(getPreset(1))
    expect(cause).toMatchObject({ status: 404 })
    expect((cause as ApiError).problem).not.toHaveProperty('code')
    expect(routeMissing(cause)).toBe(true)
    expect(routeMissing(await failure(diffPreset(1)))).toBe(true)
    expect(await listPresets()).toEqual({ rows: [], served: false })
    resetMockState()
    expect((await listPresets()).served).toBe(true)
  })
  describe('writes', () => {
    it('answer 409 version_moved, with the current version, for an edit of an old version', async () => {
      const cause = await failure(editPreset(3, { expected_version: 1, name: 'New' }, 'k1'))
      expect(cause).toMatchObject({ status: 409, problem: { code: 'version_moved', current_version: 2 } })
    })

    it('answer 409 not_manual for an edit of a 3dfp preset', async () => {
      expect(await failure(editPreset(1, { expected_version: 1, name: 'New' }, 'k1'))).toMatchObject({ status: 409, problem: { code: 'not_manual' } })
    })

    it('answer 428 with the key problem type when no key is sent', async () => {
      const response = await fetch('/api/v1/filament/presets/3/copy', { method: 'POST', body: JSON.stringify({ name: 'x' }), headers: { 'Content-Type': 'application/json' } })
      expect(response.status).toBe(428)
      expect((await response.json()).type).toBe('https://scadbuddy.dev/problems/crud/idempotency-key-required')
    })

    it('copy a preset to a manual one, and answer the same copy for the same key', async () => {
      const first = await copyPreset(1, 'Mine', 'same')
      expect(first).toMatchObject({ source: 'manual', version: 1, name: 'Mine' })
      expect(keys(first)).toEqual(keys(wire.detail))
      expect((await copyPreset(1, 'Mine', 'same')).id).toBe(first.id)
      expect((await listPresets()).rows.filter((row) => row.name === 'Mine')).toHaveLength(1)
    })

    it('edit to a new version, and say nothing changed for a same edit', async () => {
      const edited = await editPreset(3, { expected_version: 2, set: { nozzle_temperature: '225' }, unset: ['filament_max_volumetric_speed'] }, 'e1')
      expect(edited.changed).toBe(true)
      expect(edited.preset.version).toBe(3)
      expect(edited.preset.document).toEqual({ name: 'My PLA, slower', nozzle_temperature: '225' })
      const again = await editPreset(3, { expected_version: 3, set: { nozzle_temperature: '225' } }, 'e2')
      expect(again).toMatchObject({ changed: false })
      expect(again.preset.version).toBe(3)
    })

    it('send a failed preset again, and say in_sync for a sent one', async () => {
      const result = await mirrorPreset(3)
      expect(result.outcome).toBe('sent')
      expect(result.preset).toMatchObject({ mirror: 'sent', mirror_error: null })
      expect(keys(result.preset)).toEqual(keys(wire.row))
      expect((await mirrorPreset(3)).outcome).toBe('in_sync')
    })

    it('answer 503 writes_not_configured on every write while writesOff() is on', async () => {
      writesOff()
      expect(await failure(mirrorPreset(3))).toMatchObject({ status: 503, problem: { code: 'writes_not_configured' } })
      expect(await failure(copyPreset(3, 'x', 'k'))).toMatchObject({ status: 503, problem: { code: 'writes_not_configured' } })
      expect((await getPreset(3)).id).toBe(3)
    })
  })
})
