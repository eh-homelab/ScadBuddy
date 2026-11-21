import { type ChildProcess, execFileSync, spawn } from 'node:child_process'
import { createInterface } from 'node:readline'
import { fileURLToPath } from 'node:url'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { withServiceCredential } from '../src/api/serviceCredential.js'
import { kekFromBase64 } from '../src/secrets.js'
import { forgetSubject } from '../src/sessions/forget.js'
import { PgPayloadKeys } from '../src/temporal/payloadCodec.js'
import { BackendWorkspaceBlobs } from '../src/workspace/blobs.js'
import { CrudSessionFiles } from '../src/workspace/crudStore.js'
import { WorkspaceSealing } from '../src/workspace/sealing.js'
import { SessionFiles, type WorkspaceRef } from '../src/workspace/store.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'
import { TEMPORAL_CLI, TEMPORAL_CLI_ENV } from './support/temporal.js'

// #4789 end to end: the agent's SessionFiles against the real backend's hidden routes
// (backend/tests/support/workspace_backend.py: the API on moto's S3, its own KEK and a
// Temporal dev server), both services migrated into one database, as in production
// (docs/ai/operating.md §13). It also pins the holds the agent takes through its CRUD routes
// (backend api/crud_agent.py, with the agent's service credential, #5299) to the backend's
// own `blob_refs` rows: the agent's code never touches that table, the test reads it. And it
// checks that the API's sealing (backend agent_data/workspace.py, #5299 step 7) and the agent's
// (workspace/sealing.ts) open each other's files: the harness takes the test's KEK.

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

const KEK_B64 = Buffer.alloc(32, 11).toString('base64')
const kek = kekFromBase64(KEK_B64)

/** The backend harness serving `databaseUrl`, and its base URL and an agent credential once it says READY. */
async function startBackend(databaseUrl: string): Promise<{ child: ChildProcess; base: string; token: string; log: string[] }> {
  const child = spawn('uv', ['run', '--frozen', 'python', '-m', 'tests.support.workspace_backend'], {
    cwd: BACKEND_DIR,
    env: {
      ...process.env,
      SCADBUDDY_DATABASE_URL: databaseUrl,
      SCADBUDDY_TEST_TEMPORAL_DEV_SERVER: TEMPORAL_CLI!,
      SCADBUDDY_TEST_WORKSPACE_KEK: KEK_B64,
    },
    // Its own process group: uv, python and the Temporal dev server go with one kill.
    detached: true,
    stdio: ['pipe', 'pipe', 'pipe'],
  })
  const log: string[] = []
  createInterface({ input: child.stderr! }).on('line', (line) => log.push(line))
  const [base, token] = await new Promise<[string, string]>((resolve, reject) => {
    const lines = createInterface({ input: child.stdout! })
    lines.on('line', (line) => {
      const ready = /^READY (\S+) (\S+)$/.exec(line)
      if (ready) resolve([ready[1]!, ready[2]!])
      else log.push(line)
    })
    child.on('exit', (code) => reject(new Error(`the backend exited (${code}) before it was ready:\n${log.slice(-30).join('\n')}`)))
  })
  return { child, base, token, log }
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
  let backend: { child: ChildProcess; base: string; token: string; log: string[] } | undefined
  let files: SessionFiles
  let crud: CrudSessionFiles
  let keys: PgPayloadKeys

  beforeAll(async () => {
    const throwaway = await throwawayDatabase()
    ;({ db, drop } = throwaway)
    expect(await db.ready()).toBe(true)
    backend = await startBackend(throwaway.url)
    const credential = { current: () => Promise.resolve(backend!.token), invalidate: () => {} }
    const credentialed = withServiceCredential(globalThis.fetch, credential, backend.base)
    // The sessions' data keys under the API's own KEK, as in production, so the API opens them.
    keys = new PgPayloadKeys(db.sql, kek)
    files = new SessionFiles(db.sql, {
      blobs: {
        store: new BackendWorkspaceBlobs(backend.base, { fetch: credentialed }),
        sealing: new WorkspaceSealing(keys, kek),
      },
    })
    crud = new CrudSessionFiles(backend.base, { fetch: credentialed })
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

  it("holds a file in the backend's own blob_refs through the CRUD routes, and releases it on delete", async () => {
    // The backend's table, as its migrations made it: its holder is the one the trigger once wrote.
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

    await forgetSubject({ sql: db.sql, keys }, `session-${parent.id}`)
    // The rows went in a cascade: reconcile releases the parent's hold through the routes.
    expect(await files.reconcile(0)).toEqual({ released: 1, held: 0 })
    expect(await holders()).toEqual([[stored!.blob_key, `session:${fork.id}/downloads/part.bin`]])
    expect((await files.read(forkWs, 'downloads/part.bin'))?.bytes.equals(bytes)).toBe(true)

    await forgetSubject({ sql: db.sql, keys }, `session-${fork.id}`)
    expect(await files.reconcile(0)).toEqual({ released: 1, held: 0 })
    expect(await holders()).toEqual([])
    expect(await db.sql`SELECT 1 FROM ai_session_files`).toHaveLength(0)
  }, 120_000)

  it('opens on the API what the agent sealed, and here what the API sealed (#5299 step 7)', async () => {
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: scriptedRunner(() => ({ reply: 'ok' })).runner })
    const { session } = await m.start(browser, { origin: 'chat' })
    const refs: WorkspaceRef[] = [
      { kind: 'session', sessionId: session.id },
      { kind: 'principal', principalKind: 'bearer', principalId: 'token:interop' },
    ]
    // Past two 1 MiB segments, so the segments' order and the last one's mark are checked too.
    const bytes = Buffer.from(`sealed on one side ${'z'.repeat(2 * 1024 * 1024 + 3)}`)
    for (const ref of refs) {
      await files.write(ref, 'by-agent.bin', bytes)
      expect((await crud.read(ref, 'by-agent.bin'))?.bytes.equals(bytes)).toBe(true)
      await crud.write(ref, 'by-api.bin', bytes)
      expect((await files.read(ref, 'by-api.bin'))?.bytes.equals(bytes)).toBe(true)
      // The API held its blob in the row's own transaction.
      const workspace = ref.kind === 'session' ? `session:${ref.sessionId}` : `principal:${ref.principalKind}:${ref.principalId}`
      expect((await holders()).map((h) => h[1])).toEqual(expect.arrayContaining([`${workspace}/by-agent.bin`, `${workspace}/by-api.bin`]))
      expect(await crud.delete(ref, 'by-agent.bin')).toBe(true)
      expect(await files.delete(ref, 'by-api.bin')).toBe(true)
    }
    expect(await holders()).toEqual([])
    // An OIDC caller's id holds its issuer's URL: the holds' route refuses its `//`, so only the
    // API's own path keeps such a file in the blob store.
    const oidc: WorkspaceRef = { kind: 'principal', principalKind: 'oidc', principalId: 'https://idp.example/realm#sub' }
    await crud.write(oidc, 'mine.txt', Buffer.from('oidc'))
    expect((await crud.read(oidc, 'mine.txt'))?.bytes.toString()).toBe('oidc')
    expect(await crud.delete(oidc, 'mine.txt')).toBe(true)
    expect(await holders()).toEqual([])
    expect(await crud.bytesStatus()).toEqual({ store: 'blobs', backend: 's3' })
  }, 120_000)
})

