import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { bridge } from '../agent/bridge'
import { useGlobalAgentTools } from '../agent/global'
import { api } from '../api/client'
import type { FlowHostInterface, FlowScriptProblem } from '../api/types'
import { COASTER_INPUTS, flowRequests, setFlowState } from '../mocks/features/flows'
import { renderPage } from '../test/utils'
import { choose } from '../test/select'
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
    await within(list).findByRole('link', { name: /^v2 / })
    const items = within(list).getAllByRole('listitem')
    expect(items.map((li) => li.querySelector('a > span')?.textContent)).toEqual(['v2', 'v1'])
    expect(items[0]).toHaveTextContent('you, in the browser')
    expect(items[1]).toHaveTextContent('an assistant (session sess-1)')
    expect(within(list).getByRole('link', { name: /^v2 / })).toHaveAttribute('href', '/workflows/flows/def-plates-2')
    expect(within(list).getByRole('link', { name: /^v1 / })).toHaveAttribute('aria-current', 'page')
    expect(versions).toHaveBeenCalledWith('plates')
  })

  it('diffs any two versions, the one viewed against the one before by default', async () => {
    const { user } = render('def-plates-2')
    const compare = await screen.findByRole('region', { name: 'Compare versions' })
    expect(within(compare).getByRole('combobox', { name: 'From' })).toHaveTextContent('v1')
    expect(within(compare).getByRole('combobox', { name: 'To' })).toHaveTextContent('v2')
    await user.click(within(compare).getByRole('button', { name: 'Compare' }))
    const diff = await screen.findByTestId('source-diff')
    expect(diff).toHaveAttribute('data-language', 'python')
    expect(diff).toHaveAttribute('data-side-by-side', 'true')
    expect(screen.getByTestId('original')).toHaveTextContent('return 1')
    expect(screen.getByTestId('modified')).toHaveTextContent('await queue_print')
    expect(screen.getByRole('heading', { name: 'v1 → v2' })).toBeInTheDocument()

    await choose(user, within(compare).getByRole('combobox', { name: 'From' }), 'v2')
    await choose(user, within(compare).getByRole('combobox', { name: 'To' }), 'v1')
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

  it("shows the flow's triggers and makes one pinned to the version viewed (#2484)", async () => {
    render('def-swap')
    const section = await screen.findByRole('region', { name: 'Triggers' })
    expect(await within(section).findByRole('listitem', { name: /Every hour/ })).toBeInTheDocument()
    expect(within(section).getByRole('button', { name: 'New trigger…' })).toBeInTheDocument()
  })

  it('starts a run with a key and goes to it', async () => {
    const { user } = render('def-swap')
    await user.click(await screen.findByRole('button', { name: 'Start run' }))
    expect(await screen.findByText('Run page')).toBeInTheDocument()
    expect(flowRequests()).toEqual([
      { path: '/api/v1/workflows/def-swap/runs', key: expect.stringMatching(/^[0-9a-f]{32}$/), body: {} },
    ])
  })

  it('starts a run with a grant (#2481), approved as sent', async () => {
    const { user } = render('def-swap')
    await user.click(await screen.findByRole('button', { name: 'Start with a grant…' }))
    const dialog = await screen.findByRole('dialog', { name: 'Start with a grant' })
    const arrange = await within(dialog).findByRole('group', { name: 'Rule arrange' })
    await user.click(within(arrange).getByRole('checkbox', { name: 'Allow without asking' }))
    await user.type(within(arrange).getByLabelText('Max calls'), '2')
    await user.click(within(dialog).getByRole('button', { name: 'Start run' }))
    expect(await screen.findByText('Run page')).toBeInTheDocument()
    const sent = flowRequests()
    expect(sent).toHaveLength(1)
    expect(sent[0]).toMatchObject({ path: '/api/v1/workflows/def-swap/runs', key: expect.stringMatching(/^[0-9a-f]{32}$/) })
    expect(sent[0]!.body['grant']).toMatchObject({
      sessions: false,
      ask_starter_session: true,
      rules: [{ fn: 'arrange', max_calls: 2 }],
    })
  })

  it('keeps "Start with a grant…" from the browser bridge (#2481)', async () => {
    function Agent() {
      useGlobalAgentTools()
      return null
    }
    renderPage(
      <>
        <Agent />
        <Routes>
          <Route path="/workflows/flows/:id" element={<FlowPage />} />
        </Routes>
      </>,
      { route: '/workflows/flows/def-swap' },
    )
    await screen.findByRole('button', { name: 'Start with a grant…' })
    await waitFor(() => expect(bridge.liveNames()).toContain('click'))
    const outcome = await bridge.call('click', { role: 'button', name: 'Start with a grant…' })
    expect(!outcome.ok && outcome.error.code).toBe('refused')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
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
    await choose(user, within(compare).getByRole('combobox', { name: 'To' }), 'v2')
    await user.click(within(compare).getByRole('button', { name: 'Compare' }))
    expect(await screen.findByTestId('source-diff')).toHaveAttribute('data-side-by-side', 'false')
    expect(screen.getByRole('button', { name: 'Start run' })).toBeInTheDocument()
  })

  it('archives the flow, stopping new runs until it is unarchived (#2485)', async () => {
    const { user } = render('def-swap')
    await user.click(await screen.findByRole('button', { name: 'Archive' }))
    const dialog = await screen.findByRole('alertdialog', { name: 'Archive swap?' })
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
    const dialog = await screen.findByRole('alertdialog', { name: 'Delete plates?' })
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

describe('FlowPage inputs (#2483)', () => {
  it('edits the inputs schema beside the script, checks it, and saves it with the version', async () => {
    const { user } = render('def-coasters')
    const schema = (await screen.findByRole('textbox', { name: 'Inputs (JSON Schema)' })) as HTMLTextAreaElement
    expect(JSON.parse(schema.value)).toEqual(COASTER_INPUTS)
    expect(await screen.findByText('No problems', undefined, LOAD)).toBeInTheDocument()

    fireEvent.change(schema, { target: { value: '{"type": ' } })
    let problems = await screen.findByRole('list', { name: 'Problems' }, LOAD)
    expect(within(problems).getByRole('listitem')).toHaveTextContent(/^InputsNot JSON/)
    // A problem in the schema is not a line of the script.
    expect(screen.getByTestId('marked-lines')).toHaveTextContent('')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(screen.getByRole('alert')).toHaveTextContent('Not JSON')

    fireEvent.change(schema, { target: { value: '{"type": "array"}' } })
    problems = await screen.findByRole('list', { name: 'Problems' }, LOAD)
    await vi.waitFor(() =>
      expect(within(problems).getByRole('listitem')).toHaveTextContent('Inputsthe top level must be {"type": "object"}'),
    )

    const next = { type: 'object', properties: { slug: { type: 'string' } }, required: ['slug'] }
    fireEvent.change(schema, { target: { value: JSON.stringify(next) } })
    expect(await screen.findByText('No problems', undefined, LOAD)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('heading', { name: 'coasters v2' })).toBeInTheDocument()
    expect(flowRequests().map((r) => r.body)).toEqual([
      { name: 'coasters', script: expect.stringContaining('await inputs()'), inputs_schema: next },
    ])
  })

  it('completes inputs() for the schema being edited (#2651)', async () => {
    render('def-coasters')
    const schema = await screen.findByRole('textbox', { name: 'Inputs (JSON Schema)' })
    expect(await screen.findByText(/,inputs$/, undefined, LOAD)).toBeInTheDocument()
    fireEvent.change(schema, { target: { value: '' } })
    await vi.waitFor(() => expect(screen.getByTestId('completes').textContent).not.toMatch(/inputs/), LOAD)
  })

  it('saves a blank schema as a flow with no inputs', async () => {
    const { user } = render('def-coasters')
    const schema = await screen.findByRole('textbox', { name: 'Inputs (JSON Schema)' })
    fireEvent.change(schema, { target: { value: '  ' } })
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('heading', { name: 'coasters v2' })).toBeInTheDocument()
    expect(flowRequests()[0]!.body).not.toHaveProperty('inputs_schema')
  })

  it("asks for a flow's inputs before it starts", async () => {
    const { user } = render('def-coasters')
    await user.click(await screen.findByRole('button', { name: 'Start run' }))
    const dialog = await screen.findByRole('dialog', { name: 'Start coasters' })
    await user.type(within(dialog).getByRole('textbox', { name: /Template/ }), 'coaster')
    await user.click(within(dialog).getByRole('button', { name: 'Start run' }))
    expect(await screen.findByText('Run page')).toBeInTheDocument()
    expect(flowRequests().map((r) => r.body)).toEqual([
      { inputs: { slug: 'coaster', copies: 2, colour: 'red', dry_run: false, plates: [1] } },
    ])
  })

  it('starts a flow with both its inputs and a grant (#2481, #2483)', async () => {
    const { user } = render('def-coasters')
    await user.click(await screen.findByRole('button', { name: 'Start with a grant…' }))
    const dialog = await screen.findByRole('dialog', { name: 'Start with a grant' })
    const inputs = within(dialog).getByRole('region', { name: 'Inputs' })
    const arrange = await within(dialog).findByRole('group', { name: 'Rule arrange' })
    await user.click(within(arrange).getByRole('checkbox', { name: 'Allow without asking' }))
    await user.type(within(arrange).getByLabelText('Max calls'), '2')
    // The inputs are checked too: a required one left blank keeps the run from starting.
    await user.click(within(dialog).getByRole('button', { name: 'Start run' }))
    expect(within(inputs).getByText('Required')).toBeInTheDocument()
    expect(flowRequests()).toEqual([])
    await user.type(within(inputs).getByRole('textbox', { name: /Template/ }), 'lid')
    await user.click(within(dialog).getByRole('button', { name: 'Start run' }))
    expect(await screen.findByText('Run page')).toBeInTheDocument()
    const [sent] = flowRequests()
    expect(sent!.path).toBe('/api/v1/workflows/def-coasters/runs')
    expect(sent!.body['inputs']).toEqual({ slug: 'lid', copies: 2, colour: 'red', dry_run: false, plates: [1] })
    expect(sent!.body['grant']).toMatchObject({ rules: [{ fn: 'arrange', max_calls: 2 }] })
  })

  it('shows the inputs schema read only on a phone', async () => {
    asPhone()
    render('def-coasters')
    const inputs = await screen.findByLabelText('Inputs')
    expect(inputs.tagName).toBe('PRE')
    expect(inputs).toHaveTextContent('"slug"')
  })
})
