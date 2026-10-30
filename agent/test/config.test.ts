import { describe, expect, it } from 'vitest'
import { ConfigError, DEFAULT_BACKEND_URL, ENV_VARS, loadConfig } from '../src/config.js'

describe('loadConfig', () => {
  it('defaults everything when the environment is empty', () => {
    expect(loadConfig({})).toEqual({
      databaseUrl: undefined,
      backendUrl: DEFAULT_BACKEND_URL,
      secretKeyFile: undefined,
      previousSecretKeyFile: undefined,
      publicUrl: undefined,
      allowedOrigins: undefined,
      trustedProxies: undefined,
      temporalAddress: undefined,
      temporalNamespace: 'scadbuddy',
      temporalSearchAttributes: false,
      payloadDir: undefined,
      agentRole: 'all',
    })
  })

  it('reads the variables it owns', () => {
    expect(
      loadConfig({
        SCADBUDDY_DATABASE_URL: 'postgresql://u:p@db:5432/scadbuddy',
        SCADBUDDY_BACKEND_URL: 'http://localhost:9000/',
        SCADBUDDY_SECRET_KEY_FILE: '/run/secrets/kek',
        SCADBUDDY_SECRET_KEY_PREVIOUS_FILE: '/run/secrets/kek-old',
        SCADBUDDY_PUBLIC_URL: 'https://scadbuddy.example',
        SCADBUDDY_ALLOWED_ORIGINS: 'https://scadbuddy.internal.example, https://scadbuddy.lan',
        SCADBUDDY_AGENT_TRUSTED_PROXIES: '10.42.0.0/16, fd00::/8',
        SCADBUDDY_TEMPORAL_ADDRESS: 'temporal-frontend:7233',
        SCADBUDDY_TEMPORAL_NAMESPACE: 'scadbuddy-dev',
        SCADBUDDY_TEMPORAL_SEARCH_ATTRIBUTES: 'true',
        SCADBUDDY_PAYLOAD_DIR: '/data/payloads',
        SCADBUDDY_AGENT_ROLE: 'worker',
      }),
    ).toEqual({
      databaseUrl: 'postgresql://u:p@db:5432/scadbuddy',
      backendUrl: 'http://localhost:9000',
      secretKeyFile: '/run/secrets/kek',
      previousSecretKeyFile: '/run/secrets/kek-old',
      publicUrl: 'https://scadbuddy.example',
      allowedOrigins: 'https://scadbuddy.internal.example, https://scadbuddy.lan',
      trustedProxies: '10.42.0.0/16, fd00::/8',
      temporalAddress: 'temporal-frontend:7233',
      temporalNamespace: 'scadbuddy-dev',
      temporalSearchAttributes: true,
      payloadDir: '/data/payloads',
      agentRole: 'worker',
    })
  })

  it('reads the Temporal switch as the backend does, and refuses whitespace inside a Temporal value', () => {
    expect(loadConfig({ SCADBUDDY_TEMPORAL_SEARCH_ATTRIBUTES: '1' }).temporalSearchAttributes).toBe(true)
    expect(loadConfig({ SCADBUDDY_TEMPORAL_SEARCH_ATTRIBUTES: 'false' }).temporalSearchAttributes).toBe(false)
    expect(() => loadConfig({ SCADBUDDY_TEMPORAL_SEARCH_ATTRIBUTES: 'yes please' })).toThrow(ConfigError)
    expect(() => loadConfig({ SCADBUDDY_TEMPORAL_ADDRESS: 'temporal frontend:7233' })).toThrow(
      /SCADBUDDY_TEMPORAL_ADDRESS/,
    )
    expect(() => loadConfig({ SCADBUDDY_TEMPORAL_NAMESPACE: 'a b' })).toThrow(/SCADBUDDY_TEMPORAL_NAMESPACE/)
  })

  it.each([
    [{ SCADBUDDY_PUBLIC_URL: 'scadbuddy.example' }, /SCADBUDDY_PUBLIC_URL must be an http\(s\) URL/],
    [{ SCADBUDDY_PUBLIC_URL: 'ftp://scadbuddy.example' }, /SCADBUDDY_PUBLIC_URL/],
    [
      { SCADBUDDY_ALLOWED_ORIGINS: 'https://scadbuddy.internal.example, scadbuddy.lan' },
      /SCADBUDDY_ALLOWED_ORIGINS: "scadbuddy\.lan" is not an http\(s\) origin/,
    ],
    [{ SCADBUDDY_AGENT_TRUSTED_PROXIES: '10.0.0.0/33' }, /"10\.0\.0\.0\/33" is not an IP address or CIDR range/],
    [{ SCADBUDDY_AGENT_TRUSTED_PROXIES: 'ingress-nginx' }, /SCADBUDDY_AGENT_TRUSTED_PROXIES/],
    [{ SCADBUDDY_AGENT_TRUSTED_PROXIES: '10.0.0.0/8/1' }, /SCADBUDDY_AGENT_TRUSTED_PROXIES/],
  ])('refuses a malformed %j', (env, message) => {
    expect(() => loadConfig(env)).toThrow(ConfigError)
    expect(() => loadConfig(env)).toThrow(message)
  })

  // Spec 2026-10-13 §14.3, plan Step 6.1: one image, run as the HTTP half, the worker half or both.
  it.each(['all', 'api', 'worker'] as const)('reads the role %s', (role) => {
    expect(loadConfig({ SCADBUDDY_AGENT_ROLE: role }).agentRole).toBe(role)
    expect(loadConfig({ SCADBUDDY_AGENT_ROLE: ` ${role} ` }).agentRole).toBe(role)
  })

  it('refuses any other role, naming it', () => {
    expect(() => loadConfig({ SCADBUDDY_AGENT_ROLE: 'workers' })).toThrow(ConfigError)
    expect(() => loadConfig({ SCADBUDDY_AGENT_ROLE: 'workers' })).toThrow(
      'SCADBUDDY_AGENT_ROLE must be all, api or worker, not "workers"',
    )
  })

  it('treats blank values as unset', () => {
    expect(
      loadConfig({ SCADBUDDY_DATABASE_URL: '  ', SCADBUDDY_BACKEND_URL: '', SCADBUDDY_SECRET_KEY_FILE: '' }),
    ).toEqual(loadConfig({}))
  })

  it('accepts both postgres URL schemes', () => {
    expect(loadConfig({ SCADBUDDY_DATABASE_URL: 'postgres://db/x' }).databaseUrl).toBe('postgres://db/x')
  })

  it('rejects a non-postgres database URL without echoing it', () => {
    expect(() => loadConfig({ SCADBUDDY_DATABASE_URL: 'mysql://u:secret@db/x' })).toThrow(ConfigError)
    expect(() => loadConfig({ SCADBUDDY_DATABASE_URL: 'not a url secret' })).toThrow(
      /^SCADBUDDY_DATABASE_URL is not a valid URL$/,
    )
  })

  it('rejects a backend URL that is not http(s)', () => {
    expect(() => loadConfig({ SCADBUDDY_BACKEND_URL: 'ftp://backend' })).toThrow(ConfigError)
    expect(() => loadConfig({ SCADBUDDY_BACKEND_URL: '::' })).toThrow(ConfigError)
  })

  it('reads no variable outside its declared list (spec §9: no AI env vars)', () => {
    const read = new Set<string>()
    const env = new Proxy(
      {},
      {
        get(_target, key) {
          if (typeof key === 'string') read.add(key)
          return undefined
        },
      },
    )
    loadConfig(env)
    expect([...read].sort()).toEqual([...ENV_VARS].sort())
  })
})
