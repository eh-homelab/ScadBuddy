import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'
import { blobUrl } from '../../agent/chat/protocol'
import type { ToolCall, ToolStatus } from '../../agent/chat/toolGroups'
import { highlightJson, prettyJson } from '../../agent/chat/json'
import { ToolGroup } from './ToolGroup'

// #782 — grouped, friendly tool calls in the assistant panel.

const IMAGE = { name: `${'a'.repeat(64)}.png`, mediaType: 'image/png' as const }

const call = (id: string, extra: Partial<ToolCall> = {}): ToolCall => ({
  kind: 'tool',
  id,
  name: 'mcp__scadbuddy__render_model',
  input: { slug: 'cable-clip', params: { width: 40 } },
  risk: 'write',
  ...extra,
})

function group(calls: ToolCall[], statuses: ToolStatus[], advanced = false) {
  const user = userEvent.setup()
  const view = render(
    <MemoryRouter>
      <ToolGroup calls={calls} statuses={statuses} sessionId="sess-1" advanced={advanced} />
    </MemoryRouter>,
  )
  return { user, ...view }
}

describe('a single call', () => {
  it('reads as its title and status, with arguments and result behind Details', async () => {
    const { user } = group([call('t1', { title: 'Render cable-clip', result: { ok: true, summary: '{"job_id":"j1"}', sources: [] } })], ['done'])
    expect(screen.queryByTestId('agent-tool-group')).toBeNull()
    const tool = screen.getByTestId('agent-tool')
    expect(tool).toHaveTextContent('Render cable-clip')
    expect(within(tool).getByTestId('agent-tool-status')).toHaveTextContent('Done')
    expect(within(tool).getByText('write')).toBeInTheDocument()
    // The raw result and the arguments are hidden until asked for.
    expect(tool).not.toHaveTextContent('job_id')
    const details = within(tool).getByRole('button', { name: 'Details: Render cable-clip' })
    expect(details).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByTestId('agent-tool-details')).not.toBeVisible()

    await user.click(details)
    expect(details).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByTestId('agent-tool-details')).toBeVisible()
    expect(details).toHaveAttribute('aria-controls', screen.getByTestId('agent-tool-details').id)
    expect(screen.getByTestId('agent-tool-arguments')).toHaveTextContent('"width": 40')
    expect(screen.getByTestId('agent-tool-result')).toHaveTextContent('"job_id": "j1"')
    expect(tool).toHaveTextContent('mcp__scadbuddy__render_model')

    // Keyboard: the same button closes it.
    details.focus()
    await user.keyboard('{Enter}')
    expect(details).toHaveAttribute('aria-expanded', 'false')
  })

  it('names a call without a title itself', () => {
    group([call('t1', { name: 'mcp__hindsight__recall', input: {} })], ['running'])
    expect(screen.getByTestId('agent-tool')).toHaveTextContent('Recall (hindsight)')
    expect(screen.getByTestId('agent-tool-status')).toHaveTextContent('Running')
  })

  it('shows a failure’s message without opening it', () => {
    group([call('t1', { result: { ok: false, summary: 'width must be at most 200', sources: [] } })], ['failed'])
    expect(screen.getByTestId('agent-tool')).toHaveTextContent('width must be at most 200')
    expect(screen.getByTestId('agent-tool-status')).toHaveTextContent('Failed')
  })

  it.each([
    ['waiting_approval', 'Waiting for approval'],
    ['waiting_input', 'Waiting for you'],
    ['not_run', 'Not run'],
    ['stopped', 'Stopped'],
  ] as const)('says %s in words', (status, words) => {
    group([call('t1')], [status])
    expect(screen.getByTestId('agent-tool-status')).toHaveTextContent(words)
  })

  it('shows the images its result carried, served by the agent, each opening the full image', () => {
    group([call('t1', { title: 'Look at the render', result: { ok: true, summary: '', sources: [], images: [IMAGE] } })], ['done'])
    const img = screen.getByRole('img', { name: 'Look at the render' })
    expect(img).toHaveAttribute('src', blobUrl('sess-1', IMAGE.name))
    expect(img).toHaveAttribute('src', `/api/v1/ai/sessions/sess-1/blobs/${IMAGE.name}`)
    expect(img.closest('a')).toHaveAttribute('href', blobUrl('sess-1', IMAGE.name))
  })

  it('opens Details in Advanced mode, and keeps sources and the undo link visible', () => {
    group(
      [
        call('t1', {
          result: { ok: true, summary: 'ok', sources: [{ title: 'Docs', url: 'https://example.com' }], version: { slug: 'cable-clip', revision: 'abcdef123' } },
        }),
      ],
      ['done'],
      true,
    )
    expect(screen.getByTestId('agent-tool-details')).toBeVisible()
    expect(screen.getByRole('link', { name: 'Docs' })).toBeVisible()
    expect(screen.getByRole('link', { name: /Undo from version abcdef1/ })).toHaveAttribute('href', '/m/cable-clip/versions')
  })
})

