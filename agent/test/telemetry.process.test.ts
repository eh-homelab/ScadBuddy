// agent/test/telemetry.process.test.ts
import { execFile, spawn } from 'node:child_process'
import { rm, writeFile } from 'node:fs/promises'
import { createServer, type Server } from 'node:http'
import type { AddressInfo } from 'node:net'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { promisify } from 'node:util'
import { afterAll, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { TRACE_ID, TRACEPARENT } from './support/tracing.js'

// The entry as it runs in the image (spec 2026-10-01 §5.4):
// `node --import ./dist/telemetry.js dist/main.js`. Incoming requests continue
// the caller's trace; only the backend client injects `traceparent`; fetch,
// node:http, the plugin forwarder's request on to a plugin and the
// http_request tool's carry none; with no endpoint nothing is exported, and
// with OTEL_SDK_DISABLED nothing is traced at all and the SDK is never loaded.

const AGENT = fileURLToPath(new URL('..', import.meta.url))
const OUT = path.join(AGENT, 'node_modules', '.cache', 'scadbuddy-telemetry-test', String(process.pid))
const TSC = path.join(AGENT, 'node_modules', 'typescript', 'bin', 'tsc')

const CHILD = `
import http from 'node:http'
import { mkdtemp } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { trace } from '@opentelemetry/api'
const { createBackendClient } = await import('./api/backend.js')
// What main.ts imports: the SDK-free half (telemetry/runtime.ts).
const { shutdownTelemetry, traceListener } = await import('./telemetry/runtime.js')
const { PluginForwarder } = await import('./plugins/forwarder.js')
const { runHttpRequest, withDefaults } = await import('./harness/httpRequest.js')

const target = process.argv[2]
// HttpInstrumentation wraps Server.prototype.emit with shimmer, which marks the wrapper.
const emitWrapped = Object.hasOwn(http.Server.prototype.emit, '__wrapped')
const get = (url, headers = {}) =>
  new Promise((resolve, reject) => {
    http
      .get(url, { headers }, (res) => {
        let body = ''
        res.setEncoding('utf8')
        res.on('data', (chunk) => { body += chunk })
        res.on('end', () => resolve(body))
      })
      .on('error', reject)
  })
const post = (url, body, headers = {}) =>
  new Promise((resolve, reject) => {
    const req = http.request(url, { method: 'POST', headers: { 'content-type': 'application/json', ...headers } }, (res) => {
      res.resume()
      res.on('end', () => resolve(res.statusCode))
    })
    req.on('error', reject)
    req.end(body)
  })

const server = http.createServer((_req, res) => {
  res.end(JSON.stringify({ traceId: trace.getActiveSpan()?.spanContext().traceId ?? null }))
})
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
// Like main.ts: only the app's own listener is traced.
traceListener(server.address().port)
const incoming = JSON.parse(await get('http://127.0.0.1:' + server.address().port + '/', { traceparent: '${TRACEPARENT}' }))

// The plugin forwarder (plugins/forwarder.ts), with a plugin at the target:
// its listener is not the app's, its path is a capability, and the request
// it sends on carries no trace context, whatever came in.
const forwarder = await PluginForwarder.start()
const route = forwarder.register({ name: 'p', url: target + '/plugin', toolTiers: {}, disabledTools: [] }, '127.0.0.1')

const client = createBackendClient(target)
const work = await trace.getTracer('child').startActiveSpan('work', async (span) => {
  await client.GET('/healthz')
  await fetch(target + '/plain')
  await get(target + '/node')
  const forwardedStatus = await post(route.url, '{"jsonrpc":"2.0","id":1,"method":"ping"}', { traceparent: '${TRACEPARENT}' })
  // The http_request tool as a turn runs it (harness/httpRequest.ts).
  const saveDir = path.join(await mkdtemp(path.join(os.tmpdir(), 'sb-process-')), 'http')
  const requested = await runHttpRequest(withDefaults({ url: target + '/http-request', method: 'GET' }), {
    saveDir,
    secrets: () => [],
    actor: { kind: 'browser', id: 'user', label: 'User' },
  })
  span.end()
  return { traceId: span.spanContext().traceId, forwardedStatus, requestFailed: requested.isError === true }
})
await forwarder.close()
server.close()
await shutdownTelemetry()
const sdkLoaded = (globalThis.resolvedUrls ?? []).some((url) => url.includes('/@opentelemetry/sdk-node/'))
// Like main.ts, exit explicitly: a pending export retry to an unreachable
// collector would otherwise keep the loop alive past shutdownTelemetry's budget.
process.stdout.write(JSON.stringify({ incoming: incoming.traceId, forwarderToken: route.url.split('/p/')[1], work: work.traceId, forwardedStatus: work.forwardedStatus, requestFailed: work.requestFailed, emitWrapped, sdkLoaded, hooks: globalThis.registeredHooks }) + '\\n', () => process.exit(0))
`

// Imported before telemetry.js: records each loader hook registered, since
// node:module offers no way to list them, and every module resolved (an
// in-thread hook, which registers no loader). syncBuiltinESMExports carries
// the patch to the named `register` export telemetry.ts imports.
const RECORDER = `
import module, { syncBuiltinESMExports } from 'node:module'
const original = module.register
globalThis.registeredHooks = []
globalThis.resolvedUrls = []
module.registerHooks({
  resolve(specifier, context, next) {
    const resolved = next(specifier, context)
    globalThis.resolvedUrls.push(resolved.url)
    return resolved
  },
})
module.register = (specifier, ...rest) => {
  globalThis.registeredHooks.push(String(specifier))
  return original(specifier, ...rest)
}
syncBuiltinESMExports()
`

type Hit = { path: string; traceparent: string | undefined; contentType: string | undefined; body: Buffer }

type Out = {
  incoming: string | null
  forwarderToken: string
  work: string
  forwardedStatus: number
  requestFailed: boolean
  emitWrapped: boolean
  sdkLoaded: boolean
  hooks: string[]
}

let target: Server
let url: string
const hits: Hit[] = []

beforeAll(async () => {
  await rm(OUT, { recursive: true, force: true })
  await promisify(execFile)(
    process.execPath,
    [
      TSC,
      'src/telemetry.ts',
      'src/api/backend.ts',
      'src/telemetry/runtime.ts',
      'src/plugins/forwarder.ts',
      'src/harness/httpRequest.ts',
      '--ignoreConfig',
      '--outDir', OUT,
      '--rootDir', 'src',
      '--module', 'NodeNext',
      '--moduleResolution', 'NodeNext',
      '--target', 'ES2024',
      '--lib', 'ES2024',
      '--types', 'node',
      '--strict',
      '--verbatimModuleSyntax',
      '--skipLibCheck',
    ],
    { cwd: AGENT },
  )
  await writeFile(path.join(OUT, 'child.mjs'), CHILD)
  await writeFile(path.join(OUT, 'recorder.mjs'), RECORDER)
  target = createServer((req, res) => {
    const chunks: Buffer[] = []
    const hit: Hit = {
      path: (req.url ?? '').split('?')[0] ?? '',
      traceparent: req.headers.traceparent as string | undefined,
      contentType: req.headers['content-type'],
      body: Buffer.alloc(0),
    }
    hits.push(hit)
    req.on('data', (chunk: Buffer) => chunks.push(chunk))
    req.on('end', () => {
      hit.body = Buffer.concat(chunks)
      if (req.url === '/v1/traces') {
        res.writeHead(200, { 'content-type': 'application/x-protobuf' }).end()
        return
      }
      res.writeHead(200, { 'content-type': 'application/json' }).end('{"status":"ok"}')
    })
  })
  await new Promise<void>((resolve) => target.listen(0, '127.0.0.1', resolve))
  url = `http://127.0.0.1:${(target.address() as AddressInfo).port}`
}, 180_000)

afterAll(async () => {
  await new Promise<void>((resolve) => target.close(() => resolve()))
  await rm(OUT, { recursive: true, force: true })
})

beforeEach(() => {
  hits.length = 0
})

async function runChild(env: Record<string, string>): Promise<{ code: number | null; out: Out }> {
  const child = spawn(
    process.execPath,
    [
      '--import', pathToFileURL(path.join(OUT, 'recorder.mjs')).href,
      '--import', pathToFileURL(path.join(OUT, 'telemetry.js')).href,
      path.join(OUT, 'child.mjs'),
      url,
    ],
    { env: { PATH: process.env.PATH ?? '', ...env }, stdio: ['ignore', 'pipe', 'inherit'] },
  )
  let stdout = ''
  child.stdout.setEncoding('utf8')
  child.stdout.on('data', (chunk: string) => {
    stdout += chunk
  })
  const code = await new Promise<number | null>((resolve) => child.on('exit', resolve))
  const last = stdout.trim().split('\n').at(-1) ?? '{}'
  return { code, out: JSON.parse(last) as Out }
}

const hit = (p: string) => hits.find((h) => h.path === p)

describe('the agent under `node --import ./dist/telemetry.js`', () => {
  it('continues incoming traces, injects only on backend calls, and exports OTLP/protobuf', async () => {
    const { code, out } = await runChild({ OTEL_EXPORTER_OTLP_ENDPOINT: url })
    expect(code).toBe(0)
    expect(out.incoming).toBe(TRACE_ID)
    expect(out.emitWrapped).toBe(true)
    expect(out.hooks).toEqual(['@opentelemetry/instrumentation/hook.mjs'])
    expect(hit('/healthz')?.traceparent).toMatch(new RegExp(`^00-${out.work}-[0-9a-f]{16}-01$`))
    expect(hit('/plain')).toMatchObject({ traceparent: undefined })
    expect(hit('/node')).toMatchObject({ traceparent: undefined })
    expect(out.forwardedStatus).toBe(200)
    expect(hit('/plugin')).toMatchObject({ traceparent: undefined })
    expect(out.requestFailed).toBe(false)
    expect(hit('/http-request')).toMatchObject({ traceparent: undefined })
    expect(out.sdkLoaded).toBe(true)
    expect(hit('/v1/traces')?.contentType).toBe('application/x-protobuf')
  }, 30_000)

  it('leaves every listener but the app’s untraced: the forwarder’s token reaches no span', async () => {
    const { code, out } = await runChild({ OTEL_EXPORTER_OTLP_ENDPOINT: url })
    expect(code).toBe(0)
    expect(out.incoming).toBe(TRACE_ID)
    expect(out.forwardedStatus).toBe(200)
    const exported = Buffer.concat(hits.filter((h) => h.path === '/v1/traces').map((h) => h.body))
    expect(exported.length).toBeGreaterThan(0)
    expect(out.forwarderToken).toMatch(/^[A-Za-z0-9_-]{24}$/)
    expect(exported.includes(out.forwarderToken)).toBe(false)
    expect(exported.includes('/p/')).toBe(false)
  }, 30_000)

  it('without an endpoint still propagates, and exports nothing', async () => {
    const { code, out } = await runChild({})
    expect(code).toBe(0)
    expect(out.incoming).toBe(TRACE_ID)
    expect(hit('/healthz')?.traceparent).toMatch(new RegExp(`^00-${out.work}-`))
    expect(hit('/v1/traces')).toBeUndefined()
  }, 30_000)

  it('with OTEL_SDK_DISABLED=true traces nothing, injects nothing, exports nothing', async () => {
    const { code, out } = await runChild({ OTEL_SDK_DISABLED: 'true', OTEL_EXPORTER_OTLP_ENDPOINT: url })
    expect(code).toBe(0)
    expect(out.incoming).toBeNull()
    // The kill switch's point: no loader hook, so node:http is never shimmed.
    expect(out.emitWrapped).toBe(false)
    expect(out.hooks).toEqual([])
    // Nor is the SDK loaded: main.ts's telemetry/runtime.ts imports none of it.
    expect(out.sdkLoaded).toBe(false)
    expect(hit('/plugin')).toMatchObject({ traceparent: undefined })
    expect(hit('/healthz')).toMatchObject({ traceparent: undefined })
    expect(hit('/v1/traces')).toBeUndefined()
  }, 30_000)

  it('an unreachable collector costs the process nothing', async () => {
    const { code } = await runChild({ OTEL_EXPORTER_OTLP_ENDPOINT: 'http://127.0.0.1:9' })
    expect(code).toBe(0)
    expect(hit('/healthz')?.traceparent).toMatch(/^00-/)
  }, 30_000)
})
