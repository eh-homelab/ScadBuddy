import { describe, expect, it } from 'vitest'
import { createApp } from '../src/app.js'
import { originPolicy } from '../src/http/origins.js'
import { MemoryCredentials } from './support/memoryCredentials.js'
import { memoryAccess } from './support/memoryAccess.js'
import { randomBytes } from 'node:crypto'
import { kekFromBase64 } from '../src/secrets.js'

// #4828 (spec 2026-10-18 §4.6): the old switch routes are gone. The capabilities API
// (`/api/v1/ai/capabilities`) is the one way to change a tool's access.

const kek = kekFromBase64(randomBytes(32).toString('base64'))
const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https', 'content-type': 'application/json' }

function app() {
  const mem = memoryAccess()
  const a = createApp({
    access: mem.access as never,
    database: { ping: () => Promise.resolve(true), ready: () => Promise.resolve(true) },
    backend: () => Promise.resolve(true),
    kek: { ok: true, kek },
    credentials: new MemoryCredentials(),
    testConnection: () => Promise.resolve({ ok: true, detail: '', duration_ms: 0, model: null }),
    remoteAddress: () => '10.0.0.7',
    origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
  })
  return { app: a, ...mem }
}

describe('the old access routes are gone (#4828)', () => {
  it.each([
    ['GET', '/api/v1/ai/settings/printer-camera'],
    ['PUT', '/api/v1/ai/settings/printer-camera'],
    ['GET', '/api/v1/ai/settings/http-request'],
    ['PUT', '/api/v1/ai/settings/http-request'],
  ])('%s %s answers 404 and stores nothing', async (method, path) => {
    const { app: a, rows } = app()
    const res = await a.request(path, { method, headers: UI, ...(method === 'PUT' ? { body: JSON.stringify({ enabled: false }) } : {}) })
    expect(res.status).toBe(404)
    expect(rows).toEqual([])
  })
})
