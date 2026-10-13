import { screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Activity } from '../../api/types'
import { seedActivity } from '../../mocks/features/activity'
import { mockSettings, resetMockState, setMockSettings } from '../../mocks/handlers'
import { renderPage } from '../../test/utils'
import { ActivityDrawer } from './ActivityDrawer'

function failedImport(extra: Partial<Activity> = {}): Activity {
  return {
    id: 'operation:o1',
    kind: 'operation.model_import',
    status: 'failed',
    title: 'Import a model',
    principal: 'token:t1',
    session: null,
    via_tool: 'import_model',
    subject: '',
    parent_id: null,
    detail: { summary: 'could not fetch', error: { detail: 'could not fetch' }, request: { url: 'https://x' }, stages: [{ name: 'Fetch', status: 'succeeded' }] },
    workflow_id: 'op-model_import-1',
    run_id: 'run-1',
    created_at: '2026-10-09T10:01:00Z',
    updated_at: '2026-10-09T10:01:30Z',
    finished_at: '2026-10-09T10:01:30Z',
    ...extra,
  }
}

describe('ActivityDrawer', () => {
  beforeEach(() => {
    resetMockState()
    window.localStorage.clear()
    seedActivity([failedImport()])
    const settings = mockSettings()
    setMockSettings({
      ...settings,
      temporal_ui_url: 'https://temporal.example',
      bootstrap: [...(settings.bootstrap ?? []), { name: 'temporal_namespace', value: 'default' }],
    } as typeof settings)
  })

  it('Simple shows status, stages and the error in words; Advanced adds ids and raw JSON', async () => {
    const { user } = renderPage(<ActivityDrawer id="operation:o1" onClose={() => {}} />)
    expect(await screen.findByText('Failed')).toBeInTheDocument()
    expect(screen.getByText('could not fetch')).toBeInTheDocument()
    expect(screen.getByText('Fetch')).toBeInTheDocument()
    expect(screen.queryByText(/op-model_import/)).toBeNull()
    await user.click(screen.getByRole('switch', { name: 'Advanced' }))
    expect(await screen.findByText(/op-model_import/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Temporal/ })).toHaveAttribute(
      'href',
      expect.stringContaining('op-model_import'),
    )
    expect(screen.getByRole('link', { name: 'import_model' })).toHaveAttribute('href', '/settings#plugins')
    expect(screen.getByText(/"url": "https:\/\/x"/)).toBeInTheDocument()
  })

  it('closes on Escape and returns focus', async () => {
    const onClose = vi.fn()
    const opener = document.createElement('button')
    document.body.append(opener)
    opener.focus()
    const { user } = renderPage(<ActivityDrawer id="operation:o1" onClose={onClose} />)
    await screen.findByText('Failed')
    await user.keyboard('{Escape}')
    expect(onClose).toHaveBeenCalled()
    opener.remove()
  })

  it('opens a child in the same drawer', async () => {
    seedActivity([
      failedImport({ id: 'render:p', kind: 'render', title: 'Parent', status: 'running', detail: {} }),
      failedImport({ id: 'render:c', kind: 'render', title: 'Child piece', parent_id: 'render:p', status: 'succeeded', detail: {} }),
    ])
    const { user } = renderPage(<ActivityDrawer id="render:p" onClose={() => {}} />)
    await user.click(await screen.findByRole('button', { name: 'Child piece' }))
    expect(await screen.findByRole('dialog', { name: 'Child piece' })).toBeInTheDocument()
  })
})
