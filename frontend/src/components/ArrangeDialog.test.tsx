import { screen, waitFor } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import type { Output } from '../api/types'
import * as fixtures from '../mocks/fixtures'
import { lastArrangeRequest } from '../mocks/handlers'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { ArrangeDialog } from './ArrangeDialog'

const first = fixtures.outputs[0] as Output
const second: Output = { ...first, id: 'o-2', name: 'second', manifest: [] }

describe('ArrangeDialog', () => {
  it('lists every object with its count and sends the build list', async () => {
    const onArranged = vi.fn()
    const { user } = renderPage(
      <ArrangeDialog open slug="name-keychain" outputs={[first]} onClose={vi.fn()} onArranged={onArranged} />,
    )
    const count = screen.getByLabelText('Copies of wall — Reagan')
    expect(count).toHaveValue(2)
    await user.clear(count)
    await user.type(count, '5')
    await user.selectOptions(screen.getByLabelText('Goal'), 'by_colour')
    await user.click(screen.getByRole('button', { name: 'Arrange' }))
    await waitFor(() => expect(onArranged).toHaveBeenCalledOnce())
    expect(onArranged).toHaveBeenCalledWith({
      output: expect.objectContaining({ arranged_from: [first.id] }),
      plates: 2,
    })
    expect(lastArrangeRequest()).toMatchObject({
      goal: 'by_colour',
      objects: [{ output_id: first.id, part: 'piece-wall', count: 5 }],
    })
  })

  it('says which outputs cannot be arranged', () => {
    renderPage(
      <ArrangeDialog open slug="name-keychain" outputs={[second]} onClose={vi.fn()} onArranged={vi.fn()} />,
    )
    expect(screen.getByRole('status')).toHaveTextContent(
      'second was saved before Arrange; generate it again to arrange it.',
    )
    expect(screen.getByRole('button', { name: 'Arrange' })).toBeDisabled()
  })

  it('shows why an arrange failed', async () => {
    const created_at = '2026-09-28T12:00:00Z'
    server.use(
      http.post('/api/v1/outputs/arrange', () =>
        HttpResponse.json(
          { id: 'arrange-fail', slug: 'name-keychain', status: 'pending', created_at },
          { status: 202 },
        ),
      ),
      http.get('/api/v1/jobs/arrange-fail', () =>
        HttpResponse.json({
          id: 'arrange-fail',
          slug: 'name-keychain',
          status: 'failed',
          created_at,
          error: "group 'big' does not fit on one plate",
        }),
      ),
    )
    const onArranged = vi.fn()
    const { user } = renderPage(
      <ArrangeDialog open slug="name-keychain" outputs={[first]} onClose={vi.fn()} onArranged={onArranged} />,
    )
    await user.click(screen.getByRole('button', { name: 'Arrange' }))
    expect(await screen.findByRole('alert')).toHaveTextContent("group 'big' does not fit on one plate")
    expect(onArranged).not.toHaveBeenCalled()
  })
})
