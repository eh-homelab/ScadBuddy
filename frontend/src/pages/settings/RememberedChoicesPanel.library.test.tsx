import { fireEvent, screen, waitFor } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it } from 'vitest'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { RememberedChoicesPanel } from './RememberedChoicesPanel'

describe('RememberedChoicesPanel · library files (#1754)', () => {
  it("names a library file's entries and forgets each through its own route", async () => {
    const choices: unknown[] = []
    const options: unknown[] = []
    server.use(
      http.get('/api/v1/settings/remembered', () =>
        HttpResponse.json({
          model_print_choices: { 'library:89': { printer_id: null, tier: 'fine' } },
          model_print_options: { 'library:89': { timelapse: true } },
        }),
      ),
      http.put('/api/v1/print/library/89/choices', async ({ request }) => {
        choices.push(await request.json())
        return HttpResponse.json({})
      }),
      http.put('/api/v1/settings/print-options', async ({ request }) => {
        options.push(await request.json())
        return HttpResponse.json({ defaults: {}, global_options: {}, printers: {}, models: {} })
      }),
    )
    renderPage(<RememberedChoicesPanel targets={null} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Forget printer and spools for Library file 89' }))
    await waitFor(() => expect(choices).toEqual([{}]))
    fireEvent.click(screen.getByRole('button', { name: 'Forget print options for Library file 89' }))
    await waitFor(() => expect(options).toEqual([{ scope: 'model', key: 'library:89', options: {} }]))
  })
})
