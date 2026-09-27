// The agent service's whole environment surface. Design spec §9
// (docs/superpowers/specs/2026-09-27-ai-integration-design.md): "There are no
// AI env vars. The only variables the agent reads are SCADBUDDY_DATABASE_URL
// (shared with #241), the backend URL, and the key-encryption key file." Every
// AI-specific setting (credentials, MCP auth mode, plugins) lives in the
// database and is edited in Settings, so do not add a variable here for one.

export type Config = {
  /** Postgres URL shared with the backend (#241). Unset → AI features are disabled. */
  databaseUrl: string | undefined
  /** Where the Python backend listens; the sidecar reaches it over the pod's localhost (spec §4.3). */
  backendUrl: string
  /** Key-encryption key file for envelope encryption (spec §9). Read by #255, not yet. */
  secretKeyFile: string | undefined
}

export const DEFAULT_BACKEND_URL = 'http://127.0.0.1:8080'

/** The only variable names this service reads. Tests assert nothing else is consulted. */
export const ENV_VARS = [
  'SCADBUDDY_DATABASE_URL',
  'SCADBUDDY_BACKEND_URL',
  'SCADBUDDY_SECRET_KEY_FILE',
] as const

type Env = Readonly<Partial<Record<(typeof ENV_VARS)[number], string>>>

export class ConfigError extends Error {
  override name = 'ConfigError'
}

/** Empty and whitespace-only values count as unset, the way a blank Helm value arrives. */
function present(value: string | undefined): string | undefined {
  const trimmed = value?.trim()
  return trimmed ? trimmed : undefined
}

export function loadConfig(env: Env = process.env): Config {
  const databaseUrl = present(env.SCADBUDDY_DATABASE_URL)
  if (databaseUrl !== undefined) {
    let scheme: string
    try {
      scheme = new URL(databaseUrl).protocol
    } catch {
      // The URL itself is not echoed: it normally carries the password.
      throw new ConfigError('SCADBUDDY_DATABASE_URL is not a valid URL')
    }
    if (scheme !== 'postgres:' && scheme !== 'postgresql:') {
      throw new ConfigError(
        `SCADBUDDY_DATABASE_URL must be a postgres:// or postgresql:// URL, not ${scheme}//`,
      )
    }
  }

  const backendUrl = present(env.SCADBUDDY_BACKEND_URL) ?? DEFAULT_BACKEND_URL
  let parsed: URL
  try {
    parsed = new URL(backendUrl)
  } catch {
    throw new ConfigError(`SCADBUDDY_BACKEND_URL is not a valid URL: ${backendUrl}`)
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new ConfigError(`SCADBUDDY_BACKEND_URL must be http(s), not ${parsed.protocol}//`)
  }

  return {
    databaseUrl,
    // No trailing slash, so `${backendUrl}/api/v1/...` never doubles it.
    backendUrl: backendUrl.replace(/\/+$/, ''),
    secretKeyFile: present(env.SCADBUDDY_SECRET_KEY_FILE),
  }
}
