import { describe, expect, it } from 'vitest'
import { modelPath } from './deeplink'

describe('modelPath', () => {
  it('leaves a user model id as it is', () => {
    expect(modelPath('name-keychain')).toBe('/m/name-keychain')
    expect(modelPath('name-keychain', 'versions')).toBe('/m/name-keychain/versions')
  })

  it('encodes the colon in a built-in id, so it stays one path segment (#192)', () => {
    expect(modelPath('builtin:keychain-template')).toBe('/m/builtin%3Akeychain-template')
    expect(modelPath('builtin:keychain-template', 'source')).toBe(
      '/m/builtin%3Akeychain-template/source',
    )
  })
})
