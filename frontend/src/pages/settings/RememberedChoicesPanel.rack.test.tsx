import { fireEvent, screen, waitFor } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it } from 'vitest'
import type { BambuddyTargets } from '../../api/types'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { RememberedChoicesPanel } from './RememberedChoicesPanel'

describe('RememberedChoicesPanel · rack algorithm (#836)', () => {
  it('lists a remembered rack algorithm and forgets it', async () => {
    const forgot: unknown[] = []
    server.use(
      http.get('/api/v1/settings/remembered', () => HttpResponse.json({ printer_rack_algorithms: { '1': 'oldest_first' } })),
      http.put('/api/v1/print/printers/1/rack-algorithm', async ({ request }) => {
        forgot.push(await request.json())
        return HttpResponse.json({ printer_id: 1, algorithm: 'least_used' })
      }),
    )
    renderPage(<RememberedChoicesPanel targets={{ printers: [{ id: 1, name: 'H2C' }] } as BambuddyTargets} />)
    expect(await screen.findByText('Oldest first')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Forget rack nozzle for H2C' }))
    await waitFor(() => expect(forgot).toEqual([{ algorithm: null, version: expect.any(Number) }]))
  })
})
