import { readFileSync } from 'node:fs'
import { beforeEach, describe, expect, it } from 'vitest'
import { ApiError } from '../../../api/client'
import { diffPreset, getPreset, listPresets, routeMissing } from '../../../api/filamentPresets'
import { resetMockState } from '../../handlers'
import { LOST_PRESET, presetsOff } from './presets'

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
})
