import { type ChildProcess, execFileSync, spawn } from 'node:child_process'
import { createInterface } from 'node:readline'
import { fileURLToPath } from 'node:url'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { kekFromBase64 } from '../src/secrets.js'
import { forgetSubject } from '../src/sessions/forget.js'
import { BackendWorkspaceBlobs } from '../src/workspace/blobs.js'
import { WorkspaceSealing } from '../src/workspace/sealing.js'
import { SessionFiles, type WorkspaceRef } from '../src/workspace/store.js'
import { testPayloadKeys } from './support/payloadKeys.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'
import { TEMPORAL_CLI, TEMPORAL_CLI_ENV } from './support/temporal.js'

// #4789 end to end: the agent's SessionFiles against the real backend's hidden routes
// (backend/tests/support/workspace_backend.py: the API on moto's S3, its own KEK and a
// Temporal dev server), both services migrated into one database, as in production
// (docs/ai/operating.md §13: one database, one role). It also pins the agent's trigger
// (migration 20261027T0114Z) to the backend's own `blob_refs` table: a change there that
// the trigger does not follow fails here.

const BACKEND_DIR = fileURLToPath(new URL('../../backend/', import.meta.url))

function hasUv(): boolean {
  try {
    execFileSync('uv', ['--version'], { stdio: 'ignore' })
    return true
  } catch {
    return false
  }
}

const skip = !TEST_DATABASE_URL
  ? `${TEST_DATABASE_URL_ENV} is not set`
  : !TEMPORAL_CLI
    ? `${TEMPORAL_CLI_ENV} is not set and no temporal on PATH`
    : !hasUv()
      ? 'uv is not on PATH (the backend runs under it)'
      : undefined

const kek = kekFromBase64(Buffer.alloc(32, 11).toString('base64'))

/** The backend harness serving `databaseUrl`, and its base URL once it says READY. */
async function startBackend(databaseUrl: string): Promise<{ child: ChildProcess; base: string; log: string[] }> {
  const child = spawn('uv', ['run', '--frozen', 'python', '-m', 'tests.support.workspace_backend'], {
    cwd: BACKEND_DIR,
    env: { ...process.env, SCADBUDDY_DATABASE_URL: databaseUrl, SCADBUDDY_TEST_TEMPORAL_DEV_SERVER: TEMPORAL_CLI! },
    // Its own process group: uv, python and the Temporal dev server go with one kill.
    detached: true,
    stdio: ['pipe', 'pipe', 'pipe'],
  })
  const log: string[] = []
  createInterface({ input: child.stderr! }).on('line', (line) => log.push(line))
  const base = await new Promise<string>((resolve, reject) => {
    const lines = createInterface({ input: child.stdout! })
    lines.on('line', (line) => {
      log.push(line)
      const ready = /^READY (\S+)$/.exec(line)
      if (ready) resolve(ready[1]!)
    })
    child.on('exit', (code) => reject(new Error(`the backend exited (${code}) before it was ready:\n${log.slice(-30).join('\n')}`)))
  })
  return { child, base, log }
}

function stopBackend(child: ChildProcess | undefined): Promise<void> {
  if (!child?.pid || child.exitCode !== null) return Promise.resolve()
  return new Promise((resolve) => {
    const done = setTimeout(() => {
      try {
        process.kill(-child.pid!, 'SIGKILL')
      } catch {
        // gone already
      }
      resolve()
    }, 15_000)
    child.on('exit', () => {
      clearTimeout(done)
      resolve()
    })
    child.stdin?.end()
    try {
      process.kill(-child.pid!, 'SIGTERM')
    } catch {
      // gone already
    }
  })
}

