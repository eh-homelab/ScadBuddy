import postgres from 'postgres'
import { type Credential, CredentialStore, SettingsStore } from '../src/credentials.js'
import { loadKek } from '../src/secrets.js'
import { SETTING_MODEL } from '../src/sessions/manager.js'

// Where a live eval run gets its Claude credential (issue #259: "with a
// credential supplied to that job only"). In order:
//
//   1. The credential configured in Settings, read from the agent's own
//      database exactly as the service reads it (src/main.ts: CredentialStore
//      `reveal` under the KEK in SCADBUDDY_SECRET_KEY_FILE). This is the local
//      path: point SCADBUDDY_DATABASE_URL and SCADBUDDY_SECRET_KEY_FILE at a
//      running deployment's database and key. The model chosen in Settings
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

async function fromDatabase(url: string, keyFile: string | undefined, options: ResolveOptions): Promise<EvalCredential> {
  const kek = await loadKek(keyFile)
  if (!kek.ok) return { ok: false, reason: `SCADBUDDY_DATABASE_URL is set but ${kek.reason}` }
  const sql = postgres(url, {
    max: 1,
    connect_timeout: 5,
    onnotice: () => {},
    ...(options.searchPath === undefined ? {} : { connection: { search_path: options.searchPath } }),
  })
  try {
    const credential = await new CredentialStore(sql).reveal(kek.kek)
    if (!credential) return { ok: false, reason: 'no Claude credential is saved in Settings (ai_credentials is empty)' }
    const model = await new SettingsStore(sql).get<string>(SETTING_MODEL).catch(() => undefined)
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
