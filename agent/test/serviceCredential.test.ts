import { randomBytes } from 'node:crypto'
import type { Sql } from 'postgres'
import { describe, expect, it } from 'vitest'
import { credentialAad, REREAD_MS, ServiceCredential, serviceCredentialMiddleware, withServiceCredential } from '../src/api/serviceCredential.js'
import { kekFromBase64, sealSecret } from '../src/secrets.js'

// #4975 phase 4: the agent's service credential, read from its view and unsealed.

const kek = kekFromBase64(randomBytes(32).toString('base64'))
const TOKEN = `sbsvc_0123456789abcdef.${'a'.repeat(43)}`

function fakeSql(rows: () => unknown[]): { sql: Sql; reads: () => number } {
  let reads = 0
  const sql = ((..._args: unknown[]) => {
    reads += 1
    return Promise.resolve(rows())
  }) as unknown as Sql
  return { sql, reads: () => reads }
}

function row(token = TOKEN) {
  const scopes = ['internal.workspace', 'api']
  const envelope = sealSecret(kek, token, credentialAad('0123456789abcdef', 'agent', scopes))
  return {
    id: '0123456789abcdef',
    caller: 'agent',
    scopes,
    secret_sealed: envelope.secretSealed,
    dek_sealed: envelope.dekSealed,
    kek_id: envelope.kekId,
  }
}

describe('ServiceCredential', () => {
  it('binds the AAD as the backend does, scopes sorted', () => {
    expect(credentialAad('id', 'agent', ['internal.workspace', 'api'])).toBe(
      'service_credentials|id|agent|api,internal.workspace',
    )
  })

  it('unseals the newest live one and reads again only after an hour', async () => {
    let now = 0
    const { sql, reads } = fakeSql(() => [row()])
    const credential = new ServiceCredential(sql, kek, () => now)
    expect(await credential.current()).toBe(TOKEN)
    expect(await credential.current()).toBe(TOKEN)
    expect(reads()).toBe(1)
    now = REREAD_MS
    await credential.current()
    expect(reads()).toBe(2)
  })

  it('sends nothing without the KEK or a credential', async () => {
    expect(await new ServiceCredential(fakeSql(() => [row()]).sql, undefined).current()).toBeUndefined()
    expect(await new ServiceCredential(fakeSql(() => []).sql, kek).current()).toBeUndefined()
  })

  it('keeps the last token when a read fails', async () => {
    let now = 0
    let fail = false
    const sql = (() => (fail ? Promise.reject(new Error('down')) : Promise.resolve([row()]))) as unknown as Sql
    const credential = new ServiceCredential(sql, kek, () => now, () => undefined)
    expect(await credential.current()).toBe(TOKEN)
    fail = true
    now = REREAD_MS
    expect(await credential.current()).toBe(TOKEN)
  })

  it('puts it on the workspace blob routes, and on nothing outside the backend', async () => {
    const seen: (string | null)[] = []
    const inner = (async (_input: string | URL | Request, init?: RequestInit) => {
      seen.push(new Headers(init?.headers).get('authorization'))
      return new Response('{}')
    }) as typeof fetch
    const credential = new ServiceCredential(fakeSql(() => [row()]).sql, kek)
    const wrapped = withServiceCredential(inner, credential, 'http://backend:8080/')
    await wrapped('http://backend:8080/api/v1/internal/workspace-blobs/uploads', { method: 'POST' })
    await wrapped('https://bucket.s3.example/blob?X-Amz-Signature=x', { method: 'PUT' })
    expect(seen).toEqual([`Bearer ${TOKEN}`, null])
  })

  it('puts it on the request', async () => {
    const middleware = serviceCredentialMiddleware(new ServiceCredential(fakeSql(() => [row()]).sql, kek))
    const request = new Request('http://backend/api/v1/models')
    const sent = (await middleware.onRequest!({ request } as never)) as Request
    expect(sent.headers.get('authorization')).toBe(`Bearer ${TOKEN}`)
  })
})
