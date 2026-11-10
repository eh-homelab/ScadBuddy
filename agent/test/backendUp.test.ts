import { createServer, type Server } from 'node:http'
import type { AddressInfo } from 'node:net'
import type { Sql } from 'postgres'
import { afterEach, describe, expect, it } from 'vitest'
import { backendNotUp, NOT_UP, transportKind, untilBackendAnswers, WAIT_FIRST_MS, WAIT_MAX_MS } from '../src/api/backendUp.js'
import { BackendWorkspaceBlobs, WorkspaceBlobError } from '../src/workspace/blobs.js'
import type { WorkspaceSealing } from '../src/workspace/sealing.js'
import { SessionFiles } from '../src/workspace/store.js'

// #5868: the API pod's agent starts before the backend container answers on 127.0.0.1:8080.
// These run against a real port nothing listens on yet, then a backend started on it later.

/** A free port on 127.0.0.1 that nothing listens on now. */
async function freePort(): Promise<number> {
  const probe = createServer()
  await new Promise<void>((resolve) => probe.listen(0, '127.0.0.1', resolve))
  const { port } = probe.address() as AddressInfo
  await new Promise((resolve) => probe.close(resolve))
  return port
}

let server: Server | undefined
afterEach(async () => {
  if (server) await new Promise((resolve) => server!.close(resolve))
  server = undefined
})

/** The backend's /healthz and workspace upload route, started late on `port`. */
async function startBackend(port: number): Promise<void> {
  server = createServer((req, res) => {
    if (req.url === '/healthz') {
      res.writeHead(200, { 'content-type': 'application/json' })
      res.end(JSON.stringify({ status: 'ok', store: { backend: 's3' } }))
      return
    }
    res.writeHead(200, { 'content-type': 'application/json' })
    res.end(JSON.stringify({ pending_key: 'pending-workspace-x', url: { url: 'http://127.0.0.1:1/', expires_at: '' } }))
  })
  await new Promise<void>((resolve) => server!.listen(port, '127.0.0.1', resolve))
}

describe('backendNotUp', () => {
  it("is a fetch refused by a port nothing listens on, and nothing else", async () => {
    const port = await freePort()
    const refused = await fetch(`http://127.0.0.1:${port}/healthz`).catch((error: unknown) => error)
    expect(refused).toBeInstanceOf(TypeError)
    expect(backendNotUp(refused)).toBe(true)
    const both = new TypeError('fetch failed', {
      cause: new AggregateError([Object.assign(new Error('a'), { code: 'ECONNREFUSED' })]),
    })
    expect(backendNotUp(both)).toBe(true)
    // `localhost` tried on ::1 first: a refusal anywhere in the list is the backend not up yet.
    const mixed = new TypeError('fetch failed', {
      cause: new AggregateError([
        Object.assign(new Error('a'), { code: 'EADDRNOTAVAIL' }),
        Object.assign(new Error('b'), { code: 'ECONNREFUSED' }),
      ]),
    })
    expect(backendNotUp(mixed)).toBe(true)
    expect(transportKind(mixed)).toBe(NOT_UP)
    expect(backendNotUp(new TypeError('fetch failed', { cause: Object.assign(new Error('x'), { code: 'ENOTFOUND' }) }))).toBe(false)
    expect(backendNotUp(Object.assign(new Error('x'), { code: 'ECONNREFUSED' }))).toBe(false)
    expect(backendNotUp(new DOMException('timed out', 'TimeoutError'))).toBe(false)
  })
})

