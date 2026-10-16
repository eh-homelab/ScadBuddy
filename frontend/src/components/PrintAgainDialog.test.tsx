import { screen } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { afterEach, describe, expect, it } from 'vitest'
import { printRunPoll } from '../api/client'
import { prints } from '../mocks/fixtures'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { PrintAgainDialog } from './PrintAgainDialog'

describe('PrintAgainDialog', () => {
  afterEach(() => {
    printRunPoll.intervalMs = 1000
  })

  it('offers no second Queue after an answer that may have queued it (review #1316 (13) 1a)', async () => {
    printRunPoll.intervalMs = 1
    const keys: (string | null)[] = []
    server.use(
      http.post('/api/v1/prints/35/reprint', ({ request }) => {
        keys.push(request.headers.get('Idempotency-Key'))
        return HttpResponse.json(
          {
            type: 'https://scadbuddy.dev/problems/temporal-unavailable',
            title: 'Service Unavailable',
            status: 503,
            detail: 'Temporal could not start this right now.',
            may_have_started: true,
          },
          { status: 503 },
        )
      }),
    )
    const print = prints.find((p) => p.archive_id === 35)!
    const { user } = renderPage(<PrintAgainDialog open print={print} onClose={() => undefined} />)

    await user.click(screen.getByRole('button', { name: 'Queue' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/check Bambuddy.s queue/i)
    // A new press would send a new key: a second print.
    expect(screen.queryByRole('button', { name: 'Queue' })).not.toBeInTheDocument()
    expect(new Set(keys).size).toBe(1)
  })

  it('offers Queue again after a refusal that queued nothing', async () => {
    server.use(
      http.post('/api/v1/prints/35/reprint', () =>
        HttpResponse.json({ title: 'Conflict', status: 409, detail: 'The archive is gone.' }, { status: 409 }),
      ),
    )
    const print = prints.find((p) => p.archive_id === 35)!
    const { user } = renderPage(<PrintAgainDialog open print={print} onClose={() => undefined} />)

    await user.click(screen.getByRole('button', { name: 'Queue' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('The archive is gone.')
    expect(screen.getByRole('button', { name: 'Queue' })).toBeEnabled()
  })
})
