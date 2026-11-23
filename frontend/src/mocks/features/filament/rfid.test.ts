import { describe, expect, it } from 'vitest'
import { ApiError } from '../../../api/client'
import { getRfidStatus, tareScale } from '../../../api/filament'
import { setPendingCommand } from './rfid'

async function refusal(promise: Promise<unknown>): Promise<ApiError> {
  const cause = await promise.then(
    () => null,
    (error: unknown) => error,
  )
  if (!(cause instanceof ApiError)) throw new Error('expected an ApiError')
  return cause
}

describe('the tare mock (#7155)', () => {
  it('queues a tare and holds it as the device\'s pending command', async () => {
    expect(await tareScale('sb-0001')).toEqual({ device_id: 'sb-0001', status: 'queued' })
    const bench = (await getRfidStatus()).devices?.find((row) => row.device_id === 'sb-0001')
    expect(bench?.pending_command).toBe('tare')
    // A tare already pending is queued again, as the backend does.
    expect((await tareScale('sb-0001')).status).toBe('queued')
  })

  it('refuses device_busy while another command is pending, as filament/scale.py does', async () => {
    setPendingCommand('sb-0001', 'write_tag')
    const busy = await refusal(tareScale('sb-0001'))
    expect(busy.status).toBe(409)
    expect(busy.problem.code).toBe('device_busy')
    expect(busy.detail).toContain('write_tag')
  })

  it('refuses no_scale and no_device', async () => {
    expect((await refusal(tareScale('sb-0002'))).problem.code).toBe('no_scale')
    const missing = await refusal(tareScale('sb-9999'))
    expect(missing.status).toBe(404)
    expect(missing.problem.code).toBe('no_device')
  })
})