describe('untilBackendAnswers', () => {
  it('logs the wait once and tries again with back-off until the backend answers', async () => {
    const logs: string[] = []
    const sleeps: number[] = []
    let tries = 0
    const answer = await untilBackendAnswers(
      async () => {
        tries += 1
        if (tries < 8) throw new WorkspaceBlobError('the backend is not up yet (/healthz)', { unreachable: NOT_UP })
        return 's3'
      },
      {
        kind: (error) => (error instanceof WorkspaceBlobError ? error.unreachable : undefined),
        log: (message) => logs.push(message),
        error: () => expect.unreachable(),
        sleep: async (ms) => {
          sleeps.push(ms)
        },
      },
    )
    expect(answer).toBe('s3')
    expect(logs).toEqual(['waiting for the backend: the backend is not up yet (/healthz)'])
    expect(sleeps).toEqual([WAIT_FIRST_MS, 2_000, 4_000, 8_000, 16_000, WAIT_MAX_MS, WAIT_MAX_MS])
  })

  it('logs any other transport error as an error, once per kind, and keeps trying (#5889)', async () => {
    const logs: string[] = []
    const errors: string[] = []
    const kinds = ['ENOTFOUND', 'ENOTFOUND', NOT_UP, 'ETIMEDOUT', 'ENOTFOUND', NOT_UP]
    let tries = 0
    const answer = await untilBackendAnswers(
      async () => {
        const kind = kinds[tries++]
        if (kind === undefined) return 's3'
        const why = kind === NOT_UP ? 'the backend is not up yet' : 'the backend could not be reached'
        throw new WorkspaceBlobError(`${why} (/healthz)`, { unreachable: kind })
      },
      {
        kind: (error) => (error instanceof WorkspaceBlobError ? error.unreachable : undefined),
        log: (message) => logs.push(message),
        error: (message) => errors.push(message),
        sleep: async () => undefined,
      },
    )
    expect(answer).toBe('s3')
    expect(tries).toBe(7)
    expect(logs).toEqual(['waiting for the backend: the backend is not up yet (/healthz)'])
    expect(errors).toEqual([
      'the backend could not be reached (/healthz): ENOTFOUND; trying again until it answers',
      'the backend could not be reached (/healthz): ETIMEDOUT; trying again until it answers',
    ])
  })

  it('throws any other error at once', async () => {
    const refusal = new WorkspaceBlobError("the backend's /healthz names no store backend (HTTP 500)")
    await expect(
      untilBackendAnswers(() => Promise.reject(refusal), {
        kind: (error) => (error instanceof WorkspaceBlobError ? error.unreachable : undefined),
        log: () => expect.unreachable(),
        error: () => expect.unreachable(),
        sleep: async () => expect.unreachable(),
      }),
    ).rejects.toBe(refusal)
  })

  it('waits for a real backend that starts late', async () => {
    const port = await freePort()
    const store = new BackendWorkspaceBlobs(`http://127.0.0.1:${port}`)
    const logs: string[] = []
    let slept = 0
    const answer = await untilBackendAnswers(() => store.backend(), {
      kind: (error) => (error instanceof WorkspaceBlobError ? error.unreachable : undefined),
      log: (message) => logs.push(message),
      error: () => expect.unreachable(),
      sleep: async () => {
        slept += 1
        if (slept === 2) await startBackend(port)
      },
    })
    expect(answer).toBe('s3')
    expect(slept).toBe(2)
    expect(logs).toEqual(['waiting for the backend: the backend is not up yet (/healthz)'])
  })
})

describe('the workspace before the backend is up', () => {
  it('says the backend is not up yet, and needs no restart once it is', async () => {
    const port = await freePort()
    const store = new BackendWorkspaceBlobs(`http://127.0.0.1:${port}`)
    const error = await store.backend().catch((err: unknown) => err)
    expect(error).toBeInstanceOf(WorkspaceBlobError)
    expect((error as WorkspaceBlobError).message).toBe('the backend is not up yet (/healthz)')
    expect((error as WorkspaceBlobError).unreachable).toBe(NOT_UP)

    // The count of blob rows: none.
    const sql = (() => Promise.resolve([{ n: 0 }])) as unknown as Sql
    const files = new SessionFiles(sql, { blobs: { store, sealing: {} as WorkspaceSealing } })
    expect(await files.bytesStatus()).toEqual({ store: 'refused', reason: 'the backend is not up yet (/healthz)' })

    // A failure is never cached: the next status (and each write's target) asks again.
    await startBackend(port)
    expect(await files.bytesStatus()).toEqual({ store: 'blobs', backend: 's3' })
  })

  it('names the kind of a backend URL that does not answer (#5889)', async () => {
    const lookupFailed = (async () => {
      throw new TypeError('fetch failed', { cause: Object.assign(new Error('getaddrinfo ENOTFOUND backendd'), { code: 'ENOTFOUND' }) })
    }) as typeof fetch
    const error = await new BackendWorkspaceBlobs('http://backendd:8080', { fetch: lookupFailed }).backend().catch((err: unknown) => err)
    expect((error as WorkspaceBlobError).message).toBe('the backend could not be reached (/healthz)')
    expect((error as WorkspaceBlobError).unreachable).toBe('ENOTFOUND')
    expect(transportKind(new DOMException('timed out', 'TimeoutError'))).toBe('TimeoutError')
  })

  it('names no presigned URL as the backend not up', async () => {
    const backendPort = await freePort()
    await startBackend(backendPort)
    // The transfer URL the backend mints here (port 1, where nothing listens) stands for S3.
    const error = await new BackendWorkspaceBlobs(`http://127.0.0.1:${backendPort}`).put(Buffer.from('x')).catch((err: unknown) => err)
    expect(error).toBeInstanceOf(WorkspaceBlobError)
    expect((error as WorkspaceBlobError).message).toBe('the backend could not be reached (PUT)')
    // Not the backend: nothing that waits for the backend reads it (#5889).
    expect((error as WorkspaceBlobError).unreachable).toBeUndefined()
  })
})
