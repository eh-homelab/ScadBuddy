import { describe, expect, it } from 'vitest'
import { activityKindsOf } from '../src/activity/kinds.js'

// ai_activity_kinds: the agent's activity kinds as data (spec 2026-10-09 §4).

describe('activityKindsOf', () => {
  it('prefixes the name and labels it from the kind or the spaced name', () => {
    expect(activityKindsOf([{ name: 'plugin_package_install', label: 'Install plugin package' }, { name: 'a_b' }])).toEqual([
      { key: 'agent.plugin_package_install', label: 'Install plugin package', facets: [] },
      { key: 'agent.a_b', label: 'a b', facets: [] },
    ])
  })
})
