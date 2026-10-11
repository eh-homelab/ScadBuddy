import { Hono } from 'hono'
import { describe, expect, it } from 'vitest'
import { originPolicy } from '../src/http/origins.js'
import type { SettingsRepo } from '../src/routes/headlessBrowser.js'
import {
  MAX_FILE_BYTES,
  MAX_WORKSPACE_BYTES,
  MIN_WORKSPACE_BYTES,
  registerWorkspaceSettingsRoutes,
  WORKSPACE_SETTINGS_PATH,
} from '../src/routes/workspaceSettings.js'
import {
  DEFAULT_FILE_MAX_BYTES,
  DEFAULT_WORKSPACE_MAX_BYTES,
  SessionFiles,
  WORKSPACE_FILE_MAX_SETTING,
  WORKSPACE_MAX_SETTING,
} from '../src/workspace/store.js'

// /api/v1/ai/settings/workspace (#2329): the workspace caps, behind the same UI
// guards as every other AI settings route (routes/guard.ts), read by the store.

const URL_ = `https://scadbuddy.example${WORKSPACE_SETTINGS_PATH}`
const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }
const MiB = 1024 * 1024

function setup(options: { ready?: boolean; settings?: boolean; stored?: Record<string, unknown> } = {}) {
  const values = new Map<string, unknown>(Object.entries(options.stored ?? {}))
  const contexts: unknown[] = []
  const repo: SettingsRepo = {
    get: <T>(key: string) => Promise.resolve(values.get(key) as T),
    set: (key, value, context) => {
      contexts.push(context)
      values.set(key, value)
      return Promise.resolve()
    },
  }
  const app = new Hono()
  registerWorkspaceSettingsRoutes(app, {
    settings: options.settings === false ? undefined : repo,
    ready: () => Promise.resolve(options.ready ?? true),
    remoteAddress: () => '10.0.0.7',
    origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
  })
  return { app, repo, values, contexts }
}

const get = (app: Hono, headers: Record<string, string> = { host: UI.host, 'x-forwarded-proto': 'https' }) =>
  app.request(URL_, { headers })
const put = (app: Hono, body: unknown, headers: Record<string, string> = UI) =>
  app.request(URL_, { method: 'PUT', headers: { ...headers, 'content-type': 'application/json' }, body: JSON.stringify(body) })

const bounds = { min: MIN_WORKSPACE_BYTES, file_max: MAX_FILE_BYTES, workspace_max: MAX_WORKSPACE_BYTES }

describe('the workspace caps setting', () => {
  it('is 25 MiB a file and 200 MiB a workspace until set, and the store reads what is saved', async () => {
    const { app, repo, values, contexts } = setup()
    const before = await get(app)
    expect(before.status).toBe(200)
    expect(await before.json()).toEqual({ file_max_bytes: 25 * MiB, workspace_max_bytes: 200 * MiB, ...bounds })
    expect([DEFAULT_FILE_MAX_BYTES, DEFAULT_WORKSPACE_MAX_BYTES]).toEqual([25 * MiB, 200 * MiB])

    const saved = await put(app, { file_max_bytes: 5 * MiB, workspace_max_bytes: 50 * MiB })
    expect(saved.status).toBe(200)
    expect(await saved.json()).toEqual({ file_max_bytes: 5 * MiB, workspace_max_bytes: 50 * MiB, ...bounds })
    expect(values.get(WORKSPACE_FILE_MAX_SETTING)).toBe(5 * MiB)
    expect(values.get(WORKSPACE_MAX_SETTING)).toBe(50 * MiB)
    // One audited write per key, as the UI user from the request's address.
    expect(contexts).toEqual([
      expect.objectContaining({ surface: 'http', clientIp: '10.0.0.7' }),
      expect.objectContaining({ surface: 'http', clientIp: '10.0.0.7' }),
    ])
    // The store's caps are these settings (it never touches the database for them).
    const files = new SessionFiles(undefined as never, { settings: repo })
    expect(await files.limits()).toEqual({ fileMaxBytes: 5 * MiB, workspaceMaxBytes: 50 * MiB })
  })

  it('takes the bounds themselves', async () => {
    const { app } = setup()
    expect((await put(app, { file_max_bytes: MIN_WORKSPACE_BYTES, workspace_max_bytes: MIN_WORKSPACE_BYTES })).status).toBe(200)
    expect((await put(app, { file_max_bytes: MAX_FILE_BYTES, workspace_max_bytes: MAX_WORKSPACE_BYTES })).status).toBe(200)
  })

  it.each([
    [{}],
    [{ file_max_bytes: 5 * MiB }],
    [{ file_max_bytes: '5', workspace_max_bytes: 50 * MiB }],
    [{ file_max_bytes: 5.5, workspace_max_bytes: 50 * MiB }],
    [{ file_max_bytes: MIN_WORKSPACE_BYTES - 1, workspace_max_bytes: 50 * MiB }],
    [{ file_max_bytes: MAX_FILE_BYTES + 1, workspace_max_bytes: MAX_WORKSPACE_BYTES }],
    [{ file_max_bytes: 5 * MiB, workspace_max_bytes: MAX_WORKSPACE_BYTES + 1 }],
    [{ file_max_bytes: 50 * MiB, workspace_max_bytes: 5 * MiB }],
    [{ file_max_bytes: 5 * MiB, workspace_max_bytes: 50 * MiB, extra: 1 }],
  ])('refuses the body %j, and stores nothing', async (body) => {
    const { app, values } = setup()
    expect((await put(app, body)).status).toBe(400)
    expect(values.size).toBe(0)
  })

  it('reads a stored value it could not have written as the default', async () => {
    const { app } = setup({ stored: { [WORKSPACE_FILE_MAX_SETTING]: 'big', [WORKSPACE_MAX_SETTING]: -1 } })
    expect(await (await get(app)).json()).toMatchObject({ file_max_bytes: 25 * MiB, workspace_max_bytes: 200 * MiB })
  })

  it('refuses a write from another origin, and answers 503 without a database or before it is ready', async () => {
    expect((await put(setup().app, { file_max_bytes: MiB, workspace_max_bytes: MiB }, { ...UI, origin: 'https://evil.example' })).status).toBe(403)
    expect((await get(setup().app, { host: UI.host, 'x-forwarded-proto': 'https', 'sec-fetch-site': 'cross-site' })).status).toBe(403)
    expect((await get(setup({ settings: false }).app)).status).toBe(503)
    expect((await put(setup({ ready: false }).app, { file_max_bytes: MiB, workspace_max_bytes: MiB })).status).toBe(503)
  })
})
