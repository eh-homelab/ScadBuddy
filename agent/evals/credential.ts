import postgres from 'postgres'
import type { Credential } from '../src/credentials.js'
import { SqlCredentials } from '../test/support/sql/credentials.js'
import { sqlSettings } from '../test/support/sql/settings.js'
import { readFile } from 'node:fs/promises'
import { type Kek, kekFromBase64 } from '../src/secrets.js'
import { SETTING_MODEL } from '../src/sessions/manager.js'

// Where a live eval run gets its Claude credential (issue #259: "with a
// credential supplied to that job only"). In order:
//
//   1. The credential configured in Settings, read from the agents' tables and
//      opened under the KEK in SCADBUDDY_SECRET_KEY_FILE, as the API opens it
//      (test/support/sql/credentials.ts). This is the local path: point
//      SCADBUDDY_DATABASE_URL and SCADBUDDY_SECRET_KEY_FILE at a running
//      deployment's database and key. The model chosen in Settings
//      (ai_settings `model`) is used too, unless SCADBUDDY_EVAL_MODEL is set.
//   2. SCADBUDDY_EVAL_ANTHROPIC_API_KEY, for the opt-in CI job only
//      (.github/workflows/ai-evals.yml passes it from a repository secret). A
//      dedicated name, so a developer's own ANTHROPIC_API_KEY is never picked up
//      by accident.
//
// With neither, the run SKIPS with the reason; it never fails for want of a key.

export const EVAL_API_KEY_ENV = 'SCADBUDDY_EVAL_ANTHROPIC_API_KEY'
export const EVAL_MODEL_ENV = 'SCADBUDDY_EVAL_MODEL'

export type EvalCredential =
  | { ok: true; credential: Credential; source: string; model?: string }
  | { ok: false; reason: string }

type Env = Readonly<Record<string, string | undefined>>

const present = (v: string | undefined) => (v?.trim() ? v.trim() : undefined)

/** `searchPath` is for tests, which use a throwaway schema (test/support/postgres.ts). */
export type ResolveOptions = { searchPath?: string }

/** The key file, as the API reads it: 32 random bytes, base64. Never throws. */
async function readKek(file: string | undefined): Promise<{ ok: true; kek: Kek } | { ok: false; reason: string }> {
  if (file === undefined) return { ok: false, reason: 'SCADBUDDY_SECRET_KEY_FILE is not set' }
  let text: string
  try {
    text = await readFile(file, 'utf8')
  } catch {
    return { ok: false, reason: 'SCADBUDDY_SECRET_KEY_FILE cannot be read' }
  }
  try {
    return { ok: true, kek: kekFromBase64(text) }
  } catch {
    return { ok: false, reason: 'SCADBUDDY_SECRET_KEY_FILE does not hold a valid key (32 random bytes, base64)' }
  }
}

async function fromDatabase(url: string, keyFile: string | undefined, options: ResolveOptions): Promise<EvalCredential> {
  const kek = await readKek(keyFile)
  if (!kek.ok) return { ok: false, reason: `SCADBUDDY_DATABASE_URL is set but ${kek.reason}` }
  const sql = postgres(url, {
    max: 1,
    connect_timeout: 5,
    onnotice: () => {},
    ...(options.searchPath === undefined ? {} : { connection: { search_path: options.searchPath } }),
  })
  try {
    const credential = await new SqlCredentials(sql, kek.kek).reveal()
    if (!credential) return { ok: false, reason: 'no Claude credential is saved in Settings (ai_credentials is empty)' }
    const model = await sqlSettings(sql).get<string>(SETTING_MODEL).catch(() => undefined)
    return {
      ok: true,
      credential,
      source: `the ${credential.kind} saved in Settings`,
      ...(typeof model === 'string' && model ? { model } : {}),
    }
  } catch (err) {
    return { ok: false, reason: `could not read the saved credential: ${err instanceof Error ? err.message : String(err)}` }
  } finally {
    await sql.end({ timeout: 5 })
  }
}

export async function resolveEvalCredential(env: Env = process.env, options: ResolveOptions = {}): Promise<EvalCredential> {
  const override = present(env[EVAL_MODEL_ENV])
  const withModel = (c: EvalCredential): EvalCredential => (c.ok && override ? { ...c, model: override } : c)

  const databaseUrl = present(env.SCADBUDDY_DATABASE_URL)
  let databaseReason: string | undefined
  if (databaseUrl) {
    const found = await fromDatabase(databaseUrl, present(env.SCADBUDDY_SECRET_KEY_FILE), options)
    if (found.ok) return withModel(found)
    databaseReason = found.reason
  }
  const key = present(env[EVAL_API_KEY_ENV])
  if (key) return withModel({ ok: true, credential: { kind: 'anthropic_api_key', secret: key }, source: EVAL_API_KEY_ENV })
  return {
    ok: false,
    reason:
      (databaseReason ? `${databaseReason}, and ` : 'SCADBUDDY_DATABASE_URL is not set and ') +
      `${EVAL_API_KEY_ENV} is not set. Live evals need a Claude credential: see docs/ai/evals.md.`,
  }
}
