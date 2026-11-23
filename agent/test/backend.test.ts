import { createServer } from 'node:http'
import type { AddressInfo } from 'node:net'
import { describe, expect, it } from 'vitest'
import { backendReachable, createBackendClient } from '../src/api/backend.js'

describe('backend client', () => {
  it('calls the backend health route under the configured base URL', async () => {
    const seen: string[] = []
    const client = createBackendClient('http://127.0.0.1:8080', async (input) => {
      seen.push(input instanceof Request ? input.url : String(input))
      return Response.json({ status: 'ok' })
    })
    expect(await backendReachable(client)).toBe(true)
    expect(seen).toEqual(['http://127.0.0.1:8080/healthz'])
  })

  it('is unreachable on a non-2xx answer or a network error', async () => {
    const failing = createBackendClient('http://b', async () => new Response('no', { status: 503 }))
    expect(await backendReachable(failing)).toBe(false)
    const throwing = createBackendClient('http://b', async () => {
      throw new TypeError('fetch failed')
    })
    expect(await backendReachable(throwing)).toBe(false)
  })

  it('follows no redirect: a `..` path parameter cannot reach another route (#7104)', async () => {
    // As FastAPI answers a path with a trailing slash: 307 to the path without it.
    const seen: string[] = []
    const server = createServer((req, res) => {
      seen.push(`${req.method} ${req.url}`)
      if (req.url?.endsWith('/')) {
        res.writeHead(307, { location: req.url.slice(0, -1) }).end()
        return
      }
      res.writeHead(200, { 'content-type': 'application/json' }).end('{"deleted":"plugin"}')
    })
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
    try {
      const { port } = server.address() as AddressInfo
      const client = createBackendClient(`http://127.0.0.1:${port}`)
      // `..` stays a bare segment, so the URL is `/api/v1/hosted-plugins/p/`.
      await expect(
        client.DELETE('/api/v1/hosted-plugins/{name}/files/{path}', { params: { path: { name: 'p', path: '..' } } }),
      ).rejects.toThrow()
      expect(seen).toEqual(['DELETE /api/v1/hosted-plugins/p/'])
    } finally {
      await new Promise((resolve) => server.close(resolve))
    }
  })

  it("keeps a request's JSON body through the copy that refuses redirects (#7179)", async () => {
    let received = ''
    const server = createServer((req, res) => {
      req.setEncoding('utf8')
      req.on('data', (chunk: string) => (received += chunk))
      req.on('end', () => res.writeHead(200, { 'content-type': 'application/json' }).end('{}'))
    })
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
    try {
      const { port } = server.address() as AddressInfo
      const client = createBackendClient(`http://127.0.0.1:${port}`)
      await client.POST('/api/v1/customers', { body: { display_name: 'C' } })
      expect(JSON.parse(received)).toMatchObject({ display_name: 'C' })
    } finally {
      await new Promise((resolve) => server.close(resolve))
    }
  })
})
