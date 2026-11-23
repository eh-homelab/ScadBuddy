import { createServer } from 'node:http'
import type { AddressInfo } from 'node:net'
import { describe, expect, it } from 'vitest'
import { backendReachable, createBackendClient } from '../src/api/backend.js'
import { DotSegmentError } from '../src/api/pathSegments.js'

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

  it('follows no redirect, a backstop for a call that reached another route (#7104)', async () => {
    // As FastAPI answers a path with a trailing slash: 307 to another route.
    const seen: string[] = []
    const server = createServer((req, res) => {
      seen.push(`${req.method} ${req.url}`)
      if (req.url !== '/api/v1/hosted-plugins/p') {
        res.writeHead(307, { location: '/api/v1/hosted-plugins/p' }).end()
        return
      }
      res.writeHead(200, { 'content-type': 'application/json' }).end('{"deleted":"plugin"}')
    })
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
    try {
      const { port } = server.address() as AddressInfo
      const client = createBackendClient(`http://127.0.0.1:${port}`)
      await expect(
        client.DELETE('/api/v1/hosted-plugins/{name}/files/{path}', { params: { path: { name: 'p', path: 'x' } } }),
      ).rejects.toThrow()
      expect(seen).toEqual(['DELETE /api/v1/hosted-plugins/p/files/x'])
    } finally {
      await new Promise((resolve) => server.close(resolve))
    }
  })

  it.each(['..', '.'])('never sends a path parameter of %s: the URL would drop a segment (#7221)', async (item) => {
    // Mid-path, no redirect is needed: `/orders/o1/items/../quote` is `/orders/o1/quote`.
    const sent: string[] = []
    const client = createBackendClient('http://api.example', async (request) => {
      sent.push(new Request(request).url)
      return new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } })
    })
    const call = client.POST('/api/v1/orders/{order_id}/items/{item_id}/quote', {
      params: { path: { order_id: 'o1', item_id: item } },
      body: {} as never,
    })
    await expect(call).rejects.toBeInstanceOf(DotSegmentError)
    await expect(call).rejects.toThrow('/api/v1/orders/{order_id}/items/{item_id}/quote: a path segment is "." or ".."')
    expect(sent).toEqual([])
    // Dots among other text, or a `%2e` (its `%` is encoded), are sent as one segment.
    for (const safe of ['../x', '%2e%2e']) {
      await client.POST('/api/v1/orders/{order_id}/items/{item_id}/quote', {
        params: { path: { order_id: 'o1', item_id: safe } },
        body: {} as never,
      })
    }
    expect(sent).toEqual(['http://api.example/api/v1/orders/o1/items/..%2Fx/quote', 'http://api.example/api/v1/orders/o1/items/%252e%252e/quote'])
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
