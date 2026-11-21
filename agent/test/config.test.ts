import { describe, expect, it } from 'vitest'
import { ConfigError, DEFAULT_BACKEND_URL, ENV_VARS, loadConfig } from '../src/config.js'

/** A proof, which every start needs (#5299 step 12). */
const PROOF = { SCADBUDDY_SERVICE_BOOTSTRAP_FILE: '/run/bootstrap/agent' }
const load = (env: Parameters<typeof loadConfig>[0] = {}) => loadConfig({ ...PROOF, ...env })

describe('loadConfig', () => {
  it('defaults everything when the environment is empty', () => {
    expect(load({})).toEqual({
      ignored: [],
      backendUrl: DEFAULT_BACKEND_URL,
      serviceBootstrapFile: '/run/bootstrap/agent',
      publicUrl: undefined,
      allowedOrigins: undefined,
      trustedProxies: undefined,
      temporalAddress: undefined,
      temporalNamespace: 'scadbuddy',
      temporalSearchAttributes: false,
      payloadDir: undefined,
      agentRole: 'all',
      authMode: 'off',
    })
  })

  it('reads the variables it owns, and names the database and KEK ones it ignores (#5299 step 12)', () => {
    expect(
      load({
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
        SCADBUDDY_SESSION_DIR: '/var/lib/scadbuddy-sessions/',
        SCADBUDDY_AGENT_ROLE: 'worker',
        SCADBUDDY_AUTH_MODE: 'observe',
      }),
    ).toEqual({
      ignored: ['SCADBUDDY_DATABASE_URL', 'SCADBUDDY_SECRET_KEY_FILE', 'SCADBUDDY_SECRET_KEY_PREVIOUS_FILE'],
      backendUrl: 'http://localhost:9000',
      serviceBootstrapFile: '/run/bootstrap/agent',
      publicUrl: 'https://scadbuddy.example',
      allowedOrigins: 'https://scadbuddy.internal.example, https://scadbuddy.lan',
      trustedProxies: '10.42.0.0/16, fd00::/8',
      temporalAddress: 'temporal-frontend:7233',
      temporalNamespace: 'scadbuddy-dev',
      temporalSearchAttributes: true,
      payloadDir: '/data/payloads',
      sessionDir: '/var/lib/scadbuddy-sessions',
      agentRole: 'worker',
      authMode: 'observe',
    })
  })

  it('reads one service-credential proof, and refuses both or none (#5494, #5299 step 12)', () => {
    expect(loadConfig({ SCADBUDDY_SERVICE_TOKEN_FILE: '/var/run/scadbuddy/token' }).serviceTokenFile).toBe(
      '/var/run/scadbuddy/token',
    )
    expect(loadConfig({ SCADBUDDY_SERVICE_BOOTSTRAP_FILE: '/run/bootstrap/agent' }).serviceBootstrapFile).toBe(
      '/run/bootstrap/agent',
    )
    expect(() =>
      loadConfig({ SCADBUDDY_SERVICE_TOKEN_FILE: '/a', SCADBUDDY_SERVICE_BOOTSTRAP_FILE: '/b' }),
    ).toThrow(ConfigError)
    expect(() => loadConfig({})).toThrow(/SCADBUDDY_SERVICE_TOKEN_FILE .* or SCADBUDDY_SERVICE_BOOTSTRAP_FILE/)
  })

  it('takes SCADBUDDY_AUTH_MODE=enforce, and refuses an unknown mode', () => {
    expect(load({ SCADBUDDY_AUTH_MODE: 'enforce' }).authMode).toBe('enforce')
    expect(() => load({ SCADBUDDY_AUTH_MODE: 'strict' })).toThrow(ConfigError)
  })

  it('reads the Temporal switch as the backend does, and refuses whitespace inside a Temporal value', () => {
    expect(load({ SCADBUDDY_TEMPORAL_SEARCH_ATTRIBUTES: '1' }).temporalSearchAttributes).toBe(true)
    expect(load({ SCADBUDDY_TEMPORAL_SEARCH_ATTRIBUTES: 'false' }).temporalSearchAttributes).toBe(false)
    expect(() => load({ SCADBUDDY_TEMPORAL_SEARCH_ATTRIBUTES: 'yes please' })).toThrow(ConfigError)
    expect(() => load({ SCADBUDDY_TEMPORAL_ADDRESS: 'temporal frontend:7233' })).toThrow(
      /SCADBUDDY_TEMPORAL_ADDRESS/,
    )
    expect(() => load({ SCADBUDDY_TEMPORAL_NAMESPACE: 'a b' })).toThrow(/SCADBUDDY_TEMPORAL_NAMESPACE/)
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
    [{ SCADBUDDY_SESSION_DIR: 'sessions' }, /SCADBUDDY_SESSION_DIR must be an absolute path, not "sessions"/],
  ])('refuses a malformed %j', (env, message) => {
    expect(() => load(env)).toThrow(ConfigError)
    expect(() => load(env)).toThrow(message)
  })

  // Spec 2026-10-13 §14.3, plan Step 6.1: one image, run as the HTTP half, the worker half or both.
  it.each(['all', 'api', 'worker'] as const)('reads the role %s', (role) => {
    expect(load({ SCADBUDDY_AGENT_ROLE: role }).agentRole).toBe(role)
    expect(load({ SCADBUDDY_AGENT_ROLE: ` ${role} ` }).agentRole).toBe(role)
  })

  it('refuses any other role, naming it', () => {
    expect(() => load({ SCADBUDDY_AGENT_ROLE: 'workers' })).toThrow(ConfigError)
    expect(() => load({ SCADBUDDY_AGENT_ROLE: 'workers' })).toThrow(
      'SCADBUDDY_AGENT_ROLE must be all, api or worker, not "workers"',
    )
  })

  it('treats blank values as unset', () => {
    expect(
      load({ SCADBUDDY_DATABASE_URL: '  ', SCADBUDDY_BACKEND_URL: '', SCADBUDDY_SECRET_KEY_FILE: '' }),
    ).toEqual(load({}))
  })

  it('ignores a database URL, whatever it holds, and never keeps its value (#5299 step 12)', () => {
    const config = load({ SCADBUDDY_DATABASE_URL: 'mysql://u:secret@db/x' })
    expect(config.ignored).toEqual(['SCADBUDDY_DATABASE_URL'])
    expect(JSON.stringify(config)).not.toContain('secret@db')
  })

  it('rejects a backend URL that is not http(s)', () => {
    expect(() => load({ SCADBUDDY_BACKEND_URL: 'ftp://backend' })).toThrow(ConfigError)
    expect(() => load({ SCADBUDDY_BACKEND_URL: '::' })).toThrow(ConfigError)
  })

  it('reads no variable outside its declared list (spec §9: no AI env vars)', () => {
    const read = new Set<string>()
    const env = new Proxy(
      {},
      {
        get(_target, key) {
          if (typeof key === 'string') read.add(key)
          return key === 'SCADBUDDY_SERVICE_BOOTSTRAP_FILE' ? '/run/bootstrap/agent' : undefined
        },
      },
    )
    loadConfig(env)
    expect([...read].sort()).toEqual([...ENV_VARS].sort())
  })
})
