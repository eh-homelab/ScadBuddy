import { screen, waitFor } from '@testing-library/react'
import { Route, Routes, useLocation } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import { bridge } from '../agent/bridge'
import { api } from '../api/client'
import { BUILTIN_SLUG, keychainSource } from '../mocks/fixtures'
import { useGlobalAgentTools } from '../agent/global'
import { renderPage } from '../test/utils'
import { CataloguePage } from './CataloguePage'
import { EditSourcePage } from './EditSourcePage'
import { SettingsPage } from './SettingsPage'

// As in EditSourcePage.test.tsx: Monaco does not run in jsdom, so a textarea stands in
// with the same value/onChange contract. With no editor handle, replace_range takes the
// `onSourceChange` path the editor's own edits take.
vi.mock('../components/SourceEditor', () => ({
  SourceEditor: ({
    value,
    onChange,
    label,
    readOnly,
  }: {
    value: string
    onChange: (next: string) => void
    label: string
    readOnly?: boolean
  }) => (
    <textarea
      aria-label={label}
      readOnly={readOnly}
      value={value}
      onChange={(event) => onChange(event.target.value)}
    />
  ),
}))

function Shell() {
  useGlobalAgentTools()
  return <SettingsPage />
}

function Where() {
  const { pathname } = useLocation()
  return <p data-testid="where">{pathname}</p>
}

describe('catalogue tools', () => {
  it('searches the catalogue and opens a model', async () => {
    renderPage(
      <Routes>
        <Route path="/" element={<CataloguePage />} />
        <Route path="/m/:slug" element={<Where />} />
      </Routes>,
    )
    await screen.findByRole('heading', { name: 'Gridfinity Bin' })

    const found = await bridge.call('search', { query: 'grid' })
    expect(found.ok && (found.result as { slug: string }[]).map((model) => model.slug)).toEqual([
      'gridfinity-bin',
    ])

    const missing = await bridge.call('open_model', { slug: 'nope' })
    expect(!missing.ok && missing.error.code).toBe('invalid_args')

    expect(await bridge.call('open_model', { slug: 'gridfinity-bin' })).toMatchObject({ ok: true })
    expect(await screen.findByTestId('where')).toHaveTextContent('/m/gridfinity-bin')
  })
})

function CatalogueShell() {
  useGlobalAgentTools()
  return <CataloguePage />
}

describe('catalogue filters through navigate (#276)', () => {
  it('sets the search, tags, origin and sort from the route', async () => {
    renderPage(<CatalogueShell />)
    await screen.findByRole('heading', { name: 'Gridfinity Bin' })

    expect(await bridge.call('navigate', { route: '/?tag=keychain&sort=name' })).toEqual({
      ok: true,
      result: { route: '/?tag=keychain&sort=name' },
    })
    await waitFor(() => expect(screen.queryByRole('heading', { name: 'Gridfinity Bin' })).toBeNull())
    expect(
      screen.getAllByRole('heading', { level: 2 }).map((heading) => heading.textContent),
    ).toEqual(['Keychain Template', 'Name Keychain'])
    expect(screen.getByTestId('result-count')).toHaveTextContent('2 of 4')
  })
})

function renderSource(slug = 'name-keychain') {
  return renderPage(<EditSourcePage />, { route: `/m/${slug}/source`, path: '/m/:slug/source' })
}

