import { describe, expect, it } from 'vitest'
import type { PluginPackage } from '../../api/aiPlugins'
import {
  asText,
  detailsProblem,
  fileTree,
  languageOf,
  MAX_TAGS,
  offerState,
  parseTags,
  pathProblem,
  publishedByText,
  skillNameProblem,
} from './hostedDraft'

describe('pathProblem', () => {
  it.each([
    ['skills/hello/SKILL.md', null],
    ['agents/helper.md', null],
    ['commands/wave.md', null],
    ['README.md', 'A file must be under skills/, agents/ or commands/.'],
    ['hooks/hooks.json', 'A file must be under skills/, agents/ or commands/.'],
    ['skills/../x.md', 'No file or folder name may start with "." (no hidden files, no "..").'],
    ['skills/.hidden', 'No file or folder name may start with "." (no hidden files, no "..").'],
    ['/skills/a.md', "Write the path from the plugin's root, without a leading /."],
    ['skills//a.md', 'The path has an empty folder name (two / in a row, or a / at the end).'],
    ['skills\\a.md', 'Separate folders with /, not \\.'],
    ['', 'Enter a path, such as skills/my-skill/SKILL.md.'],
  ])('%s', (path, problem) => {
    expect(pathProblem(path)).toBe(problem)
  })

  it('lets only a file under skills/ be executable', () => {
    expect(pathProblem('skills/hello/run.sh', true)).toBeNull()
    expect(pathProblem('commands/run.sh', true)).toBe('Only a file under skills/ can be executable.')
  })
})

describe('skillNameProblem', () => {
  it('takes a lower-case name with hyphens only', () => {
    expect(skillNameProblem('print-notes')).toBeNull()
    expect(skillNameProblem('Print notes')).not.toBeNull()
  })
})

describe('fileTree', () => {
  it('puts folders first and sorts each level', () => {
    expect(fileTree(['skills/b/SKILL.md', 'commands/wave.md', 'skills/a/run.sh', 'skills/a/SKILL.md'])).toEqual([
      { kind: 'folder', name: 'commands', path: 'commands', children: [{ kind: 'file', name: 'wave.md', path: 'commands/wave.md' }] },
      {
        kind: 'folder',
        name: 'skills',
        path: 'skills',
        children: [
          {
            kind: 'folder',
            name: 'a',
            path: 'skills/a',
            children: [
              { kind: 'file', name: 'run.sh', path: 'skills/a/run.sh' },
              { kind: 'file', name: 'SKILL.md', path: 'skills/a/SKILL.md' },
            ],
          },
          { kind: 'folder', name: 'b', path: 'skills/b', children: [{ kind: 'file', name: 'SKILL.md', path: 'skills/b/SKILL.md' }] },
        ],
      },
    ])
  })
})

describe('languageOf and asText', () => {
  it('picks the editor language by extension', () => {
    expect(languageOf('skills/a/SKILL.md')).toBe('markdown')
    expect(languageOf('skills/a/run.sh')).toBe('shell')
    expect(languageOf('skills/a/tool.py')).toBe('python')
    expect(languageOf('skills/a/data.csv')).toBe('plaintext')
  })

  it('reads UTF-8 and refuses other bytes', () => {
    expect(asText(new TextEncoder().encode('héllo'))).toBe('héllo')
    expect(asText(new Uint8Array([0xff, 0xfe, 0x00]))).toBeNull()
  })
})

describe('offerState', () => {
  const pkg = (patch: Partial<PluginPackage>): PluginPackage =>
    ({ name: 'notes', source: { kind: 'hosted', name: 'notes' }, version: 1, approved: true, enabled: true, pending: null, ...patch }) as PluginPackage

  it('names each step from publish to load', () => {
    expect(offerState(null, undefined)).toEqual({ kind: 'unpublished' })
    expect(offerState(2, undefined)).toEqual({ kind: 'not-offered', version: 2, loaded: null })
    expect(offerState(2, pkg({ version: 1 }))).toEqual({ kind: 'not-offered', version: 2, loaded: 1 })
    expect(offerState(2, pkg({ version: 2, approved: false, enabled: false }))).toEqual({ kind: 'awaiting-approval', version: 2 })
    expect(offerState(2, pkg({ version: 1, pending: { version: 2 } as PluginPackage['pending'] }))).toEqual({
      kind: 'repin-pending',
      version: 2,
      loaded: 1,
    })
    expect(offerState(2, pkg({ version: 2 }))).toEqual({ kind: 'offered', version: 2, enabled: true })
    expect(offerState(2, pkg({ source: { kind: 'git', url: 'https://git.example/x.git', ref: '', path: '' } }))).toEqual({
      kind: 'name-taken',
      source: 'git',
    })
  })
})

describe('the details and the publisher', () => {
  it('splits tags and says why a category or a tag is refused', () => {
    expect(parseTags(' notes, prints  notes,,')).toEqual(['notes', 'prints'])
    expect(detailsProblem('', [])).toBeNull()
    expect(detailsProblem('printing', ['notes', 'a-1'])).toBeNull()
    expect(detailsProblem('Printing', [])).toMatch(/category/)
    expect(detailsProblem('', ['-x'])).toBe('"-x" is not a tag. A tag is 1 to 32 lower-case letters, digits and hyphens.')
    expect(detailsProblem('', Array.from({ length: MAX_TAGS + 1 }, (_, i) => `t${i}`))).toBe('A plugin has at most 20 tags.')
  })

  it('names who published a version, and shows an id it does not know as it is', () => {
    const label = (p: string) => `label(${p})`
    expect(publishedByText('user:https://id.example#alice', 'user:https://id.example#alice', label)).toBe('you')
    expect(publishedByText('user:https://id.example#bob', 'user:https://id.example#alice', label)).toBe('user bob')
    expect(publishedByText('service:agent', null, label)).toBe('the assistant')
    expect(publishedByText('anonymous', null, label)).toBe('an anonymous caller')
    expect(publishedByText('user:odd', null, label)).toBe('user:odd')
    expect(publishedByText('token:7', null, label)).toBe('label(token:7)')
  })
})
