import { screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { useLocation } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { bridge } from '../../agent/bridge'
import type { ConnectionTest } from '../../api/types'
import { mockSettings } from '../../mocks/handlers'
import { renderPage } from '../../test/utils'
import { SettingsAgentProvider, useSettingsAgentField, useSettingsConnectionAgent } from './agentFields'
import { SettingsCacheProvider, SettingsLoaded, useLoadedSettings } from './cache'
import { SettingsCollapseProvider } from './collapse'
import { Section } from './controls'
import { SettingsDirtyProvider, useReportDirty } from './dirty'
import type { SettingsSection } from './registry'
import { baseline } from './values'

const testStored = vi.fn(async (): Promise<ConnectionTest> => ({ ok: true, detail: 'Connected' }) as ConnectionTest)

/** The Connection section's stand-in: binds the Bambuddy URL and the connection test. */
function Here() {
  const settings = useLoadedSettings()
  const [url, setUrl] = useState(() => baseline(settings, 'bambuddy_url'))
  const dirty = url !== baseline(settings, 'bambuddy_url')
  useReportDirty('connection', dirty)
  useSettingsAgentField('bambuddy_url', { value: () => url, set: setUrl })
  useSettingsConnectionAgent({ testStored, lastTest: () => null, error: () => null, apiKeyTyped: () => false })
  return (
    <Section id="connection" title="Connection" dirty={dirty}>
      <label htmlFor="bambuddy-url">Bambuddy URL</label>
      <input id="bambuddy-url" value={url} onChange={(event) => setUrl(event.target.value)} />
    </Section>
  )
}

/** The Preview section's stand-in, in another area: binds the display unit. */
function There() {
  const settings = useLoadedSettings()
  const [unit, setUnit] = useState(() => baseline(settings, 'display_unit'))
  useReportDirty('preview', unit !== baseline(settings, 'display_unit'))
  useSettingsAgentField('display_unit', {
    value: () => unit,
    set: setUnit,
    allowed: () => ['mm', 'in'],
    listed: () => ['mm', 'in'],
  })
  return (
    <Section id="preview" title="Preview">
      <label htmlFor="display-unit">Show dimensions in</label>
      <input id="display-unit" value={unit} onChange={(event) => setUnit(event.target.value)} />
    </Section>
  )
}

const fakes: readonly SettingsSection[] = [
  { id: 'connection', area: 'general', title: 'Connection', order: 0, Component: Here },
  { id: 'preview', area: 'library', title: 'Preview', order: 0, Component: There },
]

function Where() {
  const { pathname } = useLocation()
  return <p data-testid="where">{pathname}</p>
}

/** Mounts only the current area's sections, as the page does. */
function Area() {
  const { pathname } = useLocation()
  return pathname === '/settings/library' ? <There /> : <Here />
}

function renderShell({ byArea = false } = {}) {
  const view = renderPage(
    <SettingsCollapseProvider>
      <SettingsDirtyProvider>
        <SettingsCacheProvider>
          <SettingsAgentProvider sections={fakes}>
            <Where />
            <SettingsLoaded>
              {byArea ? (
                <Area />
              ) : (
                <>
                  <Here />
                  <There />
                </>
              )}
            </SettingsLoaded>
          </SettingsAgentProvider>
        </SettingsCacheProvider>
      </SettingsDirtyProvider>
    </SettingsCollapseProvider>,
    { route: '/settings/general' },
  )
  return view
}

afterEach(() => {
  localStorage.clear()
  testStored.mockClear()
})

describe('the settings agent scope', () => {
  it('set_field in another area navigates there, unfolds it and keeps other drafts', async () => {
    localStorage.setItem('scadbuddy.settings.collapsed', JSON.stringify(['preview']))
    const { user } = renderShell()
    await user.type(await screen.findByLabelText('Bambuddy URL'), '/x')
    expect(screen.getByLabelText('Show dimensions in')).not.toBeVisible()

    expect(await bridge.call('set_field', { field: 'display_unit', value: 'in' })).toMatchObject({
      ok: true,
      result: { field: 'display_unit', value: 'in', saved: false, note: 'The user saves each section with its own Save button.' },
    })
    expect(screen.getByTestId('where')).toHaveTextContent('/settings/library')
    await waitFor(() => expect(screen.getByLabelText('Show dimensions in')).toBeVisible())
    expect(screen.getByLabelText('Bambuddy URL')).toHaveValue('https://bambuddy.internal.nullreference.io/x')
    expect(await bridge.call('get_form', {})).toMatchObject({
      ok: true,
      result: { unsaved: true, values: { display_unit: 'in' } },
    })
  })

  it('sets a field whose section mounts only once its area opens', async () => {
    renderShell({ byArea: true })
    await screen.findByLabelText('Bambuddy URL')
    expect(screen.queryByLabelText('Show dimensions in')).toBeNull()
    expect(await bridge.call('set_field', { field: 'display_unit', value: 'in' })).toMatchObject({ ok: true })
    expect(screen.getByTestId('where')).toHaveTextContent('/settings/library')
    expect(screen.getByLabelText('Show dimensions in')).toHaveValue('in')

    // The unsaved unit is in this area's section, and still refuses the test.
    const unsaved = await bridge.call('test_connection', {})
    expect(!unsaved.ok && unsaved.error.code).toBe('refused')
  })

  it('refuses a value that is not a choice', async () => {
    renderShell()
    await screen.findByLabelText('Show dimensions in')
    const bad = await bridge.call('set_field', { field: 'display_unit', value: 'cubits' })
    expect(!bad.ok && bad.error).toMatchObject({
      code: 'invalid_args',
      message: '"cubits" is not a choice for display_unit: "mm", "in".',
    })
    expect(screen.getByLabelText('Show dimensions in')).toHaveValue('mm')
    expect(screen.getByTestId('where')).toHaveTextContent('/settings/general')
  })

  it('reads a field with no binding from the saved settings', async () => {
    renderShell()
    await screen.findByLabelText('Bambuddy URL')
    const form = await bridge.call('get_form', {})
    expect(form).toMatchObject({
      ok: true,
      result: {
        unsaved: false,
        has_api_key: mockSettings().has_api_key,
        api_key_typed: false,
        values: {
          public_url: mockSettings().public_url ?? '',
          bambuddy_url: 'https://bambuddy.internal.nullreference.io',
          display_unit: 'mm',
        },
        choices: { display_unit: ['mm', 'in'] },
        last_test: null,
        error: null,
      },
    })
  })

  it('tests the stored connection when nothing is unsaved', async () => {
    renderShell()
    await screen.findByLabelText('Bambuddy URL')
    expect(await bridge.call('test_connection', {})).toMatchObject({ ok: true, result: { ok: true, detail: 'Connected' } })
    expect(testStored).toHaveBeenCalledOnce()
  })

  it('refuses test_connection while any section is unsaved', async () => {
    const { user } = renderShell()
    await user.type(await screen.findByLabelText('Bambuddy URL'), '/x')
    const dirty = await bridge.call('test_connection', {})
    expect(!dirty.ok && dirty.error).toMatchObject({
      code: 'refused',
      message: 'The form has unsaved changes, and the test saves the form first. Ask the user to save or test it.',
    })
    expect(testStored).not.toHaveBeenCalled()
  })

  it('answers a failed test as failed', async () => {
    testStored.mockRejectedValueOnce(new Error('boom'))
    renderShell()
    await screen.findByLabelText('Bambuddy URL')
    const failed = await bridge.call('test_connection', {})
    expect(!failed.ok && failed.error).toMatchObject({ code: 'failed', message: 'The connection test could not run.' })
  })
})
