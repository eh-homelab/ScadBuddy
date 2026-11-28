import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { beforeEach, describe, expect, it } from 'vitest'
import { resetMockState } from '../../mocks/handlers'
import { inventoryMock, seedItem } from '../../mocks/features/inventory'
import { seedEntityProperties } from '../../mocks/features/entities'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { choose } from '../../test/select'
import { filamentLinks } from '../../lib/filamentLinks'
import { KindsPage } from './KindsPage'

const bodyOf = (route: string) => inventoryMock.bodies.filter((b) => b.route === route).map((b) => b.body)
/** The list item of the kind its link names. */
const kindItem = (scope: HTMLElement, name: string) => within(scope).getByRole('link', { name }).closest('li') as HTMLElement
const kindList = () => screen.findByRole('list', { name: 'Item kinds' })

describe('KindsPage (#7067)', () => {
  beforeEach(() => resetMockState())

  it('shows the kinds as nested lists, each with its tracking and items', async () => {
    renderPage(<KindsPage />)
    const tree = await kindList()
    const consumable = within(kindItem(tree, 'Consumable')).getByRole('list', { name: 'Kinds under Consumable' })
    expect(kindItem(consumable, 'Hardware')).toHaveTextContent('lots')
    const part = within(kindItem(tree, 'Part')).getByRole('list', { name: 'Kinds under Part' })
    expect(kindItem(part, 'Nozzle')).toHaveTextContent('units')
    expect(kindItem(tree, 'Service')).toHaveTextContent('not counted')
    expect(screen.queryByRole('tree')).toBeNull()
  })

  it('links each kind to its label', async () => {
    renderPage(<KindsPage />)
    const tree = await kindList()
    expect(within(kindItem(tree, 'Hardware')).getByRole('link', { name: 'Print label for Hardware' })).toHaveAttribute('href', filamentLinks.kindLabel('hardware'))
  })

  it('adds a kind under a parent', async () => {
    const { user } = renderPage(<KindsPage />)
    await kindList()
    await user.click(screen.getByRole('button', { name: 'Add kind' }))
    const form = screen.getByRole('form', { name: 'Add kind' })
    await user.type(within(form).getByLabelText(/^Id/), 'magnet')
    await user.type(within(form).getByLabelText('Name'), 'Magnet')
    await choose(user, within(form).getByRole('combobox', { name: 'Parent' }), 'Hardware')
    await user.click(within(form).getByRole('button', { name: 'Add kind' }))
    expect(await screen.findByRole('link', { name: 'Magnet' })).toBeInTheDocument()
    expect(bodyOf('createKind')).toEqual([{ id: 'magnet', name: 'Magnet', parent_id: 'hardware', tracking: 'lot', uom: 'each', life_basis: 'none', saleable: false }])
  })

  it('numbers a kind added with no id, and shows each kind\'s code (the owner, 2026-11-27)', async () => {
    const { user } = renderPage(<KindsPage />)
    const tree = await kindList()
    const printer = inventoryMock.kinds.find((k) => k.id === 'printer')!
    expect(kindItem(tree, 'Printer')).toHaveTextContent(`K-${printer.number}`)
    await user.click(screen.getByRole('button', { name: 'Add kind' }))
    const form = screen.getByRole('form', { name: 'Add kind' })
    expect(within(form).getByText('Leave empty to number it')).toBeInTheDocument()
    await user.type(within(form).getByLabelText('Name'), 'Magnet')
    await user.click(within(form).getByRole('button', { name: 'Add kind' }))
    const link = await screen.findByRole('link', { name: 'Magnet' })
    const made = inventoryMock.kinds.find((k) => k.name === 'Magnet')!
    expect(made.id).toBe(`k${made.number}`)
    expect(link.closest('li')).toHaveTextContent(`K-${made.number}`)
    expect(bodyOf('createKind')).toEqual([{ name: 'Magnet', parent_id: null, tracking: 'lot', uom: 'each', life_basis: 'none', saleable: false }])
  })

  it('refuses a typed id of k and digits before it sends', async () => {
    const { user } = renderPage(<KindsPage />)
    await kindList()
    await user.click(screen.getByRole('button', { name: 'Add kind' }))
    const form = screen.getByRole('form', { name: 'Add kind' })
    await user.type(within(form).getByLabelText(/^Id/), 'k12')
    await user.type(within(form).getByLabelText('Name'), 'Magnet')
    await user.click(within(form).getByRole('button', { name: 'Add kind' }))
    expect(await within(form).findByText(/k and digits is a made kind's id/)).toBeInTheDocument()
    expect(bodyOf('createKind')).toEqual([])
  })

  it("edits a kind's schema as a key table and saves it", async () => {
    const { user } = renderPage(<KindsPage />)
    const tree = await kindList()
    await user.click(within(tree).getByRole('button', { name: 'Edit Hardware' }))
    const form = screen.getByRole('form', { name: 'Edit Hardware' })
    await user.click(within(form).getByRole('button', { name: 'Add key' }))
    await user.type(within(form).getByLabelText('Key 1'), 'thread')
    await user.type(within(form).getByLabelText('Description of key 1'), 'Thread size, such as M3')
    await user.click(within(form).getByRole('button', { name: 'Save kind' }))
    await waitFor(() => expect(bodyOf('patchKind')).toHaveLength(1))
    expect(bodyOf('patchKind')[0]).toMatchObject({
      attribute_schema: { type: 'object', properties: { thread: { type: 'string', description: 'Thread size, such as M3' } } },
    })
  })

  it('shows the 422 detail when the server refuses the schema, and keeps the edits', async () => {
    server.use(
      http.patch('/api/v1/inventory/kinds/:id', () =>
        HttpResponse.json(
          { type: 'https://scadbuddy.dev/problems/entities/bad_property_schema', title: 'Unprocessable Content', status: 422, detail: "property schema: keyword 'additionalProperties' may only be true", code: 'bad_property_schema' },
          { status: 422, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const { user } = renderPage(<KindsPage />)
    const tree = await kindList()
    await user.click(within(tree).getByRole('button', { name: 'Edit Hardware' }))
    const form = screen.getByRole('form', { name: 'Edit Hardware' })
    await user.click(within(form).getByRole('button', { name: 'Add key' }))
    await user.type(within(form).getByLabelText('Key 1'), 'thread')
    await user.click(within(form).getByRole('button', { name: 'Save kind' }))
    expect(await within(form).findByRole('alert')).toHaveTextContent("keyword 'additionalProperties' may only be true")
    expect(within(form).getByLabelText('Key 1')).toHaveValue('thread')
  })

  it('archives a kind with no items, and says why one with items is not', async () => {
    const { user } = renderPage(<KindsPage />)
    const tree = await kindList()
    await user.click(within(tree).getByRole('button', { name: 'Archive Hardware' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('The kind has items')
    await user.click(within(tree).getByRole('button', { name: 'Archive Tool' }))
    await waitFor(() => expect(within(screen.getByRole('list', { name: 'Item kinds' })).queryByRole('link', { name: 'Tool' })).toBeNull())
  })

  it("drops a key's type-specific keywords when its type changes", async () => {
    const { user } = renderPage(<KindsPage />)
    const tree = await kindList()
    await user.click(within(tree).getByRole('button', { name: 'Edit Spool insert' }))
    const form = screen.getByRole('form', { name: 'Edit Spool insert' })
    expect(within(form).getByLabelText('Key 2')).toHaveValue('role')
    await choose(user, within(form).getByRole('combobox', { name: 'Type of key 2' }), 'Number')
    await user.click(within(form).getByRole('button', { name: 'Save kind' }))
    await waitFor(() => expect(bodyOf('patchKind')).toHaveLength(1))
    const properties = (bodyOf('patchKind')[0] as { attribute_schema: { properties: Record<string, Record<string, unknown>> } }).attribute_schema.properties
    expect(properties.role).toEqual({ type: 'number', description: 'A reusable shell, or an addon such as an AMS adaptor' })
    // A key whose type stays keeps its keywords.
    expect(properties.empty_weight_g).toMatchObject({ type: 'integer', exclusiveMinimum: 0 })
  })

  it('keeps each key row its own when one above it is removed', async () => {
    const { user } = renderPage(<KindsPage />)
    const tree = await kindList()
    await user.click(within(tree).getByRole('button', { name: 'Edit Spool insert' }))
    const form = screen.getByRole('form', { name: 'Edit Spool insert' })
    await user.click(within(form).getByRole('button', { name: 'Remove key 1' }))
    expect(within(form).getByLabelText('Key 1')).toHaveValue('role')
    expect(within(form).getByRole('combobox', { name: 'Type of key 1' })).toHaveTextContent('Text')
    expect(within(form).getByLabelText('Description of key 1')).toHaveValue('A reusable shell, or an addon such as an AMS adaptor')
  })

  it('marks a key required and says about how many items lack it before the save, from keys_in_use (R-K1-33)', async () => {
    const one = seedItem({ kind_id: 'spool_part', name: 'Shell A' })
    seedItem({ kind_id: 'spool_part', name: 'Shell B' })
    const gone = seedItem({ kind_id: 'spool_part', name: 'Shell C', archived_at: '2026-11-01T00:00:00Z' })
    seedEntityProperties(`item:${one.id}`, { role: 'shell' })
    seedEntityProperties(`item:${gone.id}`, { role: 'shell' })
    const reads: string[] = []
    const onStart = ({ request }: { request: Request }) => void reads.push(new URL(request.url).pathname + new URL(request.url).search)
    server.events.on('request:start', onStart)
    try {
      const { user } = renderPage(<KindsPage />)
      const tree = await kindList()
      await user.click(within(tree).getByRole('button', { name: 'Edit Spool insert' }))
      const form = screen.getByRole('form', { name: 'Edit Spool insert' })
      await user.click(within(form).getByRole('checkbox', { name: 'Key 2 is required' }))
      // Three items, archived one too, and keys_in_use counts two rows of `role`.
      expect(await within(form).findByText('About 1 item lacks role')).toBeInTheDocument()
      expect(reads.filter((path) => path.includes('/entities/'))).toEqual([])
      expect(reads).toContain('/api/v1/inventory/kinds/spool_part/schema')
      // The route's highest limit, so the estimate counts up to 5000 items.
      expect(reads.find((path) => path.startsWith('/api/v1/inventory/items?'))).toMatch(/[?&]limit=5000(&|$)/)
      await user.click(within(form).getByRole('button', { name: 'Save kind' }))
      await waitFor(() => expect(bodyOf('patchKind')).toHaveLength(1))
      expect((bodyOf('patchKind')[0] as { attribute_schema: { required?: string[] } }).attribute_schema.required).toEqual(['role'])
    } finally {
      server.events.removeListener('request:start', onStart)
    }
  })
})
