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
// the caller's trace; only the backend client injects `traceparent`; fetch
// and node:http requests carry none; with no endpoint nothing is exported,
// and with OTEL_SDK_DISABLED nothing is traced at all.

const AGENT = fileURLToPath(new URL('..', import.meta.url))
const OUT = path.join(AGENT, 'node_modules', '.cache', 'scadbuddy-telemetry-test', String(process.pid))
const TSC = path.join(AGENT, 'node_modules', 'typescript', 'bin', 'tsc')

const CHILD = `
import http from 'node:http'
import { trace } from '@opentelemetry/api'
const { createBackendClient } = await import('./api/backend.js')
const { shutdownTelemetry } = await import('./telemetry/setup.js')

const target = process.argv[2]
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

const server = http.createServer((_req, res) => {
  res.end(JSON.stringify({ traceId: trace.getActiveSpan()?.spanContext().traceId ?? null }))
})
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
const incoming = JSON.parse(await get('http://127.0.0.1:' + server.address().port + '/', { traceparent: '${TRACEPARENT}' }))

const client = createBackendClient(target)
const work = await trace.getTracer('child').startActiveSpan('work', async (span) => {
  await client.GET('/healthz')
  await fetch(target + '/plain')
  await get(target + '/node')
  span.end()
  return span.spanContext().traceId
})
server.close()
await shutdownTelemetry()
// Like main.ts, exit explicitly: a pending export retry to an unreachable
// collector would otherwise keep the loop alive past shutdownTelemetry's budget.
process.stdout.write(JSON.stringify({ incoming: incoming.traceId, work }) + '\\n', () => process.exit(0))
`

type Hit = { path: string; traceparent: string | undefined; contentType: string | undefined }

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
  target = createServer((req, res) => {
    hits.push({
      path: (req.url ?? '').split('?')[0] ?? '',
      traceparent: req.headers.traceparent as string | undefined,
      contentType: req.headers['content-type'],
    })
    req.resume()
    req.on('end', () => {
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

async function runChild(env: Record<string, string>): Promise<{ code: number | null; out: { incoming: string | null; work: string } }> {
  const child = spawn(
    process.execPath,
    ['--import', pathToFileURL(path.join(OUT, 'telemetry.js')).href, path.join(OUT, 'child.mjs'), url],
    { env: { PATH: process.env.PATH ?? '', ...env }, stdio: ['ignore', 'pipe', 'inherit'] },
  )
  let stdout = ''
  child.stdout.setEncoding('utf8')
  child.stdout.on('data', (chunk: string) => {
    stdout += chunk
  })
  const code = await new Promise<number | null>((resolve) => child.on('exit', resolve))
  const last = stdout.trim().split('\n').at(-1) ?? '{}'
  return { code, out: JSON.parse(last) as { incoming: string | null; work: string } }
}

const hit = (p: string) => hits.find((h) => h.path === p)

describe('the agent under `node --import ./dist/telemetry.js`', () => {
  it('continues incoming traces, injects only on backend calls, and exports OTLP/protobuf', async () => {
    const { code, out } = await runChild({ OTEL_EXPORTER_OTLP_ENDPOINT: url })
    expect(code).toBe(0)
    expect(out.incoming).toBe(TRACE_ID)
    expect(hit('/healthz')?.traceparent).toMatch(new RegExp(`^00-${out.work}-[0-9a-f]{16}-01$`))
    expect(hit('/plain')).toMatchObject({ traceparent: undefined })
    expect(hit('/node')).toMatchObject({ traceparent: undefined })
    expect(hit('/v1/traces')?.contentType).toBe('application/x-protobuf')
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
    expect(hit('/healthz')).toMatchObject({ traceparent: undefined })
    expect(hit('/v1/traces')).toBeUndefined()
  }, 30_000)

  it('an unreachable collector costs the process nothing', async () => {
    const { code } = await runChild({ OTEL_EXPORTER_OTLP_ENDPOINT: 'http://127.0.0.1:9' })
    expect(code).toBe(0)
    expect(hit('/healthz')?.traceparent).toMatch(/^00-/)
  }, 30_000)
})
