import { beforeEach, describe, expect, it } from 'vitest'
import { ApiError } from '../../../api/client'
import { resolveUnknownTray } from '../../../api/unknownTrays'
import { resetMockState } from '../../handlers'
import { spoolId } from './seed'
import { resolvePendingOnce, resolveWritesOff, seedUnknownTray } from './unknownTrays'

// #8489 — the resolve mock answers as Go's route does (`tools/go/core/internal/unknowntrays/resolve.go`).

const codeOf = (call: Promise<unknown>) =>
  call.then(
    () => null,
    (cause: unknown) => (cause instanceof ApiError ? { status: cause.status, code: (cause.problem as { code?: string }).code } : cause),
  )

describe('the unknown-tray resolve mock (#8489)', () => {
  beforeEach(() => resetMockState())

  it('refuses a body Go refuses with 422 invalid_input', async () => {
    const tray = seedUnknownTray()
    for (const body of [
      { spool_id: spoolId(9), code: 'S-9', net_weight_g: 980, seen_at: tray.seen_at },
      { net_weight_g: 980, seen_at: tray.seen_at },
      { code: 'S-9', net_weight_g: 0, seen_at: tray.seen_at },
      { code: 'S-9', net_weight_g: 100000.5, seen_at: tray.seen_at },
      { code: 'S-9', net_weight_g: 980, seen_at: '2026-11-26T09:30:00' },
      { code: 'S-9', net_weight_g: 980, seen_at: tray.seen_at, colour: 'red' },
    ]) {
      expect(await codeOf(resolveUnknownTray(tray.id, body as never))).toEqual({ status: 422, code: 'invalid_input' })
    }
  })

  it('answers the spool identity, then tray_changed for the same tray', async () => {
    const tray = seedUnknownTray()
    const resolved = await resolveUnknownTray(tray.id, { code: 'S-9', net_weight_g: 980, seen_at: tray.seen_at })
    expect(Object.keys(resolved).sort()).toEqual(['code', 'id', 'location_id', 'number'])
    expect(resolved).toMatchObject({ id: spoolId(9), number: 9, code: 'S-9' })
    expect(await codeOf(resolveUnknownTray(tray.id, { code: 'S-9', net_weight_g: 980, seen_at: tray.seen_at }))).toEqual({ status: 409, code: 'tray_changed' })
  })

  it('looks the spool up by code when spool_id is null, as core does', async () => {
    const tray = seedUnknownTray()
    const resolved = await resolveUnknownTray(tray.id, { spool_id: null, code: 'S-9', net_weight_g: 980, seen_at: tray.seen_at } as never)
    expect(resolved).toMatchObject({ id: spoolId(9), number: 9, code: 'S-9' })
  })

  it('answers each refusal with its status and code', async () => {
    const tray = seedUnknownTray()
    const body = { net_weight_g: 980, seen_at: tray.seen_at }
    expect(await codeOf(resolveUnknownTray('not-a-uuid', { code: 'S-9', ...body }))).toEqual({ status: 409, code: 'tray_changed' })
    expect(await codeOf(resolveUnknownTray(tray.id, { code: 'S-999', ...body }))).toEqual({ status: 404, code: 'code_unknown' })
    expect(await codeOf(resolveUnknownTray(tray.id, { spool_id: '00000000-0000-4000-8000-000000000000', ...body }))).toEqual({ status: 404, code: 'spool_missing' })
    expect(await codeOf(resolveUnknownTray(tray.id, { code: 'S-19', ...body }))).toEqual({ status: 409, code: 'spool_archived' })
    resolvePendingOnce()
    expect(await codeOf(resolveUnknownTray(tray.id, { code: 'S-9', ...body }))).toEqual({ status: 503, code: 'resolve_pending' })
    resolveWritesOff()
    expect(await codeOf(resolveUnknownTray(tray.id, { code: 'S-9', ...body }))).toEqual({ status: 503, code: 'writes_not_configured' })
  })
})
