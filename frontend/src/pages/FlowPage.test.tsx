import { fireEvent, screen, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import type { FlowHostInterface, FlowScriptProblem } from '../api/types'
import { flowRequests, setFlowState } from '../mocks/features/flows'
import { renderPage } from '../test/utils'
import { FlowPage } from './FlowPage'

// #2350 — one flow version over the msw flows (src/mocks/features/flows.ts): its
// versions, its script, and Start run. #2487 — the editor: its check by line, Save as a
// new version, a diff of any two versions, and Restore; on a phone, read only.

// Monaco needs layout, workers and a canvas, none of which jsdom has (SourceEditor.test):
// the editor and the diff are stand-ins that show what the page hands them.
vi.mock('../components/flows/FlowEditor', () => ({
  FlowEditor: ({
    initialValue,
    onChange,
    problems,
    hostFunctions,
    label,
  }: {
    initialValue: string
    onChange: (next: string) => void
    problems: FlowScriptProblem[]
    hostFunctions?: FlowHostInterface
    label: string
  }) => (
    <div>
      <textarea aria-label={label} defaultValue={initialValue} onChange={(event) => onChange(event.target.value)} />
      <p data-testid="marked-lines">{problems.map((p) => p.line).join(',')}</p>
      <p data-testid="completes">{hostFunctions?.functions.map((f) => f.name).join(',')}</p>
    </div>
  ),
}))
vi.mock('../components/SourceDiff', () => ({
  SourceDiff: ({
    original,
    modified,
    language,
    sideBySide,
  }: {
    original: string
    modified: string
    language: string
    sideBySide: boolean
  }) => (
    <div data-testid="source-diff" data-language={language} data-side-by-side={String(sideBySide)}>
      <pre data-testid="original">{original}</pre>
      <pre data-testid="modified">{modified}</pre>
    </div>
  ),
}))

function render(id: string) {
  return renderPage(
    <Routes>
      <Route path="/workflows/flows/:id" element={<FlowPage />} />
      <Route path="/workflows/runs/:id" element={<p>Run page</p>} />
    </Routes>,
    { route: `/workflows/flows/${id}` },
  )
}

function asPhone() {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: query.includes('max-width'),
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  }))
}

afterEach(() => vi.unstubAllGlobals())

const LOAD = { timeout: 3000 }

