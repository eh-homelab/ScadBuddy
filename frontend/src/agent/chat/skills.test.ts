import type { BuiltInPluginPackage, ListedPackage, PackageReview, PluginPackage } from '../../api/aiPlugins'
import { loadedSkills, matchingSkills, skillInvocation, slashQuery } from './skills'

const review = (name: string, skills: string[]): PackageReview => ({
  name,
  description: null,
  version: null,
  skills,
  commands: [],
  agents: [],
  hooks: [],
  mcp_servers: [],
  files: [],
})

const builtIn = (name: string, skills: string[], enabled = true): BuiltInPluginPackage => ({
  name,
  built_in: true,
  source: { kind: 'built_in', path: `agent/plugins/${name}` },
  review: review(name, skills),
  approved: true,
  enabled,
})

const installed = (name: string, skills: string[], over: Partial<PluginPackage> = {}): PluginPackage => ({
  name,
  source: { kind: 'git', url: 'https://example.com/p.git', ref: 'main', path: '' },
  commit_sha: 'a'.repeat(40),
  content_hash: 'sha256:x',
  review: review(name, skills),
  approved: true,
  approved_at: '2026-10-01T00:00:00Z',
  allow_refused: false,
  enabled: true,
  pending: null,
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
  ...over,
})

describe('slashQuery', () => {
  it('is what follows a "/" at the start of the draft', () => {
    expect(slashQuery('/')).toBe('')
    expect(slashQuery('/cust')).toBe('cust')
    expect(slashQuery('/scadbuddy:pr')).toBe('scadbuddy:pr')
  })

  it('is null once the draft has a space, or does not start with "/"', () => {
    expect(slashQuery('')).toBeNull()
    expect(slashQuery('hello /cust')).toBeNull()
    expect(slashQuery(' /cust')).toBeNull()
    expect(slashQuery('/scadbuddy:print ')).toBeNull()
    expect(slashQuery('/a\nb')).toBeNull()
  })
})

describe('loadedSkills', () => {
  it('lists the skills of enabled built-ins and enabled, approved packages, in order', () => {
    const packages: ListedPackage[] = [
      builtIn('scadbuddy', ['scadbuddy:authoring', 'scadbuddy:customize']),
      builtIn('playwright', ['playwright:off'], false),
      installed('greeter', ['greeter:hello']),
      installed('waiting', ['waiting:x'], { approved: false, enabled: false }),
      installed('off', ['off:y'], { enabled: false }),
    ]
    expect(loadedSkills(packages)).toEqual([
      { name: 'scadbuddy:authoring', plugin: 'scadbuddy' },
      { name: 'scadbuddy:customize', plugin: 'scadbuddy' },
      { name: 'greeter:hello', plugin: 'greeter' },
    ])
  })

  it('leaves out a package stored under a built-in name, which the harness never loads', () => {
    const packages: ListedPackage[] = [builtIn('scadbuddy', ['scadbuddy:print']), installed('scadbuddy', ['scadbuddy:old'])]
    expect(loadedSkills(packages).map((s) => s.name)).toEqual(['scadbuddy:print'])
  })
})

describe('matchingSkills', () => {
  const skills = [
    { name: 'scadbuddy:authoring', plugin: 'scadbuddy' },
    { name: 'scadbuddy:customize', plugin: 'scadbuddy' },
    { name: 'scadbuddy:print', plugin: 'scadbuddy' },
  ]

  it('keeps every skill for an empty query', () => {
    expect(matchingSkills(skills, '')).toEqual(skills)
  })

  it('matches the start of the name, or the skill anywhere, ignoring case, prefixes first', () => {
    expect(matchingSkills(skills, 'PRI').map((s) => s.name)).toEqual(['scadbuddy:print'])
    expect(matchingSkills(skills, 'scadbuddy:c').map((s) => s.name)).toEqual(['scadbuddy:customize'])
    // The plugin's own name does not match every skill of it.
    expect(matchingSkills(skills, 'c').map((s) => s.name)).toEqual(['scadbuddy:customize'])
    expect(matchingSkills(skills, 'scad').map((s) => s.name)).toEqual(skills.map((s) => s.name))
    expect(matchingSkills(skills, 'int').map((s) => s.name)).toEqual(['scadbuddy:print'])
    expect(matchingSkills([...skills, { name: 'tools:t', plugin: 'tools' }], 't').map((s) => s.name)).toEqual([
      'tools:t',
      'scadbuddy:authoring',
      'scadbuddy:customize',
      'scadbuddy:print',
    ])
  })
})

describe('skillInvocation', () => {
  it('is the slash command Claude Code runs the skill by, with a space to go on typing', () => {
    expect(skillInvocation({ name: 'scadbuddy:print', plugin: 'scadbuddy' })).toBe('/scadbuddy:print ')
  })
})
