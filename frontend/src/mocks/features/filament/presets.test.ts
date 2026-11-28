import { readFileSync } from 'node:fs'
import { beforeEach, describe, expect, it } from 'vitest'
import { ApiError } from '../../../api/client'
import { copyPreset, diffPreset, editPreset, getPreset, listPresets, mirrorPreset, routeMissing, type PresetEdit } from '../../../api/filamentPresets'
import { resetMockState } from '../../handlers'
import { LOST_PRESET, keyReused, mirrorShows, presetsOff, writePending, writesOff } from './presets'

// #7204 — the preset mocks answer as `tools/go/core/internal/filamentpresets` does.

const wire = JSON.parse(readFileSync(`${import.meta.dirname}/../../../../../test-fixtures/filament/preset-wire.json`, 'utf8')) as {
  row: object
  detail: { versions: object[] }
  edited: object
  mirror_again_outcome: object
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

    it('answer an edit with the keys of the wire fixture', async () => {
      const edited = await editPreset(3, { expected_version: 2, set: { nozzle_temperature: '225' } }, 'ek')
      expect(keys(edited)).toEqual(keys(wire.edited))
      expect(keys(edited.preset)).toEqual(keys(wire.detail))
    })

    it('answer 428 before it looks the preset up', async () => {
      for (const path of ['/9999/copy', '/9999']) {
        const response = await fetch(`/api/v1/filament/presets${path}`, { method: path.endsWith('copy') ? 'POST' : 'PATCH', body: '{}', headers: { 'Content-Type': 'application/json' } })
        expect(response.status).toBe(428)
      }
    })

    describe('the checks of begin in writes.go', () => {
      const send = (path: string, method: string, headers: Record<string, string>, body = '{"name":"x"}') =>
        fetch(`/api/v1/filament/presets${path}`, { method, body, headers: { 'Content-Type': 'application/json', ...headers } })

      it.each(['0', '01', '-1', 'abc', '1.5', '99999999999999999999'])('answer 404 preset_not_found for the path id %s, before the key and the writes check', async (id) => {
        writesOff()
        for (const [path, method] of [[`/${id}/copy`, 'POST'], [`/${id}`, 'PATCH'], [`/${id}/mirror`, 'POST']] as const) {
          const response = await send(path, method, {})
          expect(response.status).toBe(404)
          expect((await response.json()).code).toBe('preset_not_found')
        }
      })

      it('answer 422 invalid_input for an Idempotency-Key that is empty or longer than 200 characters', async () => {
        for (const key of ['', 'k'.repeat(201)]) {
          const response = await send('/3/copy', 'POST', { 'Idempotency-Key': key })
          expect(response.status).toBe(422)
          expect((await response.json()).code).toBe('invalid_input')
        }
        expect((await send('/3/copy', 'POST', { 'Idempotency-Key': 'k'.repeat(200) })).status).toBe(201)
      })

      it.each(['not json', '[]', 'null', '"x"', '1'])('answer 422 invalid_input for the body %s, as Go does', async (body) => {
        for (const [path, method] of [['/3/copy', 'POST'], ['/3', 'PATCH']] as const) {
          const response = await send(path, method, { 'Idempotency-Key': 'k' }, body)
          expect(response.status).toBe(422)
          expect(await response.json()).toMatchObject({ code: 'invalid_input', detail: 'the body is not a JSON object' })
        }
      })

      it('refuse a credential key whose name has the dotted capital I (U+0130), as Go does', async () => {
        const cause = await failure(editPreset(3, { expected_version: 2, set: { 'PR\u0130NTHOST_AP\u0130KEY': 'x' } }, 'k'))
        expect(cause).toMatchObject({ status: 422, problem: { code: 'key_refused' } })
      })
    })

    describe('the body checks of R7 come before not_manual and the lookup', () => {
      const refused = (body: Partial<PresetEdit>, id = 1) => failure(editPreset(id, { expected_version: 1, ...body } as PresetEdit, 'k'))

      it.each([
        ['a key with a space', { set: { 'a b': 'x' } }, 'a b'],
        ['a key longer than 128 characters', { unset: ['k'.repeat(129)] }, 'k'.repeat(64) + '…'],
        ['the key name', { set: { name: 'x' } }, 'name'],
        ['a credential key', { set: { printhost_apikey: 'x' } }, 'printhost_apikey'],
        ['a key that holds a token', { unset: ['my_Token_key'] }, 'my_Token_key'],
      ])('refuse %s as key_refused 422 with the key', async (_what, body, key) => {
        const cause = await refused(body)
        expect(cause).toMatchObject({ status: 422, problem: { code: 'key_refused', key } })
        expect((cause as ApiError).detail).toBe(`the key ${key} cannot be set or unset`)
      })

      it('refuse a key both set and unset', async () => {
        expect(await refused({ set: { a: 'x' }, unset: ['a'] })).toMatchObject({ status: 422, problem: { code: 'key_refused', key: 'a' } })
      })

      it('refuse more than 500 keys in set and unset together', async () => {
        const set = Object.fromEntries(Array.from({ length: 300 }, (_, i) => [`s${i}`, 'x']))
        const unset = Array.from({ length: 201 }, (_, i) => `u${i}`)
        expect(await refused({ set, unset })).toMatchObject({ status: 422, problem: { code: 'invalid_input' } })
      })

      it('refuse a value that is not a string or a list of strings', async () => {
        const cause = await refused({ set: { a: 1 } as unknown as Record<string, string> })
        expect(cause).toMatchObject({ status: 422, problem: { code: 'invalid_input' } })
        expect(await refused({ set: { a: [1] } as unknown as Record<string, string[]> })).toMatchObject({ status: 422, problem: { code: 'invalid_input' } })
      })

      it('refuse an edit with no name, set or unset', async () => {
        expect(await refused({})).toMatchObject({ status: 422, problem: { code: 'invalid_input' } })
      })

      it('refuse a body check on a 3dfp preset before not_manual, and not_manual after the checks', async () => {
        expect(await refused({ set: { password: 'x' } }, 1)).toMatchObject({ status: 422 })
        expect(await refused({ name: 'x' }, 1)).toMatchObject({ status: 409, problem: { code: 'not_manual' } })
        expect(await refused({ name: 'x' }, 9999)).toMatchObject({ status: 404, problem: { code: 'preset_not_found' } })
      })
    })

    it('answer 503 preset_write_pending on each write while writePending() is on', async () => {
      writePending()
      for (const call of [() => copyPreset(3, 'x', 'k'), () => editPreset(3, { expected_version: 2, name: 'y' }, 'k'), () => mirrorPreset(3)]) {
        expect(await failure(call())).toMatchObject({ status: 503, problem: { code: 'preset_write_pending' } })
      }
      writePending(false)
      expect((await copyPreset(3, 'x', 'k')).name).toBe('x')
    })

    it.each(['mirror_off', 'not_configured', 'instance_changed'] as const)('answer 409 %s on a send again while mirrorShows() holds it', async (code) => {
      mirrorShows(code)
      expect(await failure(mirrorPreset(3))).toMatchObject({ status: 409, problem: { code } })
      expect(await failure(mirrorPreset(2))).toMatchObject({ status: 409, problem: { code: 'not_mirrored' } })
    })

    it.each(['busy', 'pending', 'failed'] as const)('answer the outcome %s of a send again while mirrorShows() holds it', async (outcome) => {
      mirrorShows(outcome)
      const result = await mirrorPreset(3)
      expect(result.outcome).toBe(outcome)
      expect(keys(result)).toEqual(keys(wire.mirror_again_outcome))
      expect(result.preset.mirror).toBe('failed')
      if (outcome === 'failed') expect(result.preset.mirror_error).not.toBeNull()
    })

    it('answer 422 idempotency_key_reused on a copy and an edit while keyReused() is on', async () => {
      keyReused()
      for (const call of [() => copyPreset(3, 'x', 'k'), () => editPreset(3, { expected_version: 2, name: 'y' }, 'k')]) {
        const cause = await failure(call())
        expect(cause).toMatchObject({ status: 422, problem: { code: 'idempotency_key_reused', type: 'https://scadbuddy.dev/problems/crud/idempotency-key-reused' } })
      }
      expect((await mirrorPreset(3)).outcome).toBe('sent')
      keyReused(false)
      expect((await copyPreset(3, 'x', 'k')).name).toBe('x')
    })

    it('answer 503 writes_not_configured on every write while writesOff() is on', async () => {
      writesOff()
      expect(await failure(mirrorPreset(3))).toMatchObject({ status: 503, problem: { code: 'writes_not_configured' } })
      expect(await failure(copyPreset(3, 'x', 'k'))).toMatchObject({ status: 503, problem: { code: 'writes_not_configured' } })
      expect((await getPreset(3)).id).toBe(3)
    })
  })
})
