import { describe, expect, it } from 'vitest'
import type { Settings } from '../api/types'
import { settings as fixture } from '../mocks/fixtures'
import { temporalWorkflowUrl } from './temporal'

function settings(ui: string | null, namespace: string | null): Settings {
  const bootstrap = (fixture.bootstrap ?? []).filter((entry) => entry.name !== 'temporal_namespace')
  if (namespace !== null) {
    bootstrap.push({ name: 'temporal_namespace', env_var: 'SCADBUDDY_TEMPORAL_NAMESPACE', value: namespace, source: 'env', reason: '' })
  }
  return { ...fixture, temporal_ui_url: ui, bootstrap } as Settings
}

describe('temporalWorkflowUrl (#1293)', () => {
  it('deep-links the workflow under its namespace', () => {
    expect(temporalWorkflowUrl(settings('https://temporal.example/', 'scad buddy'), 'render-ab/c')).toBe(
      'https://temporal.example/namespaces/scad%20buddy/workflows/render-ab%2Fc',
    )
  })

  it('is null unless the URL, the namespace and the workflow are all known', () => {
    expect(temporalWorkflowUrl(undefined, 'render-1')).toBeNull()
    expect(temporalWorkflowUrl(settings(null, 'default'), 'render-1')).toBeNull()
    expect(temporalWorkflowUrl(settings('https://temporal.example', null), 'render-1')).toBeNull()
    expect(temporalWorkflowUrl(settings('https://temporal.example', 'default'), null)).toBeNull()
  })

  it('never links a URL that is not http(s)', () => {
    expect(temporalWorkflowUrl(settings('javascript:alert(1)', 'default'), 'render-1')).toBeNull()
  })
})