describe.skipIf(skip !== undefined)(`workspace files through the real backend${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let backend: { child: ChildProcess; base: string; log: string[] } | undefined
  let files: SessionFiles

  beforeAll(async () => {
    const throwaway = await throwawayDatabase()
    ;({ db, drop } = throwaway)
    expect(await db.ready()).toBe(true)
    backend = await startBackend(throwaway.url)
    files = new SessionFiles(db.sql, {
      blobs: { store: new BackendWorkspaceBlobs(backend.base), sealing: new WorkspaceSealing(testPayloadKeys(db.sql), kek, { sql: db.sql }) },
    })
  }, 240_000)
  afterAll(async () => {
    await stopBackend(backend?.child)
    await drop?.()
  }, 60_000)

  const holders = async () =>
    (
      await db.sql<{ key: string; holder_id: string }[]>`
        SELECT key, holder_id FROM blob_refs WHERE holder_kind = 'ai_session_file' ORDER BY holder_id`
    ).map((r) => [r.key, r.holder_id])

  it("holds a file in the backend's own blob_refs, with the columns the trigger writes", async () => {
    // The backend's table, as its migrations made it: the trigger names these columns and this key.
    const columns = await db.sql<{ column_name: string }[]>`
      SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = 'blob_refs'`
    expect(columns.map((c) => c.column_name)).toEqual(expect.arrayContaining(['key', 'holder_kind', 'holder_id', 'created_at']))
    const [pkey] = await db.sql<{ def: string }[]>`
      SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint WHERE conrelid = 'blob_refs'::regclass AND contype = 'p'`
    expect(pkey?.def).toBe('PRIMARY KEY (key, holder_kind, holder_id)')
    const principal: WorkspaceRef = { kind: 'principal', principalKind: 'bearer', principalId: 'token:pin' }
    await files.write(principal, 'pin.txt', Buffer.from('pinned'))
    const [row] = await db.sql<{ blob_key: string }[]>`SELECT blob_key FROM ai_session_files WHERE workspace = 'principal:bearer:token:pin'`
    const [ref] = await db.sql<{ key: string; holder_kind: string; holder_id: string; created_at: Date }[]>`
      SELECT key, holder_kind, holder_id, created_at FROM blob_refs WHERE key = ${row!.blob_key}`
    expect(ref).toMatchObject({ key: row!.blob_key, holder_kind: 'ai_session_file', holder_id: 'principal:bearer:token:pin/pin.txt' })
    expect(ref?.created_at).toBeInstanceOf(Date)
    // The backend indexed the blob as a workspace file of its own store.
    expect((await files.read(principal, 'pin.txt'))?.bytes.toString()).toBe('pinned')
    expect(await files.delete(principal, 'pin.txt')).toBe(true)
    expect(await db.sql`SELECT 1 FROM blob_refs WHERE key = ${row!.blob_key}`).toHaveLength(0)
    expect(await files.bytesStatus()).toEqual({ store: 'blobs', backend: 's3' })
  }, 60_000)

  it('writes, reads, forks, and forgets both sessions, leaving no holder', async () => {
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: scriptedRunner(() => ({ reply: 'ok' })).runner })
    const { session: parent } = await m.start(browser, { origin: 'chat' })
    const { session: fork } = await m.start(browser, { origin: 'chat' })
    const parentWs: WorkspaceRef = { kind: 'session', sessionId: parent.id }
    const forkWs: WorkspaceRef = { kind: 'session', sessionId: fork.id }
    const bytes = Buffer.from(`through the backend ${'x'.repeat(3 * 1024 * 1024)}`)

    await files.write(parentWs, 'downloads/part.bin', bytes)
    expect((await files.read(parentWs, 'downloads/part.bin'))?.bytes.equals(bytes)).toBe(true)
    const [stored] = await db.sql<{ blob_key: string; data: Buffer | null }[]>`
      SELECT blob_key, data FROM ai_session_files WHERE workspace = ${`session:${parent.id}`}`
    expect(stored?.data).toBeNull()
    expect(stored?.blob_key).toMatch(/^workspace-[0-9a-f]{64}$/)

    await files.copy(parent.id, fork.id)
    expect(await holders()).toEqual(
      [
        [stored!.blob_key, `session:${fork.id}/downloads/part.bin`],
        [stored!.blob_key, `session:${parent.id}/downloads/part.bin`],
      ].sort((a, b) => a[1]!.localeCompare(b[1]!)),
    )

    await forgetSubject({ sql: db.sql, keys: testPayloadKeys(db.sql) }, `session-${parent.id}`)
    expect(await holders()).toEqual([[stored!.blob_key, `session:${fork.id}/downloads/part.bin`]])
    expect((await files.read(forkWs, 'downloads/part.bin'))?.bytes.equals(bytes)).toBe(true)

    await forgetSubject({ sql: db.sql, keys: testPayloadKeys(db.sql) }, `session-${fork.id}`)
    expect(await holders()).toEqual([])
    expect(await db.sql`SELECT 1 FROM ai_session_files`).toHaveLength(0)
  }, 120_000)
})