describe('FlowPage', () => {
  it("edits the script in the editor, completing the host functions the server lists", async () => {
    render('def-swap')
    expect(await screen.findByRole('heading', { name: 'swap v1' })).toBeInTheDocument()
    const editor = await screen.findByRole('textbox', { name: 'Script' })
    expect((editor as HTMLTextAreaElement).value).toContain('await wait_for_human("Swap to pink?", 600)')
    expect(await screen.findByText(/^sleep,wait_for_human,render/)).toBeInTheDocument()
    expect(screen.getByText(/Registered by an assistant \(session sess-1\)/)).toBeInTheDocument()
  })

  it('type-checks as you type and marks each problem by line', async () => {
    render('def-swap')
    const editor = await screen.findByRole('textbox', { name: 'Script' })
    expect(await screen.findByText('No problems', undefined, LOAD)).toBeInTheDocument()
    fireEvent.change(editor, { target: { value: 'async def main():\n    x = 1\n    await sleep("soon")\n' } })
    const problems = await screen.findByRole('list', { name: 'Problems' }, LOAD)
    expect(within(problems).getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      'Line 3error[invalid-argument-type]: Argument to function `sleep` is incorrect',
    ])
    expect(screen.getByTestId('marked-lines')).toHaveTextContent('3')
  })

  it('saves an edit as a new version, with the version it came from’s approval timeout, and goes to it', async () => {
    const { user } = render('def-swap')
    const editor = await screen.findByRole('textbox', { name: 'Script' })
    const save = screen.getByRole('button', { name: 'Save' })
    expect(save).toBeDisabled()
    fireEvent.change(editor, { target: { value: 'async def main():\n    await sleep(5)\n' } })
    expect(screen.getByText('Unsaved changes')).toBeInTheDocument()
    await user.click(save)
    expect(await screen.findByRole('heading', { name: 'swap v2' })).toBeInTheDocument()
    expect(flowRequests()).toEqual([
      { path: '/api/v1/workflows', key: null, body: { name: 'swap', script: 'async def main():\n    await sleep(5)\n' } },
    ])
    expect(screen.getByText(/Registered by you, in the browser/)).toBeInTheDocument()
  })

  it("shows a refused Save's problems by line and keeps the edit", async () => {
    const { user } = render('def-swap')
    const editor = await screen.findByRole('textbox', { name: 'Script' })
    fireEvent.change(editor, { target: { value: 'async def main():\n    await nope()\n' } })
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('The script does not type-check.')
    const problems = await screen.findByRole('list', { name: 'Problems' })
    expect(within(problems).getByRole('listitem')).toHaveTextContent('Line 2')
    expect(screen.getByRole('heading', { name: 'swap v1' })).toBeInTheDocument()
    expect((editor as HTMLTextAreaElement).value).toContain('nope()')
  })

  it("lists the name's versions, newest first, each with who saved it and when", async () => {
    const versions = vi.spyOn(api, 'listFlowVersions')
    render('def-plates-1')
    const list = await screen.findByRole('region', { name: 'Versions' })
    await within(list).findByRole('link', { name: 'v2' })
    const items = within(list).getAllByRole('listitem')
    expect(items.map((li) => li.textContent?.split(' ')[0])).toEqual(['v2', 'v1'])
    expect(items[0]).toHaveTextContent('you, in the browser')
    expect(items[1]).toHaveTextContent('an assistant (session sess-1)')
    expect(within(list).getByRole('link', { name: 'v2' })).toHaveAttribute('href', '/workflows/flows/def-plates-2')
    expect(within(list).getByText('v1')).toHaveAttribute('aria-current', 'page')
    expect(versions).toHaveBeenCalledWith('plates')
  })

  it('diffs any two versions, the one viewed against the one before by default', async () => {
    const { user } = render('def-plates-2')
    const compare = await screen.findByRole('region', { name: 'Compare versions' })
    await within(compare).findAllByRole('option', { name: 'v1' })
    expect(within(compare).getByRole('combobox', { name: 'From' })).toHaveDisplayValue('v1')
    expect(within(compare).getByRole('combobox', { name: 'To' })).toHaveDisplayValue('v2')
    await user.click(within(compare).getByRole('button', { name: 'Compare' }))
    const diff = await screen.findByTestId('source-diff')
    expect(diff).toHaveAttribute('data-language', 'python')
    expect(diff).toHaveAttribute('data-side-by-side', 'true')
    expect(screen.getByTestId('original')).toHaveTextContent('return 1')
    expect(screen.getByTestId('modified')).toHaveTextContent('await queue_print')
    expect(screen.getByRole('heading', { name: 'v1 → v2' })).toBeInTheDocument()

    await user.selectOptions(within(compare).getByRole('combobox', { name: 'From' }), 'v2')
    await user.selectOptions(within(compare).getByRole('combobox', { name: 'To' }), 'v1')
    await user.click(within(compare).getByRole('button', { name: 'Compare' }))
    expect(await screen.findByRole('heading', { name: 'v2 → v1' })).toBeInTheDocument()
    expect(screen.getByTestId('original')).toHaveTextContent('await queue_print')
  })

  it('restores an older version as a new one and goes to it; the newest has no Restore', async () => {
    const { user } = render('def-plates-1')
    await user.click(await screen.findByRole('button', { name: 'Restore v1' }))
    expect(await screen.findByRole('heading', { name: 'plates v3' })).toBeInTheDocument()
    expect(flowRequests()).toEqual([
      { path: `/api/v1/workflows/names/plates/versions/${'c0ffee1'.padEnd(40, '0')}/restore`, key: null, body: {} },
    ])
    expect(((await screen.findByRole('textbox', { name: 'Script' })) as HTMLTextAreaElement).value).toBe(
      'async def main():\n    return 1\n',
    )
    expect(screen.queryByRole('button', { name: /^Restore/ })).not.toBeInTheDocument()
  })

  it('starts a run with a key and goes to it', async () => {
    const { user } = render('def-swap')
    await user.click(await screen.findByRole('button', { name: 'Start run' }))
    expect(await screen.findByText('Run page')).toBeInTheDocument()
    expect(flowRequests()).toEqual([
      { path: '/api/v1/workflows/def-swap/runs', key: expect.stringMatching(/^[0-9a-f]{32}$/), body: {} },
    ])
  })

  it('on a phone, shows the script and a diff read only: no editor, Save or Restore', async () => {
    asPhone()
    const { user } = render('def-plates-1')
    expect(await screen.findByRole('heading', { name: 'plates v1' })).toBeInTheDocument()
    const script = await screen.findByLabelText('Script')
    expect(script.tagName).toBe('PRE')
    expect(script).toHaveTextContent('return 1')
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Restore/ })).not.toBeInTheDocument()
    const compare = screen.getByRole('region', { name: 'Compare versions' })
    await within(compare).findAllByRole('option', { name: 'v2' })
    await user.selectOptions(within(compare).getByRole('combobox', { name: 'To' }), 'v2')
    await user.click(within(compare).getByRole('button', { name: 'Compare' }))
    expect(await screen.findByTestId('source-diff')).toHaveAttribute('data-side-by-side', 'false')
    expect(screen.getByRole('button', { name: 'Start run' })).toBeInTheDocument()
  })

  it('archives the flow, stopping new runs until it is unarchived (#2485)', async () => {
    const { user } = render('def-swap')
    await user.click(await screen.findByRole('button', { name: 'Archive' }))
    const dialog = await screen.findByRole('dialog', { name: 'Archive swap?' })
    expect(dialog).toHaveTextContent('Finished runs keep their steps, their result and the script they ran')
    await user.click(within(dialog).getByRole('button', { name: 'Archive' }))
    expect(await screen.findByText(/Archived by you, in the browser/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Start run' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Unarchive' }))
    await vi.waitFor(() => expect(screen.getByRole('button', { name: 'Start run' })).toBeEnabled())
    expect(screen.queryByText(/Archived by/)).not.toBeInTheDocument()
  })

  it('deletes the flow and restores the version viewed as its next (#2485)', async () => {
    const { user } = render('def-plates-1')
    await user.click(await screen.findByRole('button', { name: 'Delete' }))
    const dialog = await screen.findByRole('dialog', { name: 'Delete plates?' })
    expect(dialog).toHaveTextContent('which keeps every version')
    await user.click(within(dialog).getByRole('button', { name: 'Delete flow' }))
    expect(await screen.findByText(/Deleted by you, in the browser/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Start run' })).toBeDisabled()
    // Its script and versions are still read from the history.
    expect(((await screen.findByRole('textbox', { name: 'Script' })) as HTMLTextAreaElement).value).toContain('return 1')
    await user.click(screen.getByRole('button', { name: 'Restore v1' }))
    expect(await screen.findByRole('heading', { name: 'plates v3' })).toBeInTheDocument()
    expect(screen.queryByText(/Deleted by/)).not.toBeInTheDocument()
    expect(flowRequests().map((r) => r.path)).toEqual([
      '/api/v1/workflows/names/plates',
      `/api/v1/workflows/names/plates/versions/${'c0ffee1'.padEnd(40, '0')}/restore`,
    ])
  })

  it('offers Restore for a deleted flow on a phone too (#2485)', async () => {
    asPhone()
    setFlowState({ name: 'swap', deleted_at: '2026-10-12T00:00:00Z', deleted_by: { kind: 'browser' } })
    render('def-swap')
    expect(await screen.findByRole('button', { name: 'Restore v1' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Start run' })).toBeDisabled()
  })

  it('says when there is no such flow', async () => {
    render('def-gone')
    expect(await screen.findByRole('alert')).toHaveTextContent('No such flow.')
  })
})
