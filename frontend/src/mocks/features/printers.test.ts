import { beforeEach, describe, expect, it } from 'vitest'
import { resetMockState } from '../handlers'
import { HOT_PRINTER } from './hotPath'
import { withLanLinked } from './printers'

const at = (path: string, init?: RequestInit) => fetch(new URL(`/api/v1${path}`, window.location.href), init)
const GARAGE = '00000000-0000-4000-8000-0000000000aa'
const PIN = 'ab'.repeat(32)

beforeEach(() => resetMockState())

describe('printer mock lan_linked (#7276 Task 6, P14)', () => {
  it('follows the backend rule: physical, host, pinned cert and access code', () => {
    const bare = { ...HOT_PRINTER, lan_cert_sha256: PIN, lan_linked: undefined }
    expect(withLanLinked(bare).lan_linked).toBe(true)
    expect(withLanLinked({ ...bare, lan_cert_sha256: null }).lan_linked).toBe(false)
    expect(withLanLinked({ ...bare, has_access_code: false }).lan_linked).toBe(false)
    expect(withLanLinked({ ...bare, lan_host: null }).lan_linked).toBe(false)
    expect(withLanLinked({ ...bare, kind: 'bambuddy_virtual' }).lan_linked).toBe(false)
  })

  it('is recomputed when a printer is linked and unlinked over the LAN', async () => {
    const get = async () => ((await (await at(`/printers/${GARAGE}`)).json()) as { lan_linked: boolean }).lan_linked
    expect(await get()).toBe(false)
    const put = await at(`/printers/${GARAGE}/lan`, {
      method: 'PUT',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ host: '10.0.0.7', access_code: '12345678' }),
    })
    expect(((await put.json()) as { lan_linked: boolean }).lan_linked).toBe(true)
    expect(await get()).toBe(true)
    await at(`/printers/${GARAGE}/lan`, { method: 'DELETE' })
    expect(await get()).toBe(false)
  })
})
