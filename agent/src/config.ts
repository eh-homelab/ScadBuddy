import { BrowserOriginsError, parseBrowserAllowedOrigins } from './harness/browserOrigins.js'
import { OriginConfigError, originPolicy } from './http/origins.js'

// The agent service's whole environment surface. Design spec §9
// (docs/superpowers/specs/2026-09-27-ai-integration-design.md): "There are no
// AI-*configuration* env vars ... infrastructure bootstrap variables still
// reach the agent container, because Settings itself needs them to exist."
// Every AI-specific setting (credentials, MCP auth mode, plugins) lives in the
// database and is edited in Settings, so do not add a variable here for one.
//
// What is here is infrastructure: where the database and backend are, the
// key-encryption key (and the previous one while rotating it), and how the pod
// is reached (its public URL and the proxies in front of it). The last two
// cannot live in Settings: they decide which requests may change Settings.
// SCADBUDDY_BROWSER_ALLOWED_ORIGINS is here for the same reason: it decides
// what on the network the headless browser inside the pod may reach
// (harness/browserOrigins.ts), which is the operator's call, not a setting a
// request could change.

export type Config = {
  /** Postgres URL shared with the backend (#241). Unset → AI features are disabled. */
  databaseUrl: string | undefined
  /** Where the Python backend listens; the sidecar reaches it over the pod's localhost (spec §4.3). */
  backendUrl: string
  /** Key-encryption key file for envelope encryption (spec §9); format and loading in secrets.ts. */
  secretKeyFile: string | undefined
  /** The key being rotated away from; rows sealed under it are re-wrapped at start (spec §9). */
  previousSecretKeyFile: string | undefined
  /**
   * The UI's public URL, the same SCADBUDDY_PUBLIC_URL the backend reads
   * (backend/scadbuddy/core/settings.py `public_url`). Its origin is the one
   * accepted on writes (src/http/origins.ts). Unset → loopback only.
   */
  publicUrl: string | undefined
  /**
   * Comma-separated origins the UI is also served under (the LAN hostname beside
   * an SSO proxy, say), accepted on writes like the public URL's; the backend
   * reads the same variable for its realtime socket. Unset → the public URL only.
   */
  allowedOrigins: string | undefined
  /** CIDR list of proxies whose X-Forwarded-* headers are believed. Unset → none. */
  trustedProxies: string | undefined
  /**
   * Origins beyond the backend's that the headless browser may open, each once
   * a human approves it for the session (harness/browserOrigins.ts): a
   * comma-separated list, or `*` for any. Unset → none.
   */
  browserAllowedOrigins: string | undefined
}

export const DEFAULT_BACKEND_URL = 'http://127.0.0.1:8080'

/** The only variable names this service reads. Tests assert nothing else is consulted. */
export const ENV_VARS = [
  'SCADBUDDY_DATABASE_URL',
  'SCADBUDDY_BACKEND_URL',
  'SCADBUDDY_SECRET_KEY_FILE',
  'SCADBUDDY_SECRET_KEY_PREVIOUS_FILE',
  'SCADBUDDY_PUBLIC_URL',
  'SCADBUDDY_ALLOWED_ORIGINS',
  'SCADBUDDY_AGENT_TRUSTED_PROXIES',
  'SCADBUDDY_BROWSER_ALLOWED_ORIGINS',
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

  const publicUrl = present(env.SCADBUDDY_PUBLIC_URL)
  const allowedOrigins = present(env.SCADBUDDY_ALLOWED_ORIGINS)
  const trustedProxies = present(env.SCADBUDDY_AGENT_TRUSTED_PROXIES)
  try {
    originPolicy(publicUrl, trustedProxies, allowedOrigins)
  } catch (err) {
    if (err instanceof OriginConfigError) throw new ConfigError(err.message)
    throw err
  }
  const browserAllowedOrigins = present(env.SCADBUDDY_BROWSER_ALLOWED_ORIGINS)
  try {
    parseBrowserAllowedOrigins(browserAllowedOrigins)
  } catch (err) {
    if (err instanceof BrowserOriginsError) throw new ConfigError(err.message)
    throw err
  }

  return {
    databaseUrl,
    // No trailing slash, so `${backendUrl}/api/v1/...` never doubles it.
    backendUrl: backendUrl.replace(/\/+$/, ''),
    secretKeyFile: present(env.SCADBUDDY_SECRET_KEY_FILE),
    previousSecretKeyFile: present(env.SCADBUDDY_SECRET_KEY_PREVIOUS_FILE),
    publicUrl,
    allowedOrigins,
    trustedProxies,
    browserAllowedOrigins,
  }
}
