import type { Credential } from '../credentials.js'
import { redact } from '../secrets.js'
import type { HarnessPaths } from './options.js'
import { runHarness } from './run.js'

// Settings' "test connection" button (issue #255: "a test-connection button
// that makes a one-turn `query()` with `maxTurns: 1`"). No tools, no plugins,
// a small budget and a timeout. The reply text is not interpreted: a
// successful result from the model is the whole test.

export type ConnectionTest = {
  ok: boolean
  /** A human-readable reason on failure; the credential is redacted from it. */
  detail: string
  duration_ms: number
  /** The model the SDK reported using, when it got that far. */
  model: string | null
}

export type ConnectionTestOptions = {
  paths: HarnessPaths
  model?: string
  timeoutMs?: number
}

export const TEST_PROMPT = 'Reply with the single word: ok'

export async function testConnection(credential: Credential, options: ConnectionTestOptions): Promise<ConnectionTest> {
  const started = Date.now()
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(new Error('timed out')), options.timeoutMs ?? 60_000)
  const stderr: string[] = []
  let model: string | null = null
  const done = (ok: boolean, detail: string): ConnectionTest => ({
    ok,
    detail: redact(detail, [credential.secret]),
    duration_ms: Date.now() - started,
    model,
  })
  try {
    const run = runHarness({
      paths: options.paths,
      credential,
      prompt: TEST_PROMPT,
      maxTurns: 1,
      maxBudgetUsd: 0.05,
      signal: controller.signal,
      ...(options.model === undefined ? {} : { model: options.model }),
      stderr: (line) => {
        if (stderr.length < 20) stderr.push(line.trim())
      },
    })
    for await (const message of run) {
      if (message.type === 'system' && message.subtype === 'init') model = message.model
      // Claude Code retries failed requests with backoff (a 401 is retried
      // too, measured against the fake endpoint in test/run.test.ts). A
      // connection test should answer now, so the first retry is the verdict.
      if (message.type === 'system' && message.subtype === 'api_retry') {
        const status = message.error_status === null ? 'no response' : `HTTP ${message.error_status}`
        return done(false, `the model endpoint refused the request (${status}: ${message.error})`)
      }
      if (message.type === 'assistant' && message.error) {
        return done(false, `the model endpoint answered with an error: ${message.error}`)
      }
      if (message.type === 'result') {
        if (message.subtype === 'success' && !message.is_error) return done(true, 'connected')
        const reason =
          message.subtype === 'success' ? message.result : `${message.subtype}: ${message.errors.join('; ')}`
        return done(false, reason || message.subtype)
      }
    }
    return done(false, `Claude Code exited without a result. ${stderr.join(' ')}`.trim())
  } catch (err) {
    if (controller.signal.aborted) return done(false, 'timed out waiting for the model endpoint')
    return done(false, `${(err as Error).message} ${stderr.join(' ')}`.trim())
  } finally {
    clearTimeout(timer)
    // Stops the Claude Code process when we returned early (retry, error).
    controller.abort()
  }
}
