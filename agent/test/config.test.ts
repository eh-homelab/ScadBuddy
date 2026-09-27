import { describe, expect, it } from 'vitest'
import { ConfigError, DEFAULT_BACKEND_URL, ENV_VARS, loadConfig } from '../src/config.js'

describe('loadConfig', () => {
  it('defaults everything when the environment is empty', () => {
    expect(loadConfig({})).toEqual({
      databaseUrl: undefined,
      backendUrl: DEFAULT_BACKEND_URL,
      secretKeyFile: undefined,
    })
  })

  it('reads the three variables it owns', () => {
    expect(
      loadConfig({
        SCADBUDDY_DATABASE_URL: 'postgresql://u:p@db:5432/scadbuddy',
        SCADBUDDY_BACKEND_URL: 'http://localhost:9000/',
        SCADBUDDY_SECRET_KEY_FILE: '/run/secrets/kek',
      }),
    ).toEqual({
      databaseUrl: 'postgresql://u:p@db:5432/scadbuddy',
      backendUrl: 'http://localhost:9000',
      secretKeyFile: '/run/secrets/kek',
    })
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
