import { describe, expect, it } from 'vitest'
import { readArchive } from '../src/plugins/packages/unpack.js'
import { storedZip } from '../src/plugins/packages/zip.js'

// The feed's zip of an npm pin (#6936, plugins/packages/zip.ts): the same files give the same
// bytes, whatever their order, and the installer's own reader takes them back unchanged.

const FILES = [
  { path: 'skills/hi/SKILL.md', data: Buffer.from('---\nname: hi\n---\n'), executable: false },
  { path: 'skills/hi/run.sh', data: Buffer.from('echo hi\n'), executable: true },
  { path: 'ünï.md', data: Buffer.alloc(0), executable: false },
]

describe('storedZip', () => {
  it('gives the same bytes for the same files in any order', () => {
    expect(storedZip(FILES)).toEqual(storedZip([...FILES].reverse()))
  })

  it('reads back as the files it was given, with their modes', () => {
    const entries = readArchive(storedZip(FILES))
    expect(entries.map((e) => [e.path, e.executable, e.data!.toString()])).toEqual([
      ['skills/hi/SKILL.md', false, '---\nname: hi\n---\n'],
      ['skills/hi/run.sh', true, 'echo hi\n'],
      ['ünï.md', false, ''],
    ])
  })
})
