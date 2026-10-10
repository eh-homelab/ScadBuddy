import { describe, expect, it } from 'vitest'
import { principalLabel, toolTitle } from './labels'

describe('toolTitle (#782)', () => {
  it('uses the title the tool declared', () => {
    expect(toolTitle({ name: 'mcp__scadbuddy__render_model', input: { slug: 'x' }, title: 'Render cable-clip' })).toBe(
      'Render cable-clip',
    )
  })

  it('names a ScadBuddy tool without one in words, with its slug', () => {
    expect(toolTitle({ name: 'mcp__scadbuddy__get_readme', input: { slug: 'cable-clip' } })).toBe('Get readme → cable-clip')
    expect(toolTitle({ name: 'mcp__scadbuddy__list_fonts', input: {} })).toBe('List fonts')
  })

  it('names a plugin’s tool with its server, and Claude Code’s built-ins by what they act on', () => {
    expect(toolTitle({ name: 'mcp__hindsight__recall', input: {} })).toBe('Recall (hindsight)')
    expect(toolTitle({ name: 'Agent', input: { description: 'Check the fit', subagent_type: 'fit' } })).toBe(
      'Subagent: Check the fit',
    )
    expect(toolTitle({ name: 'Agent', input: {} })).toBe('Subagent')
    expect(toolTitle({ name: 'Skill', input: { skill: 'customize' } })).toBe('Skill: customize')
    expect(toolTitle({ name: 'AskUserQuestion', input: {} })).toBe('Ask you')
    expect(toolTitle({ name: 'browser_navigate', input: {} })).toBe('Browser navigate')
  })
})

describe('principalLabel (#1288)', () => {
  it('names each kind of principal an agent revision can carry', () => {
    expect(principalLabel('browser')).toBe('you')
    expect(principalLabel('token:ci-bot')).toBe('MCP token ci-bot')
    expect(principalLabel('oidc:https://id.example.com/realms/home#alice')).toBe('OIDC user alice')
    expect(principalLabel('anonymous:3f2a')).toBe('an anonymous MCP client')
    expect(principalLabel(null)).toBe('an unknown caller')
    expect(principalLabel('something-else')).toBe('something-else')
  })
})
