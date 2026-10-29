import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import type { ItemDetails as Details, ItemView } from '../../api/capabilities'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { ItemDetails } from './ItemDetails'

const BASE = '/api/v1/ai/capabilities'

function item(over: Partial<ItemView> = {}): ItemView {
  return {
    id: 'recall',
    kind: 'tool',
    name: 'recall',
    harness_name: 'mcp__plugin_hindsight__recall',
    label: 'Recall memories',
    description: 'The recall tool.',
    default: 'outward',
    default_note: null,
    suggested: 'read',
    access: 'outward',
    overridden: false,
    missing: false,
    allowed: null,
    fixed: null,
    reason: null,
    ...over,
  }
}

function details(over: Partial<Details> = {}): Details {
  const input_schema = {
    type: 'object',
    properties: {
      query: { type: 'string', description: 'What to look for.' },
      limit: { type: 'integer', default: 10 },
    },
    required: ['query'],
  }
  return {
    item: item(),
    input_schema,
    output_schema: { type: 'object', properties: { memories: { type: 'array', description: 'What was found.' } } },
    annotations: { readOnlyHint: true, title: 'Recall' },
    raw: { name: 'recall', title: 'Recall memories', description: 'The recall tool.', inputSchema: input_schema, extra: { b: 1, a: [2] } },
    omitted: [],
    ...over,
  }
}

/** Serves one item's details and counts the reads. */
function serve(answer: Details) {
  const reads = vi.fn()
  server.use(
    http.get(`${BASE}/:source/items/:item`, () => {
      reads()
      return HttpResponse.json(answer)
    }),
  )
  return reads
}

describe('ItemDetails', () => {
  it('loads details on first open and lists parameters', async () => {
    const reads = serve(details())
    const { user } = renderPage(<ItemDetails source="remote:hindsight" item={item()} claims />)
    expect(reads).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Details of recall' }))
    const input = await screen.findByRole('table', { name: 'Parameters of recall' })
    const rows = within(input).getAllByRole('row')
    expect(rows[1]).toHaveTextContent('query')
    expect(rows[1]).toHaveTextContent('string')
    expect(rows[1]).toHaveTextContent('required')
    expect(rows[1]).toHaveTextContent('What to look for.')
    expect(rows[2]).toHaveTextContent('limit')
    expect(rows[2]).toHaveTextContent('default 10')
    const output = screen.getByRole('table', { name: 'Output of recall' })
    expect(output).toHaveTextContent('memories')
    expect(screen.getByText('readOnlyHint: true')).toBeInTheDocument()
    expect(screen.getByText('title: Recall')).toBeInTheDocument()
    expect(screen.getByText('Claims made by the server, not checked by ScadBuddy.')).toBeInTheDocument()
    expect(screen.getByText('The recall tool.')).toHaveClass('whitespace-pre-wrap')

    await user.click(screen.getByRole('button', { name: 'Details of recall' }))
    await user.click(screen.getByRole('button', { name: 'Details of recall' }))
    await screen.findByRole('table', { name: 'Parameters of recall' })
    expect(reads).toHaveBeenCalledTimes(1)
  })

  it("does not label a built-in tool's annotations as a server's claims", async () => {
    serve(details())
    const { user } = renderPage(<ItemDetails source="builtin:scadbuddy" item={item()} claims={false} />)
    await user.click(screen.getByRole('button', { name: 'Details of recall' }))
    await screen.findByText('readOnlyHint: true')
    expect(screen.queryByText('Claims made by the server, not checked by ScadBuddy.')).toBeNull()
  })

  it('View JSON shows the raw entry unmodified', async () => {
    const answer = details()
    serve(answer)
    const { user } = renderPage(<ItemDetails source="remote:hindsight" item={item()} claims />)
    await user.click(screen.getByRole('button', { name: 'Details of recall' }))
    const view = await screen.findByRole('button', { name: 'View JSON' })
    expect(screen.queryByTestId('raw-json')).toBeNull()
    await user.click(view)
    expect(screen.getByTestId('raw-json').textContent).toBe(JSON.stringify(answer.raw, null, 2))
    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'View JSON' }))
    expect(screen.queryByTestId('raw-json')).toBeNull()
  })

  it('renders a description with markup as text', async () => {
    const markup = '<img src=x onerror=alert(1)> **bold** [link](https://x.example)'
    serve(details({ item: item({ description: markup }), input_schema: { type: 'object', properties: { q: { type: 'string', description: markup } } } }))
    const { user, container } = renderPage(<ItemDetails source="remote:hindsight" item={item({ description: markup })} claims />)
    await user.click(screen.getByRole('button', { name: 'Details of recall' }))
    await screen.findByRole('table', { name: 'Parameters of recall' })
    expect(screen.getAllByText(markup).length).toBeGreaterThanOrEqual(2)
    expect(container.querySelector('img')).toBeNull()
    expect(document.querySelector('img, strong, a[href="https://x.example"]')).toBeNull()
  })

  it('names an omitted part', async () => {
    serve(details({ input_schema: null, omitted: ['inputSchema', 'annotations'] }))
    const { user } = renderPage(<ItemDetails source="remote:hindsight" item={item()} claims />)
    await user.click(screen.getByRole('button', { name: 'Details of recall' }))
    expect(await screen.findByText('Input schema: too large to keep')).toBeInTheDocument()
    expect(screen.getByText('Annotations: too large to keep')).toBeInTheDocument()
  })

  it('says why the details did not load', async () => {
    server.use(http.get(`${BASE}/:source/items/:item`, () => HttpResponse.json({ detail: 'no item "recall"' }, { status: 404 })))
    const { user } = renderPage(<ItemDetails source="remote:hindsight" item={item()} claims />)
    await user.click(screen.getByRole('button', { name: 'Details of recall' }))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('no item "recall"'))
  })
})
