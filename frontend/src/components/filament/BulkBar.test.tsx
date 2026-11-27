import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { useLocation } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { listSpoolEvents, type Spool } from '../../api/filament'
import { spoolId } from '../../mocks/features/filament/seed'
import { server } from '../../mocks/server'
import { problem } from '../../mocks/handlers'
import { renderPage } from '../../test/utils'
import { BulkBar } from './BulkBar'

describe('the bulk bar', () => {
  // The request listeners each test adds end with it.
  afterEach(() => server.events.removeAllListeners())

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

  it("resets the used counter of the selection in one request, only after a confirm (#7149)", async () => {
    const bodies: unknown[] = []
    server.events.on('request:start', async ({ request }) => {
      if (request.method === 'POST' && request.url.endsWith('/spools/reset-consumed')) bodies.push(await request.clone().json())
    })
    const gone = '00000000-0000-4000-8000-00000000dead'
    const selected = [{ id: spoolId(2), number: 2 } as Spool, { id: gone, number: 9 } as Spool]
    const onDone = vi.fn()
    const { user } = renderPage(<BulkBar selected={selected} places={[]} types={[]} onDone={onDone} onClear={() => {}} />)
    await user.click(screen.getByRole('button', { name: 'Reset used' }))
    const dialog = await screen.findByRole('alertdialog', { name: 'Reset the used counter of 2 spools?' })
    expect(bodies).toEqual([])
    await user.click(within(dialog).getByRole('button', { name: 'Reset counters' }))
    await waitFor(() => expect(onDone).toHaveBeenCalled())
    expect(bodies).toEqual([{ spool_ids: [spoolId(2), gone] }])
    expect(onDone).toHaveBeenCalledWith('Reset the used counter of 1 spool. #9 no longer exists.')
    expect((await listSpoolEvents(spoolId(2))).at(-1)?.kind).toBe('consumed_reset')
  })

  it('keeps counting when the reset is not confirmed (#7149)', async () => {
    const posts: string[] = []
    server.events.on('request:start', ({ request }) => {
      if (request.method === 'POST') posts.push(request.url)
    })
    const { user } = renderPage(<BulkBar selected={two} places={[]} types={[]} onDone={() => {}} onClear={() => {}} />)
    await user.click(screen.getByRole('button', { name: 'Reset used' }))
    const dialog = await screen.findByRole('alertdialog')
    await user.click(within(dialog).getByRole('button', { name: 'Keep counting' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(posts).toEqual([])
  })

  it('resets a selection of more than 500 spools in requests of 500 (#7149)', async () => {
    const sizes: number[] = []
    server.use(
      http.post('/api/v1/filament/spools/reset-consumed', async ({ request }) => {
        const { spool_ids } = (await request.json()) as { spool_ids: string[] }
        sizes.push(spool_ids.length)
        return HttpResponse.json({ reset: spool_ids.length, not_found: [] })
      }),
    )
    const many = Array.from({ length: 501 }, (_, at) => ({ id: `s${at}`, number: at + 1 }) as Spool)
    const onDone = vi.fn()
    const { user } = renderPage(<BulkBar selected={many} places={[]} types={[]} onDone={onDone} onClear={() => {}} />)
    await user.click(screen.getByRole('button', { name: 'Reset used' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Reset counters' }))
    await waitFor(() => expect(onDone).toHaveBeenCalledWith('Reset the used counter of 501 spools.'))
    expect(sizes).toEqual([500, 1])
  })

  it('reports a refused reset once (#7149)', async () => {
    server.use(http.post('/api/v1/filament/spools/reset-consumed', () => problem(503, 'Unavailable', 'The database is busy.')))
    const onDone = vi.fn()
    const { user } = renderPage(<BulkBar selected={two} places={[]} types={[]} onDone={onDone} onClear={() => {}} />)
    await user.click(screen.getByRole('button', { name: 'Reset used' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Reset counters' }))
    await waitFor(() => expect(onDone).toHaveBeenCalledWith('Reset no counter: The database is busy.'))
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

  it('says plainly that no spool changed when every one already had the values (#7322)', async () => {
    server.use(
      http.post('/api/v1/filament/spools/bulk-update', () => HttpResponse.json({ changed: [], unchanged: ['s1', 's2'], not_found: [] })),
    )
    const onDone = vi.fn()
    const { user } = renderPage(<BulkBar selected={two} places={[]} types={[]} onDone={onDone} onClear={() => {}} />)
    await user.click(screen.getByRole('button', { name: 'Edit fields' }))
    const dialog = await screen.findByRole('dialog', { name: 'Edit 2 spools' })
    await user.click(within(dialog).getByRole('checkbox', { name: 'Change Note' }))
    await user.type(within(dialog).getByRole('textbox', { name: 'Note' }), 'same')
    await user.click(within(dialog).getByRole('button', { name: 'Change 2 spools' }))
    await waitFor(() => expect(onDone).toHaveBeenCalledWith('No spool changed. 2 spools already had these values.'))
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

  it('sets a type with the unchecked bulk-update, so an incomplete spool takes it (#7703)', async () => {
    const bodies: unknown[] = []
    let patched = 0
    server.use(
      http.post('/api/v1/filament/spools/bulk-update', async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ changed: ['s1'], unchanged: [], not_found: [] })
      }),
      http.patch('/api/v1/filament/spools/:id', () => {
        patched += 1
        return HttpResponse.json({}, { status: 500 })
      }),
    )
    const onDone = vi.fn()
    const types = [{ id: 't1', code: 'PLA-RED', colour_name: null }] as unknown as Parameters<typeof BulkBar>[0]['types']
    const { user } = renderPage(<BulkBar selected={[two[1]!]} places={[]} types={types} onDone={onDone} onClear={() => {}} />)
    await user.click(screen.getByRole('button', { name: /Set type/ }))
    await user.click(await screen.findByRole('menuitem', { name: /PLA-RED/ }))
    await waitFor(() => expect(onDone).toHaveBeenCalledWith('Set PLA-RED on 1 spool.'))
    expect(bodies).toEqual([{ spool_ids: ['s1'], patch: { type_id: 't1' } }])
    expect(patched).toBe(0)
  })
})