describe('source editor tools', () => {
  it('reads the text, replaces a range as an unsaved edit, and reports the problems', async () => {
    const replace = vi.spyOn(api, 'replaceSource')
    renderSource()
    const editor = await screen.findByLabelText('OpenSCAD source')

    const text = await bridge.call('get_editor_text', {})
    expect(text).toMatchObject({ ok: true, result: { text: keychainSource, read_only: false } })

    // Line 3 is `name = "Reagan";`: columns 9–15 are Reagan.
    expect(await bridge.call('replace_range', {
      start_line: 3,
      start_column: 9,
      end_line: 3,
      end_column: 15,
      text: 'Nova',
    })).toMatchObject({ ok: true, result: { saved: false } })
    expect(editor).toHaveValue(keychainSource.replace('"Reagan"', '"Nova"'))

    const clean = await bridge.call('get_problems', { timeout_ms: 5000 })
    expect(clean).toMatchObject({ ok: true, result: { current: true, ok: true, diagnostics: [] } })

    // Break line 3 the way the mock's parser notices: a bracket never closed.
    await bridge.call('replace_range', { start_line: 3, start_column: 1, end_line: 3, end_column: 1, text: 'x = [1, 2;\n' })
    const broken = await bridge.call('get_problems', { timeout_ms: 5000 })
    expect(broken).toMatchObject({ ok: true, result: { current: true, ok: false } })
    expect((broken as { result: { diagnostics: { line: number }[] } }).result.diagnostics[0]?.line).toBe(3)

    const past = await bridge.call('replace_range', { start_line: 999, start_column: 1, end_line: 999, end_column: 1, text: '' })
    expect(!past.ok && past.error.code).toBe('invalid_args')

    // Nothing was saved: that is the user's Save.
    expect(replace).not.toHaveBeenCalled()
  })

  it('refuses to edit a built-in template', async () => {
    renderSource(BUILTIN_SLUG)
    await screen.findByLabelText('OpenSCAD source')
    const outcome = await bridge.call('replace_range', {
      start_line: 1,
      start_column: 1,
      end_line: 1,
      end_column: 1,
      text: '// hi\n',
    })
    expect(!outcome.ok && outcome.error.code).toBe('refused')
  })
})

describe('settings tools', () => {
  async function seeded() {
    renderPage(<Shell />)
    await waitFor(() =>
      expect(screen.getByLabelText('Bambuddy URL')).toHaveValue('https://bambuddy.internal.nullreference.io'),
    )
  }

  it('reads the form without the key, and sets a field without saving', async () => {
    const put = vi.spyOn(api, 'putSettings')
    await seeded()

    const form = await bridge.call('get_form', {})
    expect(form).toMatchObject({
      ok: true,
      result: {
        unsaved: false,
        has_api_key: true,
        values: { bambuddy_url: 'https://bambuddy.internal.nullreference.io' },
      },
    })
    expect(JSON.stringify(form)).not.toMatch(/api_key"\s*:\s*"/)

    expect(await bridge.call('set_field', { field: 'display_unit', value: 'in' })).toMatchObject({
      ok: true,
      result: { saved: false },
    })
    expect(screen.getByLabelText('Show dimensions in')).toHaveValue('in')
    expect(put).not.toHaveBeenCalled()

    const bad = await bridge.call('set_field', { field: 'display_unit', value: 'cubits' })
    expect(!bad.ok && bad.error.code).toBe('invalid_args')
    // The API key is not a field at all.
    const key = await bridge.call('set_field', { field: 'bambuddy_api_key', value: 'x' })
    expect(!key.ok && key.error.code).toBe('invalid_args')
  })

  it('tests the stored connection, and refuses while the form is unsaved', async () => {
    const put = vi.spyOn(api, 'putSettings')
    await seeded()

    expect(await bridge.call('test_connection', {})).toMatchObject({ ok: true, result: { ok: true } })

    await bridge.call('set_field', { field: 'public_url', value: 'https://elsewhere.example' })
    const dirty = await bridge.call('test_connection', {})
    expect(!dirty.ok && dirty.error.code).toBe('refused')
    expect(put).not.toHaveBeenCalled()

    // Save is the user's: the fallback click will not press it either.
    const save = await bridge.call('click', { role: 'button', name: 'Save Connection' })
    expect(!save.ok && save.error.code).toBe('refused')
    const key = await bridge.call('fill', { label: 'API key', value: 'secret' })
    expect(!key.ok && key.error.code).toBe('refused')
    expect(put).not.toHaveBeenCalled()
  })
})
