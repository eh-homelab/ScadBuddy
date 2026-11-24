import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { useLocation } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import type { Spool } from '../../api/filament'
import { server } from '../../mocks/server'
import { problem } from '../../mocks/handlers'
import { renderPage } from '../../test/utils'
import { BulkBar } from './BulkBar'

describe('the bulk bar', () => {
  const two = [{ id: 's2', number: 5 } as Spool, { id: 's1', number: 4 } as Spool]

  it('archives the selection in one request and reports each spool once (#7151)', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post('/api/v1/filament/spools/bulk-archive', async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ changed: ['s1'], unchanged: ['s2'], not_found: [] })
      }),
    )
    const onDone = vi.fn()
    const { user } = renderPage(<BulkBar selected={two} places={[]} types={[]} onDone={onDone} onClear={() => {}} />)
    await user.click(screen.getByRole('button', { name: 'Archive' }))
    await waitFor(() => expect(onDone).toHaveBeenCalled())
    expect(bodies).toEqual([{ spool_ids: ['s1', 's2'] }])
    expect(onDone).toHaveBeenCalledWith('Archived 1 spool. #5 was already archived.')
  })

  it('restores the selection in one request (#7151)', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post('/api/v1/filament/spools/bulk-restore', async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ changed: [], unchanged: [], not_found: ['s1', 's2'] })
      }),
    )
    const onDone = vi.fn()
    const { user } = renderPage(<BulkBar selected={two} places={[]} types={[]} onDone={onDone} onClear={() => {}} />)
    await user.click(screen.getByRole('button', { name: 'Restore' }))
    await waitFor(() => expect(onDone).toHaveBeenCalled())
    expect(bodies).toEqual([{ spool_ids: ['s1', 's2'] }])
    expect(onDone).toHaveBeenCalledWith('Restored 0 spools. #4 and #5 no longer exist.')
  })

  it('reports a refused bulk archive once, without doubling its full stop', async () => {
    server.use(http.post('/api/v1/filament/spools/bulk-archive', () => problem(503, 'Unavailable', 'The database is busy.')))
    const onDone = vi.fn()
    const { user } = renderPage(<BulkBar selected={two} places={[]} types={[]} onDone={onDone} onClear={() => {}} />)
    await user.click(screen.getByRole('button', { name: 'Archive' }))
    await waitFor(() => expect(onDone).toHaveBeenCalled())
    expect(onDone).toHaveBeenCalledWith('Archived no spool: The database is busy.')
  })

  it('edits the ticked fields of every selected spool in one request, an emptied one cleared (#7151)', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post('/api/v1/filament/spools/bulk-update', async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ changed: ['s1'], unchanged: ['s2'], not_found: [] })
      }),
    )
    const onDone = vi.fn()
    const { user } = renderPage(<BulkBar selected={two} places={[]} types={[]} onDone={onDone} onClear={() => {}} />)
    await user.click(screen.getByRole('button', { name: 'Edit fields' }))
    const dialog = await screen.findByRole('dialog', { name: 'Edit 2 spools' })
    expect(within(dialog).getByRole('textbox', { name: 'Note' })).toBeDisabled()
    await user.click(within(dialog).getByRole('checkbox', { name: 'Change Note' }))
    await user.type(within(dialog).getByRole('textbox', { name: 'Note' }), 'June order')
    await user.click(within(dialog).getByRole('checkbox', { name: 'Change Low stock (%)' }))
    await user.type(within(dialog).getByRole('textbox', { name: 'Low stock (%)' }), '15')
    await user.click(within(dialog).getByRole('checkbox', { name: 'Change Cost per kg' }))
    await user.click(within(dialog).getByRole('button', { name: 'Change 2 spools' }))
    await waitFor(() => expect(onDone).toHaveBeenCalledWith('Changed 1 spool. 1 spool already had these values.'))
    expect(bodies).toEqual([{ spool_ids: ['s1', 's2'], patch: { note: 'June order', low_stock_pct: 15, cost_per_kg: null } }])
  })

  it('words the Type pick\'s clear choice as "No type" and shows each subtype\'s material (#7322)', async () => {
    server.use(
      http.get('/api/v1/filament/catalog/materials', () => HttpResponse.json([{ id: 'm1', name: 'PLA' }, { id: 'm2', name: 'PETG' }])),
      http.get('/api/v1/filament/catalog/subtypes', () =>
        HttpResponse.json([
          { id: 'u1', name: 'Silk', material_id: 'm1' },
          { id: 'u2', name: 'Silk', material_id: 'm2' },
        ]),
      ),
    )
    const { user } = renderPage(<BulkBar selected={two} places={[]} types={[]} onDone={() => {}} onClear={() => {}} />)
    await user.click(screen.getByRole('button', { name: 'Edit fields' }))
    const dialog = await screen.findByRole('dialog', { name: 'Edit 2 spools' })
    await user.click(within(dialog).getByRole('checkbox', { name: 'Change Type' }))
    await user.click(within(dialog).getByRole('combobox', { name: 'Type' }))
    expect(await screen.findByRole('option', { name: 'No type' })).toBeInTheDocument()
    await user.keyboard('{Escape}')
    await user.click(within(dialog).getByRole('checkbox', { name: 'Change Subtype' }))
    await user.click(within(dialog).getByRole('combobox', { name: 'Subtype' }))
    expect(await screen.findByRole('option', { name: 'Silk (PLA)' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Silk (PETG)' })).toBeInTheDocument()
  })

  it('keeps the edit open with the reason when one spool refuses it (#7151)', async () => {
    server.use(http.post('/api/v1/filament/spools/bulk-update', () => problem(422, 'Unprocessable', 'spool #5: nozzle_temp_min is above nozzle_temp_max')))
    const onDone = vi.fn()
    const { user } = renderPage(<BulkBar selected={two} places={[]} types={[]} onDone={onDone} onClear={() => {}} />)
    await user.click(screen.getByRole('button', { name: 'Edit fields' }))
    const dialog = await screen.findByRole('dialog', { name: 'Edit 2 spools' })
    expect(within(dialog).getByRole('button', { name: 'Change 2 spools' })).toBeDisabled()
    await user.click(within(dialog).getByRole('checkbox', { name: 'Change Nozzle min (°C)' }))
    await user.type(within(dialog).getByRole('textbox', { name: 'Nozzle min (°C)' }), '210')
    await user.click(within(dialog).getByRole('button', { name: 'Change 2 spools' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Nothing changed. spool #5: nozzle_temp_min is above nozzle_temp_max')
    expect(onDone).not.toHaveBeenCalled()
  })

  it('refuses a whole number field that is not one before it sends', async () => {
    const { user } = renderPage(<BulkBar selected={two} places={[]} types={[]} onDone={() => {}} onClear={() => {}} />)
    await user.click(screen.getByRole('button', { name: 'Edit fields' }))
    const dialog = await screen.findByRole('dialog', { name: 'Edit 2 spools' })
    await user.click(within(dialog).getByRole('checkbox', { name: 'Change Label weight (g)' }))
    await user.type(within(dialog).getByRole('textbox', { name: 'Label weight (g)' }), 'lots')
    await user.click(within(dialog).getByRole('button', { name: 'Change 2 spools' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Enter the label weight (g) as a whole number.')
  })

  it('opens the labels of the selected spools, in number order', async () => {
    const Where = () => {
      const { pathname, search } = useLocation()
      return <p>{pathname + search}</p>
    }
    const { user } = renderPage(
      <>
        <BulkBar selected={[{ id: 'id2', number: 2 } as Spool, { id: 'id1', number: 1 } as Spool]} places={[]} types={[]} onDone={() => {}} onClear={() => {}} />
        <Where />
      </>,
    )
    await user.click(screen.getByRole('button', { name: 'Labels' }))
    expect(screen.getByText('/filament/labels?ids=id1,id2')).toBeInTheDocument()
  })
})
