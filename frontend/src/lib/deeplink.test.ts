import { describe, expect, it } from 'vitest'
import { modelPath, sourceFilePath } from './deeplink'

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

describe('sourceFilePath', () => {
  it("opens model.scad on the model's Edit source page, and any other file after it (#1290)", () => {
    expect(sourceFilePath('box', 'model.scad')).toBe('/m/box/source')
    expect(sourceFilePath('box', 'parts.scad')).toBe('/m/box/source/parts.scad')
    expect(sourceFilePath('builtin:box', 'lid-v2.scad')).toBe('/m/builtin%3Abox/source/lid-v2.scad')
  })
})
