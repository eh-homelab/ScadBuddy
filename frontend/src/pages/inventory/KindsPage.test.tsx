import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { beforeEach, describe, expect, it } from 'vitest'
import { resetMockState } from '../../mocks/handlers'
import { inventoryMock } from '../../mocks/features/inventory'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { choose } from '../../test/select'
import { KindsPage } from './KindsPage'

const bodyOf = (route: string) => inventoryMock.bodies.filter((b) => b.route === route).map((b) => b.body)

describe('KindsPage (#7067)', () => {
  beforeEach(() => resetMockState())

  it('shows the kinds as a tree, each with its tracking and items', async () => {
    renderPage(<KindsPage />)
    const tree = await screen.findByRole('tree', { name: 'Item kinds' })
    const part = within(tree).getByRole('treeitem', { name: /^Part/ })
    expect(within(part).getByRole('treeitem', { name: /^Hardware/ })).toHaveTextContent('lots')
    expect(within(part).getByRole('treeitem', { name: /^Nozzle/ })).toHaveTextContent('units')
    expect(within(tree).getByRole('treeitem', { name: /^Service/ })).toHaveTextContent('not counted')
  })

  it('adds a kind under a parent', async () => {
    const { user } = renderPage(<KindsPage />)
    await screen.findByRole('tree', { name: 'Item kinds' })
    await user.click(screen.getByRole('button', { name: 'Add kind' }))
    const form = screen.getByRole('form', { name: 'Add kind' })
    await user.type(within(form).getByLabelText('Id'), 'magnet')
    await user.type(within(form).getByLabelText('Name'), 'Magnet')
    await choose(user, within(form).getByRole('combobox', { name: 'Parent' }), 'Hardware')
    await user.click(within(form).getByRole('button', { name: 'Add kind' }))
    expect(await screen.findByRole('treeitem', { name: /^Magnet/ })).toBeInTheDocument()
    expect(bodyOf('createKind')).toEqual([{ id: 'magnet', name: 'Magnet', parent_id: 'hardware', tracking: 'lot', uom: 'each', life_basis: 'none', saleable: false }])
  })

  it("edits a kind's schema as a key table and saves it", async () => {
    const { user } = renderPage(<KindsPage />)
    const tree = await screen.findByRole('tree', { name: 'Item kinds' })
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
    const tree = await screen.findByRole('tree', { name: 'Item kinds' })
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
    const tree = await screen.findByRole('tree', { name: 'Item kinds' })
    await user.click(within(tree).getByRole('button', { name: 'Archive Hardware' }))
    expect(await screen.findByRole('alert')).toHaveTextContent("the kind's items")
    await user.click(within(tree).getByRole('button', { name: 'Archive Tool' }))
    await waitFor(() => expect(within(screen.getByRole('tree', { name: 'Item kinds' })).queryByRole('treeitem', { name: /^Tool/ })).toBeNull())
  })
})