describe('a group of calls', () => {
  const calls = [
    call('t1', { title: 'Render cable-clip', result: { ok: true, summary: '', sources: [] } }),
    call('t2', { title: 'Look at the render', result: { ok: true, summary: '', sources: [], images: [IMAGE] } }),
  ]

  it('is closed once every call has ended, with its status, last title and images in the header', async () => {
    const { user } = group(calls, ['done', 'done'])
    const section = screen.getByRole('region', { name: '2 steps' })
    const header = within(section).getByRole('button', { name: /2 steps/ })
    expect(header).toHaveAttribute('aria-expanded', 'false')
    expect(header).toHaveTextContent('Look at the render')
    expect(within(section).getByTestId('agent-tool-group-status')).toHaveTextContent('Done')
    expect(screen.queryAllByTestId('agent-tool')).toEqual([])
    // The image shows with the group closed.
    expect(screen.getByRole('img', { name: 'Look at the render' })).toHaveAttribute('src', blobUrl('sess-1', IMAGE.name))

    await user.click(header)
    expect(header).toHaveAttribute('aria-expanded', 'true')
    expect(header).toHaveAttribute('aria-controls', expect.any(String))
    expect(screen.getAllByTestId('agent-tool')).toHaveLength(2)
  })

  it('is open while a call is live, and shows the live status', () => {
    group([calls[0]!, call('t2', { risk: 'outward' })], ['done', 'waiting_approval'])
    const header = screen.getByRole('button', { name: /2 steps/ })
    expect(header).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByTestId('agent-tool-group-status')).toHaveTextContent('Waiting for approval')
    expect(screen.getAllByTestId('agent-tool')).toHaveLength(2)
  })

  it('says failed when a call failed', () => {
    group([calls[0]!, call('t2', { result: { ok: false, summary: 'boom', sources: [] } })], ['done', 'failed'])
    expect(screen.getByTestId('agent-tool-group-status')).toHaveTextContent('Failed')
  })

  it('opens every group in Advanced mode', () => {
    group(calls, ['done', 'done'], true)
    expect(screen.getByRole('button', { name: /2 steps/ })).toHaveAttribute('aria-expanded', 'true')
  })

  it('indents a subagent’s call under its Agent call', () => {
    group(
      [call('agent', { name: 'Agent', input: { description: 'Check the fit' } }), call('sub', { parent: 'agent' })],
      ['running', 'running'],
    )
    const [agent, sub] = screen.getAllByTestId('agent-tool')
    expect(agent).toHaveTextContent('Subagent: Check the fit')
    expect(agent!.className).not.toContain('ml-4')
    expect(sub!.className).toContain('ml-4')
  })
})

describe('JSON (#886)', () => {
  it('pretty-prints JSON and leaves anything else as it is', () => {
    expect(prettyJson({ a: 1 })).toBe('{\n  "a": 1\n}')
    expect(prettyJson('{"a":[1,true,null]}')).toBe('{\n  "a": [\n    1,\n    true,\n    null\n  ]\n}')
    expect(prettyJson('{"cut": "short…')).toBeUndefined()
    expect(prettyJson('Set 1 parameter')).toBeUndefined()
  })

  it('highlights keys apart from values', () => {
    const { container } = render(<pre>{highlightJson('{\n  "a": "b",\n  "n": 2\n}')}</pre>)
    expect([...container.querySelectorAll('[data-token="key"]')].map((e) => e.textContent)).toEqual(['"a"', '"n"'])
    expect(container.textContent).toBe('{\n  "a": "b",\n  "n": 2\n}')
  })
})
