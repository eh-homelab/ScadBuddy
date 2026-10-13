import { decodePath, recordResponse } from './backend.js'
import type { LoggedRequest, ScenarioBackend } from './render.js'

// A running ScadBuddy backend (the image) behind the same recorder as the
// recorded backend (issue #1924): every request the agent's tools send is
// forwarded to it unchanged and logged with its answer, so a scenario's checks
// read the same log either way, and the render is OpenSCAD's. Live runs use it
// for the scenarios that seed nothing (`realBackend` in evals/scenarios.ts)
// when SCADBUDDY_EVAL_BACKEND_URL is set (evals/live.eval.ts).
//
// Nothing is cleaned up afterwards: the outputs a run saves stay, so point it
// at a throwaway or development backend (the AI evals workflow starts its own).

export const EVAL_BACKEND_URL_ENV = 'SCADBUDDY_EVAL_BACKEND_URL'

function parse(raw: string): unknown {
  try {
    return raw ? JSON.parse(raw) : undefined
  } catch {
    return raw
  }
}

export class RealBackend implements ScenarioBackend {
  readonly log: LoggedRequest[] = []
  private readonly base: string

  constructor(baseUrl: string) {
    this.base = baseUrl.replace(/\/+$/, '')
  }

  requests(pattern: RegExp): LoggedRequest[] {
    return this.log.filter((r) => pattern.test(`${r.method} ${r.path}`))
  }

  readonly fetch: typeof fetch = async (input, init) => {
    const request = new Request(input, init)
    const url = new URL(request.url)
    const raw = request.method === 'GET' || request.method === 'HEAD' ? undefined : await request.arrayBuffer()
    const entry: LoggedRequest = {
      method: request.method,
      path: decodePath(url.pathname),
      body: raw ? parse(Buffer.from(raw).toString('utf8')) : undefined,
    }
    this.log.push(entry)
    const response = await fetch(`${this.base}${url.pathname}${url.search}`, {
      method: request.method,
      headers: request.headers,
      ...(raw ? { body: raw } : {}),
      signal: request.signal,
    })
    await recordResponse(entry, response)
    return response
  }

  peek(path: string, init?: RequestInit): Promise<Response> {
    return fetch(`${this.base}${path}`, init)
  }
}
